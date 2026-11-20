"""The controller's decisions: a rules engine for delivery, ground and tower.

The language model turns speech into an Intent; this module decides what the controller
says, from the real airport data, the weather and where the flight is in its sequence.
Every instruction is built from templates, so runways, routes, frequencies and pressure
are never invented. `handle` returns None for requests it does not cover, and the caller
may then fall back to a free-form reply.
"""

from __future__ import annotations

import math
import random
import re
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from enum import Enum

from . import answers, chart, ifr, questions, traffic, vfr
from . import phraseology as say
from .atis import (
    Issue,
    RunwaysInUse,
    close_runways,
    forget_runways,
    plan_runways,
    real_runways,
    runways_in_use,
    split_runways,
    uses_faa_style,
)
from .intents import Intent
from .navdata import Airport, AirportDB, Station, bearing, distance_nm, expect_station, forget_stations, prefer_airports
from .taxi import (
    FULL_LENGTH_M,
    INTERSECTION_NEEDS_M,
    TaxiNetwork,
    aircraft_category,
    assign_stand,
    end_key,
    nearest_parking,
    parse_taxi,
    route_to_point,
    route_to_runway,
    use_airport_runway_names,
)
from .vectors import locate

READBACK_WAIT_S = 30  # how long an instruction waits for a readback before it is repeated
CONTINUE_BEYOND_NM = 5  # further out than this, tower says "continue approach"
CLEAR_TO_LAND_NM = 4.5
GO_AROUND_NM = 1.0  # still no landing clearance this close in: go around
BACKTRACK_BLOCKS_NM = 8  # traffic on final this close keeps a backtrack off the runway: it holds it for minutes
LINED_UP_NM = 0.22  # this near the threshold (400 m), on the centreline and pointing down the runway, is lined up
LINED_UP_DEG = 20
EXIT_AHEAD_NM = 0.03  # a taxiway joining the runway this far ahead of the aircraft is still a way off it
LATE_GO_AROUND_NM = 2.0  # a runway that becomes occupied after the landing clearance: go around from this close in
# What must be read back. A readback that leaves one of these out is asked for once more.
CRITICAL = ("runway", "squawk", "frequency", "altitude", "heading", "hold_short", "crossing")
READBACK_WORDS = {
    "runway": "the runway",
    "squawk": "the squawk",
    "frequency": "the frequency",
    "altitude": "your cleared level",
    "heading": "the heading",
    "hold_short": "the runway to hold short of",
    "crossing": "the runway to cross",
}
REPOSITION_NM = 2.0  # on the ground, then on the ground this far away at the next look: the aircraft was moved
REPOSITION_AIRBORNE_NM = 10.0  # on the ground, then airborne this far away
ON_RUNWAY_NM = 0.022  # this close to a runway's centreline is on it
# A runway the taxi network never names counts as reached when a taxiway comes this near its centreline. Further than
# a holding point (up to 90 m out), so a network that stops at the holding points is not taken for a closed runway.
REACHED_WITHIN_M = 150
RUNWAY_END_NM = 0.008  # 15 m: a taxiway passing further beyond a runway's end does not cross it
HOLDING_SHORT_NM = 0.075  # stopped this close to a runway to be crossed: holding short of it
CROSSING_FINAL_NM = 3.0  # an aircraft on final this close in keeps a crossing waiting
REACHABLE_TRIES = 12  # stands tried for one the taxiways reach
RELEASE_DEPARTURE_NM = 25  # the tower hands a departure on within this distance (its own range is 30)


class Phase(Enum):
    PARKED = "parked"
    TAXI_OUT = "taxiing out"
    TAKEOFF_CLEARED = "cleared for takeoff"
    AIRBORNE = "airborne"
    LANDING_CLEARED = "cleared to land"
    LANDED = "landed"
    TAXI_IN = "taxiing in"


@dataclass
class Reply:
    text: str  # "" means the controller stays silent
    expect: dict = field(default_factory=dict)  # items the pilot must read back: runway, squawk, frequency, qnh
    on_correct: str = ""  # said after a correct readback (delivery's "readback correct")
    correction: bool = False  # a repeat of the pending instruction after a wrong readback
    note: str = ""  # with no text: why nobody answered (a blind call at an airfield without a controller)
    event: str = ""  # what this transmission is, for the debrief: readback_corrected, go_around, level_bust, ...
    asked: bool = False  # the pilot was already asked once to read back what was left out
    asks: dict = field(default_factory=dict)  # with a question: what its answer needs (see questions.py)


@dataclass
class Flight:
    phase: Phase = Phase.PARKED
    rules: str | None = None
    destination: str | None = None
    squawk: str | None = None
    runway: str | None = None  # runway assigned for departure or landing
    touch_and_go: bool = False
    has_been_airborne: bool = False
    origin: tuple[float, float] | None = None  # where the aircraft was first seen parked
    contacted: set[str] = field(default_factory=set)
    pending: Reply | None = None  # instruction awaiting readback
    last_said: dict[str, str] = field(default_factory=dict)  # per station, for "say again"
    told_departure: bool = False
    told_vacate: bool = False
    # Instrument flight (see ifr.py)
    plan: object | None = None  # flightplan.FlightPlan
    origin_ident: str | None = None  # the airport the flight is leaving from
    sid: str | None = None
    star: str | None = None
    cleared_ft: int | None = None  # last altitude or level cleared to
    arrival: object | None = None  # ifr.Arrival, once an approach controller has taken the flight
    descent_given: bool = False
    told_approach: bool = False
    told_sector: str | None = None  # last centre the flight was sent to
    approach_told_by: str | None = None  # the station that last said "contact approach"
    left_sector: tuple | None = None  # (centre, lat, lon): the centre that handed the flight on, and where
    sector_ahead: str | None = None  # the centre the flight was sent to before reaching its boundary
    direct_to: str | None = None
    next_frequency: str | None = None  # the frequency of the last hand-off, until a call is made on it
    handoffs: int = 0  # how many hand-offs have been given: the same frequency given twice is two hand-offs
    next_contact: tuple[str, str] | None = None  # (station, "Eastfield Tower one one niner decimal one"): that hand-off
    # (airport, runway, "departure" or "arrival"): asked for and granted, for that purpose only. It is checked
    # against the wind again every time it is used, and dropped once used or once the wind rules it out.
    requested_runway: tuple[str, str, str] | None = None
    heading_assigned: int | None = None  # the last heading given by any instruction, until it no longer stands
    crossing_given: str | None = None  # the runway of the last crossing clearance, to put back if the pilot cannot cross
    crossing_refused: str | None = None  # a crossing the pilot said "unable" to: given again only when asked for
    last_instruction: dict[str, tuple[str, dict, int]] = field(default_factory=dict)  # per station: (text, items, call)
    calls: int = 0  # how many calls the pilot has made: puts the instructions of different stations in order
    away_from: str | None = None  # the station the pilot was allowed to leave for a while; it stays quiet until called
    handoffs_held_until: float = 0.0  # after "unable" to a hand-off, radar controllers wait before giving another
    requested_approach: str | None = None  # the approach family asked for before reaching approach ("RNAV")
    star_procedure: object | None = None  # procedures.Procedure
    constraints: list | None = None  # restrictions.Constraint along the STAR; None until looked up
    deviating: bool = False  # off track around weather
    low_altitude_called: bool = False
    emergency: str | None = None  # "mayday" or "pan pan"
    services_told: bool = False
    following: bool = False  # VFR flight following
    waiting_takeoff: bool = False  # held short for traffic; the takeoff clearance follows
    backtrack: bool = False  # the taxi route ends where the runway is entered: the tower clears a backtrack first
    backtracking: bool = False  # cleared to backtrack and line up: the takeoff clearance comes at the threshold
    go_around_told: bool = False
    sid_constraints: list | None = None  # restrictions.Constraint: the SID's 'at or above' crossings
    atis_seen: dict[str, str] = field(default_factory=dict)  # airport -> information letter last heard
    awaiting_landing: bool = False  # told to continue approach; landing clearance still to come
    squawk_asked: bool = False
    bust_called_for: int | None = None  # the cleared level a bust was already called for
    pending_at: float = 0.0  # when the pending instruction was given (engine clock)
    unsaid: object | None = None  # the pending instruction while its voice is still being made: not yet on the air
    pending_key: str | None = None  # the station that gave it
    pending_khz: int | None = None  # the frequency it was given on: a station that shares it may answer for it
    reminded: bool = False
    # Taxi (see _taxi_out, _taxi_in)
    crossings: list[str] = field(default_factory=list)  # runways ("09R/27L") still to be crossed, each needing a clearance
    taxi_goal: tuple | None = None  # ("runway", ident) or ("stand", lat, lon, spoken name)
    taxi_route: object | None = None  # taxi.Route last given, for the map
    taxi_airport: str | None = None  # where that route is
    stand: object | None = None  # taxi.Parking assigned after landing
    go_arounds: int = 0
    diverting: bool = False  # on the way to another airport after a missed approach
    # Visual flying (see vfr.py)
    service: str | None = None  # the radar service a VFR flight has: flight following, basic, traffic, ...
    circuits: bool = False  # staying in the circuit after takeoff
    vfr_from: str | None = None  # the airport a VFR flight is departing from
    told_leaving: bool = False  # frequency change given on leaving the zone
    transit: str | None = None  # the airport whose zone is being crossed
    transit_inside: bool = False
    low_approach: bool = False
    special_vfr: bool = False
    airspace_called: set[str] = field(default_factory=set)  # airspaces already warned about
    last_note: str = ""  # why the last call got no answer, for the app
    last_event: str = ""  # the kind of the last controller transmission, for the flight log
    seen_at: tuple | None = None  # (lat, lon, on the ground) at the last look at the aircraft
    sid_path: list = field(default_factory=list)  # the SID's track over the ground, for route watching
    star_path: list = field(default_factory=list)
    sid_fixes: list = field(default_factory=list)  # the SID's waypoints as (name, lat, lon): the way back to it
    star_fixes: list = field(default_factory=list)
    question: object | None = None  # questions.Question: what a controller asked and the pilot has not answered
    off_route_since: float | None = None
    off_route_called: set[str] = field(default_factory=set)
    on_track: set[str] = field(default_factory=set)  # the SID, STAR or route the aircraft has been on: only those are watched
    qnh_told: bool = False  # the destination's pressure was given with a descent to an altitude
    route_points: list | None = None  # the filed route, waypoint by waypoint; None until worked out


def _end(ident: str) -> str:
    """A runway end as the scenery may write it: '07L' and '7L' are the same end."""
    return ident.strip().upper().lstrip("0") or "0"


def _taxiway_near(network: TaxiNetwork, taxi: set[int], runway) -> bool:
    """Whether a taxiway node lies within REACHED_WITHIN_M of the runway's centreline."""
    scale = math.cos(math.radians(runway.lat1))  # a degree of longitude in degrees of latitude, near enough
    ax, ay, bx, by = runway.lon1 * scale, runway.lat1, runway.lon2 * scale, runway.lat2
    dx, dy = bx - ax, by - ay
    for node in taxi:
        lat, lon = network.nodes[node]
        along = max(0.0, min(1.0, ((lon * scale - ax) * dx + (lat - ay) * dy) / (dx * dx + dy * dy or 1.0)))
        if distance_nm(lat, lon, ay + along * dy, (ax + along * dx) / scale) * 1852 <= REACHED_WITHIN_M:
            return True
    return False


def _squawk(rng: random.Random) -> str:
    while True:
        code = "".join(str(rng.randint(0, 7)) for _ in range(4))
        if code not in ("7500", "7600", "7700", "7000", "1200", "2000") and not code.endswith("00"):
            return code


class Engine:
    def __init__(
        self, db: AirportDB, seed: int | None = None, procedures=None, centers=None, navaids=None, clock=None,
        navfix=None, metars=None, holding: str = "never", traffic: bool = True, airspace=None, airways=None,
        real_atis=None,
    ):  # fmt: skip
        self.db = db
        # datis.RealAtis: the runways airports are really using, from their real digital ATIS. Used on real weather
        # only: with weather the pilot has set, the real airport's runways are not this one's.
        self.real_atis = real_atis
        self._real_weather = False  # whether the sim was last seen on real weather
        self.airways = airways  # airways.Airways: the waypoints along each airway
        self.airspace = airspace  # airspace.AirspaceIndex: control zones, class B/C/D, restricted areas
        self._zones: dict = {}  # airport -> its control zone
        self._patterns: dict = {}  # airport -> {runway: "left" | "right"}
        self.traffic_enabled = traffic  # traffic calls and runway sequencing against the sim's other aircraft
        self._traffic_seen: dict[int, float] = {}  # target -> when it was last called
        self.navfix = navfix  # navfix.NavFix: waypoint positions, holds, minimum safe altitudes
        self.metars = metars  # metar.MetarStore: reported weather at any airport
        self.holding = holding  # "never", "sometimes" or "always": holds before the approach
        self.now = clock or time.monotonic  # seconds; tests pass their own
        self._load_procedures = procedures  # ident -> procedures.Procedures; default reads X-Plane's CIFP
        self._procedures: dict = {}
        self.centers = centers  # centers.CenterIndex or None
        self.navaids = navaids  # procedures.NavaidNames or None
        self.flight = Flight()
        # Set by the radio: the calls `tick` decides are spoken by a voice that takes seconds to make, and are not
        # open to a readback until `on_air` says the voice has started. Without it they count as heard at once.
        self.voiced = False
        forget_runways()  # a new session: no plan yet, and nothing known of any airport's taxiways
        self.last_raw: dict = {}  # the latest sim snapshot the engine was shown
        self.lock = threading.RLock()
        self._networks: dict[str, TaxiNetwork] = {}
        self._closed: dict[str, set[str]] = {}  # airport -> runway ends no taxiway reaches
        self._charts: dict[str, dict] = {}
        self._map_airport: tuple[float, float, Airport | None] | None = None
        self._rerouted_at: tuple[float, float] | None = None
        # The seed is kept, and saved with the flight log, so that a log can be replayed with the same
        # squawk codes, stands and delays.
        self.seed = seed if seed is not None else random.SystemRandom().randrange(2**31)
        self._rng = random.Random(self.seed)

    # ---- helpers ------------------------------------------------------------

    def procedures(self, ident: str):
        """SIDs, STARs and approaches of an airport (empty when the sim has none for it)."""
        if ident not in self._procedures:
            if self._load_procedures:
                self._procedures[ident] = self._load_procedures(ident)
            else:
                from .procedures import load

                self._procedures[ident] = load(self.db.xplane_root, ident)
            airport = self.db.airport(ident)
            if airport:
                from .procedures import use_airport_runway_names

                use_airport_runway_names(self._procedures[ident], [end for runway in airport.runways for end, _ in runway.ends()])
        return self._procedures[ident]

    def set_plan(self, plan) -> None:
        """A new plan for a flight that has not started, or has ended, begins a new flight."""
        with self.lock:
            flight = self.flight
            parked = bool(self.last_raw.get("on_ground", True)) and self.last_raw.get("groundspeed_ms", 0.0) < 1
            if parked and (flight.has_been_airborne or flight.squawk or flight.contacted):
                self.reset()
            self.flight.plan = plan
            self.flight.route_points = None
            prefer_airports([plan.origin, plan.destination, plan.alternate] if plan else [])
            self.share_plan_runways()

    def share_plan_runways(self) -> None:
        """Give the ATIS the runways the plan names, so that the airport is taken to use them where the wind allows."""
        plan = self.flight.plan
        if plan is None:
            plan_runways(None, None, None, None)
            return
        origin, destination = self.db.airport(plan.origin or ""), self.db.airport(plan.destination or "")
        for airport in (origin, destination):
            if airport:
                self.closed_runways(airport)  # so that their ATIS leaves them out from the first time it is heard
                if self.real_atis is not None:
                    self.real_atis.runways(airport)  # asked for now, so that it is known by the time it is needed
        plan_runways(
            plan.origin,
            self._planned_runway(origin, "departure"),
            plan.destination,
            self._planned_runway(destination, "arrival"),
        )

    def route(self) -> list[tuple[str, float, float]]:
        """The filed route as every waypoint it passes, with positions. Worked out once per plan."""
        from .airways import expand

        flight, plan = self.flight, self.flight.plan
        if flight.route_points is None:
            origin = self.db.airport(plan.origin) if plan and plan.origin else None
            start = (origin.lat, origin.lon) if origin else (self.last_raw.get("lat", 0.0), self.last_raw.get("lon", 0.0))
            flight.route_points = expand(plan.route, self.airways, self.navfix, start) if plan else []
        return flight.route_points

    def reset(self) -> None:
        """Start a new flight: every clearance, contact and counter is forgotten. The plan is kept."""
        with self.lock:
            plan = self.flight.plan
            self.flight = Flight(plan=plan)
            self._traffic_seen.clear()
            forget_stations()  # who the last flight was sent to on a shared frequency does not carry over
            if self.centers:
                self.centers.forget()
            self._rng = random.Random(self.seed)  # a new flight begins the same sequence again

    def active_runways(self, airport: Airport, issue: Issue | None = None, weather=None, raw: dict | None = None) -> list[str]:
        """The runways in use, as the ATIS gives them: the ones the airport's real ATIS gives where that is known;
        never one no taxiway reaches while another remains. `raw`: the sim's state, from a caller the engine has
        not been shown it by (the ATIS broadcast)."""
        weather = issue.weather if issue else weather
        if weather is None:
            return []
        self.closed_runways(airport)  # known before the choice, and from then on to the ATIS too
        self.real_runways(airport, raw)
        return runways_in_use(airport, weather.wind_dir_true, weather.wind_kt)

    def real_runways(self, airport: Airport, raw: dict | None = None) -> None:
        """Tell the ATIS what the airport's real ATIS says of its runways: on real weather, where it has one."""
        if self.real_atis is None:
            real_runways(airport.ident, None)  # switched off: what was known of it is no longer used
            return
        raw = raw if raw is not None else self.last_raw
        if "wx_source" in raw:
            self._real_weather = raw["wx_source"] == 1
        found = self.real_atis.runways(airport)  # never waits: what is known now, and asked for when it is not
        real_runways(airport.ident, found if self._real_weather else None)

    def closed_runways(self, airport: Airport) -> set[str]:
        """Runway ends the scenery's taxiways do not reach: a runway drawn but closed, or one left out of the taxi
        network. A runway the network has as a runway with no taxiway joining it is one; so is a runway the network
        never names with no taxiway near it. One the network does not name but a taxiway comes near is not: many
        sceneries leave runways untagged. With no taxi network at all, no runway is known to be closed.
        apt.dat has no mark for a closed runway: a runway closed for good is taken out of the scenery or drawn
        without the taxiways that joined it, which is what is looked for here."""
        if airport.ident not in self._closed:
            network, closed = self.network(airport), set()
            if network.usable:
                taxi = network.taxi_nodes()
                joined = {
                    _end(end): bool(nodes & taxi) for name, nodes in network.runway_nodes.items() for end in name.split("/")
                }
                for runway in airport.runways:
                    known = [joined[_end(end)] for end in (runway.end1, runway.end2) if _end(end) in joined]
                    if not (any(known) if known else _taxiway_near(network, taxi, runway)):
                        closed.update((runway.end1, runway.end2))
            self._closed[airport.ident] = closed
            close_runways(airport.ident, closed)
        return self._closed[airport.ident]

    def _planned_runway(self, airport: Airport | None, purpose: str) -> str | None:
        """The plan's departure runway at its origin, or its arrival runway at its destination, as the airport names
        it: a plan's '01L' is the airport's '1L'."""
        plan = self.flight.plan
        if plan is None or airport is None:
            return None
        ident, runway = (
            (plan.origin, plan.departure_runway) if purpose == "departure" else (plan.destination, plan.arrival_runway)
        )
        if not runway or (ident or "").upper() != airport.ident:
            return None
        return next((end for rw in airport.runways for end in (rw.end1, rw.end2) if _end(end) == _end(runway)), None)

    @staticmethod
    def _long_enough(airport: Airport, idents: list[str], raw: dict) -> list[str]:
        """Of these runways, those as long as an aircraft of this kind needs; all of them when none is."""
        need = INTERSECTION_NEEDS_M[aircraft_category(raw.get("aircraft_type"))]
        lengths = {end: rw.length_m for rw in airport.runways for end in (rw.end1, rw.end2)}
        return [ident for ident in idents if lengths.get(ident, 0.0) >= need] or idents

    def network(self, airport: Airport) -> TaxiNetwork:
        if airport.ident not in self._networks:
            ends = [end for runway in airport.runways for end in (runway.end1, runway.end2)]
            self._networks[airport.ident] = use_airport_runway_names(parse_taxi(self.db.record_lines(airport)), ends)
        return self._networks[airport.ident]

    def _callsign(self, raw: dict, intent: Intent | None = None) -> str:
        """An airline flight number from the filed plan, else the aircraft's registration."""
        plan = self.flight.plan
        airline = say.airline_callsign(plan.callsign) if plan and plan.callsign else None
        if airline:
            return airline
        if raw.get("tail_number"):
            return say.spoken_callsign(raw["tail_number"])
        return intent.callsign if intent and intent.callsign else "Station calling"

    @staticmethod
    def _wind(issue: Issue | None, variation: float, faa: bool) -> str:
        if issue is None:
            return ""
        weather = issue.weather
        if weather.wind_kt < 1:
            return "wind calm"
        direction = round(((weather.wind_dir_true + variation) % 360) / 10) * 10 or 360
        spoken = say.digits(f"{direction:03d}")
        knots = say.digits(round(weather.wind_kt))
        gusting = weather.gust_kt and weather.gust_kt >= weather.wind_kt + 5
        if faa:
            return f"wind {spoken} at {knots}" + (f" gust {say.digits(round(weather.gust_kt))}" if gusting else "")
        return f"wind {spoken} degrees {knots}" + (f" gusting {say.digits(round(weather.gust_kt))}" if gusting else "") + " knots"

    @staticmethod
    def _pressure(issue: Issue | None, faa: bool) -> tuple[str, float | None]:
        if issue is None:
            return "", None
        if faa:
            inches = round(issue.weather.qnh_hpa * 0.02953, 2)
            return f"altimeter {say.digits(f'{round(inches * 100):04d}')}", inches
        return say.qnh(issue.weather.qnh_hpa), float(int(issue.weather.qnh_hpa))

    def requested_runway(self, airport: Airport, issue: Issue | None = None, weather=None) -> str | None:
        """The runway the pilot asked for at this airport and was granted, for what the flight is doing now:
        departing (never landed or airborne yet) or arriving. With the weather, it is checked against the wind
        again and forgotten when the wind no longer allows it."""
        asked = self.flight.requested_runway
        purpose = "arrival" if self.flight.has_been_airborne else "departure"
        if not asked or asked[0] != airport.ident or asked[2] != purpose:
            return None
        weather = issue.weather if issue else weather
        if weather is not None and answers.acceptable_runway(airport, asked[1], weather, self.last_raw):
            self.flight.requested_runway = None  # the wind has changed: back to the runway in use
            return None
        return asked[1]

    def _departure_runway(self, airport: Airport, issue: Issue | None, raw: dict) -> str | None:
        """The runway the pilot asked for, else the plan's when it is in use, else the departure runway whose threshold
        is nearest the aircraft, of those long enough for it."""
        if self.requested_runway(airport, issue):
            return self.requested_runway(airport, issue)
        # Runways no taxiway reaches (a grass strip beside the main runway, a closed one) are already left out.
        active = self.active_runways(airport, issue)
        planned = self._planned_runway(airport, "departure")
        # The plan's runway, when it is one of those departing: with a real ATIS, not one that only lands.
        if planned in (split_runways(active)[1] if isinstance(active, RunwaysInUse) else active):
            return planned
        active = self._long_enough(airport, split_runways(active)[1], raw)
        if not active:
            return None
        if "lat" not in raw:
            return active[0]
        return min(active, key=lambda ident: distance_nm(raw["lat"], raw["lon"], *self._threshold(airport, ident)))

    @staticmethod
    def _threshold(airport: Airport, ident: str) -> tuple[float, float]:
        for runway in airport.runways:
            if ident == runway.end1:
                return runway.lat1, runway.lon1
            if ident == runway.end2:
                return runway.lat2, runway.lon2
        return airport.lat, airport.lon

    def _handoff(
        self, airport: Airport, kind: str, raw: dict | None = None, to_aircraft: bool = False
    ) -> tuple[str, str, str] | None:
        """(spoken callsign, spoken frequency, 'nnn.nnn') of the airport's station of this kind. With the
        aircraft's position, the sector that works its side of the airport. With the position or `to_aircraft`,
        the flight is being sent there: on that frequency it is this station that answers."""
        bearing_from = None
        if raw and "lat" in raw:
            variation = raw.get("heading_mag", 0.0) - raw.get("heading_true", 0.0) if "heading_true" in raw else 0.0
            bearing_from = (bearing(airport.lat, airport.lon, raw["lat"], raw["lon"]) + variation) % 360
        station = self.db.station_of(airport, kind, bearing_from)
        if not station:
            return None
        if raw is not None or to_aircraft:
            # Said to an aircraft: on that frequency it is this station that answers, even where a centre,
            # another airport or another of this airport's controllers shares it.
            expect_station(station.khz, station.key)
        return station.callsign, say.frequency(station.khz), f"{station.khz / 1000:.3f}"

    def _greeting(self, station: Station) -> str:
        """The station names itself on the pilot's first call only."""
        key = station.key
        if key in self.flight.contacted:
            return ""
        self.flight.contacted.add(key)
        return f"{station.callsign}, "

    @staticmethod
    def _route_words(route) -> str:
        return f" via {say.join([say.taxiway(name) for name in route.taxiways])}" if route and route.taxiways else ""

    @staticmethod
    def _crossings(route) -> str:
        if not route or not route.crossings:
            return ""
        return "".join(f", cross runway {say.runway(name.split('/')[0])}" for name in route.crossings)

    # ---- readbacks ----------------------------------------------------------

    @staticmethod
    def _readback_heard(expect: dict, intent: Intent) -> set[str]:
        """The expected items the pilot said something for, right or wrong."""
        said = {
            "runway": intent.runway,
            "squawk": intent.squawk,
            "frequency": intent.frequency,
            "qnh": intent.qnh,
            "altitude": intent.altitude,
            "heading": intent.heading,
            "hold_short": intent.runway,
            "crossing": intent.runway,
        }
        return {key for key in expect if said.get(key) is not None}

    @staticmethod
    def _readback_errors(expect: dict, intent: Intent, own_runway: str | None = None) -> list[str]:
        """Items the pilot read back wrongly. What was left out is handled separately (see _check_readback)."""
        errors = []
        if "runway" in expect and intent.runway and intent.runway != expect["runway"]:
            errors.append("runway")
        for key in ("hold_short", "crossing"):
            # 'Hold short runway 27L' on the way to 27R: either of those runways is a correct thing to say back.
            if key in expect and intent.runway and intent.runway not in (*expect[key].split("/"), own_runway):
                errors.append("runway")
        if "squawk" in expect and intent.squawk and intent.squawk != expect["squawk"]:
            errors.append("squawk")
        if "frequency" in expect and intent.frequency and abs(float(intent.frequency) - float(expect["frequency"])) > 0.006:
            errors.append("frequency")
        if "qnh" in expect and intent.qnh and abs(intent.qnh - expect["qnh"]) > (0.011 if expect["qnh"] < 100 else 0.5):
            errors.append("qnh")
        if "altitude" in expect and intent.altitude is not None and abs(intent.altitude - expect["altitude"]) > 50:
            errors.append("altitude")
        # Heading zero is north, not a missing value.
        if "heading" in expect and intent.heading is not None and abs((intent.heading - expect["heading"] + 180) % 360 - 180) > 2:
            errors.append("heading")
        return errors

    @staticmethod
    def instruction_body(text: str, station_callsign: str) -> str:
        """An instruction without the callsign, the station's self-introduction and 'identified'."""
        body = text.split(", ", 1)[1] if ", " in text else text
        for lead in (f"{station_callsign}, ", "identified, ", "radar contact, "):
            if body.startswith(lead):
                body = body[len(lead) :]
        return body

    @staticmethod
    def _readback_intent(expect: dict) -> dict:
        """A complete, correct readback of these items, as the 'Read back' button and the copilot send it."""
        intent = {"intent": "readback", **expect}
        for key in ("hold_short", "crossing"):
            if key in expect:
                intent["runway"] = expect[key].split("/")[0]
        return intent

    def _check_readback(self, intent: Intent, callsign: str, station, raw: dict) -> Reply | None:
        pending = self.flight.pending
        # A request that happens to carry a number (a level asked for, a runway named) is not a readback.
        if pending is None or intent.intent not in ("readback", "other"):
            return None
        if not self.waiting_on(station, raw):
            return None  # read back to someone else: the controller who gave the instruction has not heard it
        values = (intent.runway, intent.squawk, intent.frequency, intent.qnh, intent.altitude, intent.heading)
        if intent.intent != "readback" and all(value is None for value in values):
            return None
        errors = self._readback_errors(pending.expect, intent, self.flight.runway)
        if errors:
            body = self.instruction_body(pending.text, station.callsign)
            return Reply(
                f"{callsign}, negative, {body}", pending.expect, pending.on_correct, correction=True, event="readback_corrected"
            )
        if intent.intent != "readback":
            return None  # something else that happens to mention a correct value
        # "Roger" is not a readback. What was left out is asked for, once: after that the readback is
        # taken as given, because speech recognition does drop words and a loop helps nobody.
        heard = self._readback_heard(pending.expect, intent)
        missing = [key for key in pending.expect if key in CRITICAL and key not in heard]
        if missing and not pending.asked:
            pending.asked = True
            items = say.join([READBACK_WORDS[key] for key in missing])
            return Reply(
                f"{callsign}, read back {items}", pending.expect, pending.on_correct, correction=True, event="readback_incomplete"
            )
        self.flight.pending = None
        return Reply(f"{callsign}, {pending.on_correct}" if pending.on_correct else "")

    # ---- main entry ---------------------------------------------------------

    def handle(self, intent: Intent, station: Station, issue: Issue | None, raw: dict, variation: float = 0.0) -> Reply | None:
        with self.lock:
            self._observe(raw)
            key = station.key
            callsign = self._callsign(raw, intent)
            intent = self._runway_as_written(intent, station)
            intent, instead = questions.confirmed(self, intent, station, callsign)
            if instead is not None:
                return self._said(instead, key)
            intent = self._meaning(intent, station, raw)
            self.flight.calls += 1
            if self.flight.away_from == key:
                self.flight.away_from = None  # back on frequency
            told = self.flight.next_frequency
            if told and abs(float(told) * 1000 - station.khz) <= 6:
                self.flight.next_frequency = None  # the hand-off is made: the pilot is calling that station
            answered = questions.answer(self, intent, station, issue, raw, variation, callsign, self._greeting_if(station))
            if answered is not None:
                self.flight.contacted.add(key)
                return self._said(answered, key)
            if intent.intent in ("affirm", "negative"):
                # Nothing was asked: "negative" to an instruction is "unable"; otherwise there is nothing to answer.
                waiting = self.flight.pending and self.flight.pending_key == key
                intent = replace(intent, intent="unable" if intent.intent == "negative" and waiting else "acknowledge")
            if intent.intent == "say_again":
                previous = self.flight.last_said.get(key)
                if previous:
                    return Reply(previous, self.flight.pending.expect if self.flight.pending else {})
                if station.airport is None or vfr.service(station) not in ("ctaf", "unicom"):
                    return Reply(f"{callsign}, {self._greeting(station)}no instructions for you")
            if intent.intent == "acknowledge":
                return Reply("")  # "thank you", "good day", "standby": nothing to answer
            if intent.intent in ("mayday", "pan_pan", "cancel_emergency"):
                reply = self._emergency(intent, station, callsign)
                if reply and reply.text:
                    self.flight.last_said[key] = reply.text
                    questions.record(self, key, reply)
                return reply

            self._reached(station, raw)
            reply = self._check_readback(intent, callsign, station, raw)
            if reply is None:
                if intent.intent == "readback":
                    reply = Reply("", note=self._not_for(station, raw))  # an acknowledgement with nothing outstanding
                else:
                    contacted = set(self.flight.contacted)
                    reply = self._decide(intent, station, issue, raw, variation, callsign)
                    if reply is None:
                        # No rule for this call: the controller still says who can help, or what it can do.
                        self.flight.contacted = set(contacted)
                        reply = answers.way_out(self, intent, station, raw, callsign, self._greeting(station))
                    if reply is None:
                        self.flight.contacted = contacted  # the free-form fallback will do the greeting
            if reply is None:
                return None
            if reply.correction:
                return reply  # the original instruction stays pending and is what "say again" repeats
            questions.settle(self, intent, station)
            return self._said(reply, key)

    def confirm_request(self, intent: Intent, station: Station, raw: dict) -> Reply | None:
        """'Confirm you request descent to flight level two zero zero': asked when a call was understood only on a
        second reading (resolver.py). None when the call cannot be put as a question; the caller then answers
        as it would have without it."""
        with self.lock:
            intent = self._runway_as_written(intent, station)
            words = answers.confirm_words(self, intent, station, raw)
            if not words:
                return None
            callsign = self._callsign(raw, intent)
            reply = Reply(f"{callsign}, {self._greeting(station)}confirm {words}")
            said = {key: value for key, value in asdict(intent).items() if value is not None and value is not False}
            reply.asks = {"intent": {key: value for key, value in said.items() if value != ""}}
            return self._said(reply, station.key)

    def _runway_as_written(self, intent: Intent, station: Station) -> Intent:
        """A runway the pilot said, written the way the airport writes it. The language model writes '04R' and '09';
        an airport in the United States calls them '4R' and '9'. Compared as they came, a correct readback of
        "runway four right" was refused and a request for it was not found."""
        if not intent.runway:
            return intent
        plan = self.flight.plan
        idents = [station.airport.ident] if station.airport else []
        idents += [ident for ident in (plan.destination, plan.origin) if ident] if plan else []
        if self.flight.arrival:
            idents.append(self.flight.arrival.airport)
        for ident in dict.fromkeys(idents):
            airport = self.db.airport(ident) if not (station.airport and station.airport.ident == ident) else station.airport
            for runway in airport.runways if airport else []:
                for end in (runway.end1, runway.end2):
                    if _end(end) == _end(intent.runway):
                        return replace(intent, runway=end) if end != intent.runway else intent
        return intent

    def _said(self, reply: Reply, key: str) -> Reply:
        """What the controller said is remembered: for a readback, for "say again", and as a question to answer."""
        if reply.expect:
            self._await_readback(reply, key)
        if reply.text:
            self.flight.last_said[key] = reply.text
            questions.record(self, key, reply)
        return reply

    def _greeting_if(self, station: Station) -> str:
        """The station's name when this is the first call to it, without counting the call as made yet."""
        return "" if station.key in self.flight.contacted else f"{station.callsign}, "

    def _meaning(self, intent: Intent, station: Station, raw: dict) -> Intent:
        """Calls that mean another call where the aircraft is: 'request delay' in the air is a hold,
        'request return' in the air a diversion back, a check-in on the ground a first call."""
        name, airborne = intent.intent, not bool(raw.get("on_ground", True))
        if name == "say_again" and intent.item:
            return replace(intent, intent="request_information")
        if name == "acknowledge" and self.flight.pending:
            return replace(intent, intent="readback")  # "wilco" to an instruction: it is still to be read back
        if name == "check_in" and not airborne:
            return replace(intent, intent="initial_call")
        if name == "request_delay" and airborne:
            return replace(intent, intent="request_hold")
        if name == "request_return" and airborne:
            if station.airport and station.airport.ident == self.flight.origin_ident and station.kind not in ifr.RADAR_KINDS:
                return replace(intent, intent="inbound")  # the tower of the airport the flight left: back to land
            # Anywhere else it is a diversion back to where the flight came from: radar's to give.
            return replace(intent, intent="request_diversion", destination=self.flight.origin_ident)
        if name == "request_return" and vfr.service(station) == "afis":
            return replace(intent, intent="runway_vacated")  # to parking at the pilot's discretion
        return intent

    def _common(self, intent: Intent, station: Station, issue: Issue | None, raw: dict, variation: float, callsign: str):
        """Calls every controller answers alike: a radio check, a question, 'unable', a runway, 'not ready yet'."""
        name, on_ground = intent.intent, bool(raw.get("on_ground", True))
        faa = answers.faa_for(station)
        if name not in ("radio_check", "request_information", "unable", "request_runway", "request_delay", "report_leaving",
                        "request_return"):  # fmt: skip
            return None
        informing = vfr.service(station) if station.airport else None
        if informing == "afis" and name not in ("radio_check", "request_information", "request_runway"):
            return None  # an information service gives no instructions: its own rules, or a plain "roger"
        if informing == "afis" and name == "request_runway":
            return answers.afis_runway(self, intent, station, issue, raw, variation, callsign, self._greeting(station))
        if name in ("request_delay", "report_leaving", "request_return") and not on_ground:
            return None
        if name == "request_return":
            kinds = {freq.kind for freq in station.airport.freqs} if station.airport else set()
            if not station.airport or station.kind != "ground" and (station.kind != "tower" or "ground" in kinds):
                return None  # another controller's: the way out sends the pilot there
            if self.flight.phase == Phase.PARKED and not self.flight.has_been_airborne:
                return Reply(f"{callsign}, {self._greeting(station)}roger")  # at the stand already
            if self.flight.phase != Phase.TAXI_OUT or self.flight.has_been_airborne:
                return None  # cleared for takeoff (the tower's to cancel), or arrived: the way out answers
            self.flight.runway, self.flight.waiting_takeoff = None, False
            self.flight.backtrack = self.flight.backtracking = False
            return self._taxi_in(station, raw, callsign, f"{self._greeting(station)}roger, ", faa)
        hello = self._greeting(station)
        if name == "radio_check":
            return Reply(f"{callsign}, {hello}reading you five")
        if name == "request_information":
            return answers.information(self, intent, station, issue, raw, variation, callsign, hello)
        if name == "unable":
            return answers.unable(self, station, raw, callsign, hello, faa)
        if name == "request_runway":
            return answers.runway_request(self, intent, station, issue, raw, callsign, hello, faa)
        if name == "request_delay":
            return answers.delay(self, station, raw, callsign, hello, faa)
        return answers.leave_frequency(self, station, raw, callsign, hello, faa)

    def on_runway(self, airport: Airport, raw: dict) -> bool:
        """Whether the aircraft is on one of the airport's runways (within its length, near the centreline)."""
        if "lat" not in raw:
            return False
        for runway in airport.runways:
            heading = runway.ends()[0][1]
            position = locate(raw["lat"], raw["lon"], (runway.lat2, runway.lon2), heading)
            if position.cross_nm < 0.035 and -0.05 < position.along_nm < runway.length_m / 1852 + 0.05:
                return True
        return False

    def _gives_clearance(self, station: Station, kinds: set[str], raw: dict) -> bool:
        """Delivery gives clearances; without one, ground does; without either, tower. A flight that
        starts on the runway (as X-Plane likes to place them) gets it from tower rather than being
        sent back to delivery."""
        if station.kind == "delivery":
            return True
        if station.kind == "ground":
            return "delivery" not in kinds
        if station.kind == "tower":
            return not kinds & {"delivery", "ground"} or self.on_runway(station.airport, raw)
        return False

    def _emergency(self, intent: Intent, station: Station, callsign: str) -> Reply | None:
        """Mayday and pan-pan: acknowledged at once, on any frequency, and the flight gets priority."""
        flight = self.flight
        if intent.intent == "cancel_emergency":
            if not flight.emergency:
                return Reply(f"{callsign}, {self._greeting(station)}roger")  # nothing declared: nothing to cancel
            flight.emergency = None
            return Reply(f"{callsign}, roger, emergency cancelled")
        flight.emergency = "mayday" if intent.intent == "mayday" else "pan pan"
        flight.squawk, flight.services_told = "7700", False
        hello = self._greeting(station)
        return Reply(f"{callsign}, {hello}roger {flight.emergency}, squawk seven seven zero zero, say intentions")

    def waiting_on(self, station: Station, raw: dict) -> bool:
        """Whether the controller on frequency is the one waiting for a readback: the station that gave the
        instruction, or whoever answers on the frequency it was given on (where a centre and an airport share
        one, which of them answers can change while the pilot has not touched the radio)."""
        from .xpbridge import tuned_khz

        flight = self.flight
        if flight.pending is None or flight.pending is flight.unsaid:
            return False  # nothing given, or given and not heard yet: there is nothing to read back
        if flight.pending_key == station.key:
            return True
        khz = tuned_khz(raw)
        return flight.pending_khz is not None and khz is not None and abs(khz - flight.pending_khz) <= 5

    def _reached(self, station: Station, raw: dict) -> None:
        """A call on the frequency the flight was handed to completes the hand-off: there is no frequency left
        to follow, and a hand-off not yet read back to the controller who gave it has been obeyed instead."""
        flight, khz = self.flight, getattr(station, "khz", None)
        if khz is None:
            return
        if flight.next_frequency and abs(float(flight.next_frequency) * 1000 - khz) <= 5:
            flight.next_frequency = None
        handed = flight.pending.expect.get("frequency") if flight.pending else None
        if handed and not self.waiting_on(station, raw) and abs(float(handed) * 1000 - khz) <= 5:
            flight.pending = None

    def _not_for(self, station: Station, raw: dict) -> str:
        """Why a readback got no answer: the instruction it reads back was another controller's."""
        if self.flight.pending is None or self.waiting_on(station, raw):
            return ""
        return f"That readback was not for {station.callsign}: read it back to the controller who gave the instruction."

    def _await_readback(self, reply: Reply, key: str) -> None:
        flight = self.flight
        if "frequency" in reply.expect:
            flight.next_frequency = reply.expect["frequency"]  # offered in the app as a one-click tune
            flight.handoffs += 1
            told = re.search(r"contact ([^,]+)", reply.text)
            flight.next_contact = (key, told.group(1)) if told else None
        if "heading" in reply.expect:
            flight.heading_assigned = reply.expect["heading"]
        # Kept after the readback: "unable" said a moment later is about this instruction.
        flight.last_instruction[key] = (reply.text, dict(reply.expect), flight.calls)
        flight.pending, flight.pending_key, flight.pending_at, flight.reminded = reply, key, self.now(), False
        from .xpbridge import tuned_khz

        flight.pending_khz = tuned_khz(self.last_raw)

    def _decide(
        self, intent: Intent, station: Station, issue: Issue | None, raw: dict, variation: float, callsign: str
    ) -> Reply | None:
        if station.airport is None or vfr.service(station) not in ("ctaf", "unicom"):
            common = self._common(intent, station, issue, raw, variation, callsign)
            if common is not None:
                return common
        if station.kind in ifr.RADAR_KINDS and not bool(raw.get("on_ground", True)):
            return ifr.handle(self, intent, station, issue, raw, variation, callsign, self._greeting(station))
        if station.kind == "center":
            return None
        airport, flight = station.airport, self.flight
        faa = uses_faa_style(airport.ident)
        on_ground = bool(raw.get("on_ground", True))
        kinds = {freq.kind for freq in airport.freqs}
        informing = vfr.service(station)
        if informing in ("ctaf", "unicom"):
            return vfr.ctaf(self, intent, station, issue, raw, variation, callsign, informing)
        hello = self._greeting(station)
        if informing == "afis":
            if on_ground and not flight.has_been_airborne:
                flight.origin_ident = airport.ident
            return vfr.afis(self, intent, station, issue, raw, variation, callsign, hello)
        if on_ground and not flight.has_been_airborne:
            flight.origin_ident = airport.ident
        pressure, pressure_value = self._pressure(issue, faa)
        pressure_tail = f", {pressure}" if pressure else ""

        if intent.intent == "radio_check":
            return Reply(f"{callsign}, {hello}reading you five")
        if intent.intent == "request_weather":
            return ifr.weather_request(self, station, issue, raw, variation, callsign, hello)
        if intent.intent == "initial_call":
            return Reply(f"{callsign}, {hello}{'go ahead' if faa else 'pass your message'}")

        # Clearance: delivery, or the next station down when the airport has no delivery.
        gives_clearance = self._gives_clearance(station, kinds, raw)
        if intent.intent == "request_clearance" and on_ground and flight.has_been_airborne and raw.get("groundspeed_ms", 0.0) < 1:
            # Landed, parked, and asking for a clearance: this is the next leg.
            self.reset()
            flight = self.flight
            flight.contacted.add(station.key)
            flight.origin_ident = airport.ident
        if intent.intent in ("request_circuits", "request_special_vfr") and on_ground:
            # Both are VFR departure clearances with something extra.
            if not gives_clearance and station.kind in ("ground", "tower"):
                target = self._handoff(airport, "delivery", to_aircraft=True) or self._handoff(
                    airport, "ground", to_aircraft=True
                )
                return Reply(f"{callsign}, {hello}contact {target[0]} {target[1]} for clearance", {"frequency": target[2]})
            if gives_clearance:
                flight.runway = self._departure_runway(airport, issue, raw)
                runway_part = f", departure runway {say.runway(flight.runway)}" if flight.runway else ""
                return vfr.departure(
                    self, intent, station, issue, raw, callsign, hello, faa, runway_part, pressure_tail,
                    "readback correct" + (", report ready for departure" if station.kind == "tower" else ""),
                )  # fmt: skip
        if intent.intent == "request_clearance" and on_ground:
            if station.kind in ("ground", "tower") and not gives_clearance:
                target = self._handoff(airport, "delivery", to_aircraft=True) or self._handoff(
                    airport, "ground", to_aircraft=True
                )
                return Reply(f"{callsign}, {hello}contact {target[0]} {target[1]} for clearance", {"frequency": target[2]})
            if not gives_clearance:
                return None
            runway = self._departure_runway(airport, issue, raw)
            runway_part = f", departure runway {say.runway(runway)}" if runway else ""
            atis_part = f", information {say.letter(issue.letter)} is current" if issue and intent.atis != issue.letter else ""
            has_plan = bool(flight.plan and flight.plan.destination)
            visual = intent.flight_rules == "VFR" or (intent.flight_rules is None and not intent.destination and not has_plan)
            flight.runway = runway
            next_station = self._handoff(airport, "ground", to_aircraft=True) if station.kind == "delivery" else None
            on_correct = "readback correct" + (
                f", contact {next_station[0]} {next_station[1]} when ready for push and start"
                if next_station
                else ", report ready for departure"
                if station.kind == "tower"
                else ""
            )
            if visual:
                return vfr.departure(
                    self,
                    intent,
                    station,
                    issue,
                    raw,
                    callsign,
                    hello,
                    faa,
                    runway_part,
                    f"{pressure_tail}{atis_part}",
                    on_correct,
                )
            if has_plan:
                return ifr.clearance(self, station, issue, raw, callsign, hello, faa, atis_part, on_correct)
            if not intent.destination and not flight.destination:
                return Reply(f"{callsign}, {hello}say destination")
            flight.rules, flight.destination = "IFR", intent.destination or flight.destination
            flight.squawk = flight.squawk if flight.squawk and flight.squawk not in ("1200", "7000") else _squawk(self._rng)
            text = (
                f"{callsign}, {hello}cleared to {flight.destination} as filed{runway_part}, "
                f"squawk {say.digits(flight.squawk)}{atis_part}"
            )
            return Reply(text, {"squawk": flight.squawk}, on_correct)

        if station.kind == "delivery":
            if intent.intent in ("request_pushback", "request_taxi", "ready_for_departure"):
                target = self._handoff(airport, "ground", to_aircraft=True) or self._handoff(airport, "tower", to_aircraft=True)
                if target:
                    return Reply(f"{callsign}, {hello}contact {target[0]} {target[1]}", {"frequency": target[2]})
            return None

        if on_ground and station.kind in ("ground", "tower"):
            if intent.intent == "request_crossing" and flight.crossings:
                return self._crossing(station, raw, callsign, hello, faa, asked=True)
            if intent.intent == "request_progressive":
                return self._progressive(station, raw, callsign, hello, faa)
        if station.kind == "ground":
            return self._ground(intent, station, issue, raw, callsign, hello, faa, pressure_tail, on_ground)
        if station.kind == "tower":
            return self._tower(intent, station, issue, raw, variation, callsign, hello, faa, pressure_tail, on_ground, kinds)
        return None

    # ---- ground -------------------------------------------------------------

    def _taxi_out(self, station, issue, raw, callsign, hello, faa, pressure_tail) -> Reply | None:
        airport, flight = station.airport, self.flight
        runway = flight.runway or self._departure_runway(airport, issue, raw)
        if not runway:
            return None
        flight.runway, flight.phase = runway, Phase.TAXI_OUT
        route = None
        if "lat" in raw:
            category = aircraft_category(raw.get("aircraft_type"), self._airline() is not None)
            route = route_to_runway(
                self.network(airport), raw["lat"], raw["lon"], runway, self._threshold(airport, runway), category
            )
            self._airport_names(airport, route, exclude=runway, start=(raw["lat"], raw["lon"]))
        via = self._route_words(route)
        flight.taxi_goal, flight.crossings = ("runway", runway), list(route.crossings) if route else []
        flight.taxi_route, flight.taxi_airport = route, airport.ident
        flight.backtrack, flight.backtracking = bool(route and route.backtrack), False
        limit = self._limit()
        if flight.backtrack:
            limit = f"{limit}, expect {'back-taxi' if faa else 'backtrack'}"  # the tower clears it at the runway
        # From an intersection, the pilot is told so and how much runway is left: they may refuse it.
        if route and route.available_m and route.taxiways:
            entry = say.taxiway(route.taxiways[-1])
            left = f"{round(route.available_m / 0.3048, -2):,.0f} feet" if faa else f"{round(route.available_m, -1):,.0f} metres"
            limit = f", intersection departure at {entry}, {left} available{limit}"
        if faa:
            text = f"{callsign}, {hello}runway {say.runway(runway)}, taxi{via}{limit}{pressure_tail}"
        else:
            text = f"{callsign}, {hello}taxi to holding point runway {say.runway(runway)}{via}{limit}{pressure_tail}"
        # With a runway to hold short of, two runways are said: the readback is not checked against one of them.
        return Reply(text, {"hold_short": flight.crossings[0]} if flight.crossings else {"runway": runway})

    def _taxi_in(self, station, raw, callsign, hello, faa) -> Reply:
        airport, flight = station.airport, self.flight
        flight.phase = Phase.TAXI_IN
        network = self.network(airport)
        stand, route = None, None
        if "lat" in raw and network.usable:
            # Back to where the flight started if that was here, otherwise the nearest stand.
            home = flight.origin if flight.origin and distance_nm(*flight.origin, airport.lat, airport.lon) < 5 else None
            if home:
                stand = nearest_parking(network, *home)
            else:
                airline = self._airline()
                category = aircraft_category(raw.get("aircraft_type"), bool(airline))
                stand = flight.stand or assign_stand(network, category, airline, (raw["lat"], raw["lon"]), self._rng)
                stand = stand or nearest_parking(network, raw["lat"], raw["lon"])
            if stand:
                route = self._route_to_stand(network, raw, (stand.lat, stand.lon))
            if stand and route is None and not home:
                # No way there over the taxiways from where the aircraft left the runway: the nearest stand that
                # fits and that it can taxi to.
                category = aircraft_category(raw.get("aircraft_type"), self._airline() is not None)
                stand, route = self._reachable_stand(network, raw, stand, category) or (stand, None)
            self._airport_names(airport, route, start=(raw["lat"], raw["lon"]))
        flight.taxi_route, flight.taxi_airport = route, airport.ident
        named = stand and stand.name and len(stand.name) <= 12
        destination = f"{'gate' if faa else 'stand'} {say.stand(stand.name)}" if named else "parking"
        flight.stand = stand
        flight.crossings = list(route.crossings) if route else []
        flight.taxi_goal = ("stand", stand.lat, stand.lon, destination) if stand else None
        limit = self._limit()
        return Reply(
            f"{callsign}, {hello}taxi to {destination}{self._route_words(route)}{limit}",
            {"hold_short": flight.crossings[0]} if flight.crossings else {},
        )

    # ---- runway crossings and progressive taxi ------------------------------

    def _limit(self) -> str:
        """', hold short runway two seven left' while a runway on the route has not been cleared for crossing."""
        crossings = self.flight.crossings
        return f", hold short runway {say.runway(crossings[0].split('/')[0])}" if crossings else ""

    def _goal_words(self, faa: bool) -> str:
        goal = self.flight.taxi_goal
        if not goal:
            return ""
        if goal[0] == "runway":
            return f"runway {say.runway(goal[1])}" if faa else f"holding point runway {say.runway(goal[1])}"
        return goal[3]

    @staticmethod
    def _runway_named(airport: Airport, name: str):
        ends = {end_key(end) for end in name.split("/")}  # the network's '04R/22L' is the airport's '4R'
        return next((r for r in airport.runways if end_key(r.end1) in ends or end_key(r.end2) in ends), None)

    def _offset_nm(self, airport: Airport, name: str, raw: dict, beyond_nm: float = 0.05) -> float | None:
        """Distance from the centreline of this runway, while abeam it (or no further than `beyond_nm` past an end);
        None when not beside it. To tell whether a point is on the runway, `RUNWAY_END_NM`: beyond its end there is
        at most a blast pad, which is not runway."""
        runway = self._runway_named(airport, name)
        if not runway or "lat" not in raw:
            return None
        position = locate(raw["lat"], raw["lon"], (runway.lat2, runway.lon2), runway.ends()[0][1])
        if not -beyond_nm < position.along_nm < runway.length_m / 1852 + beyond_nm:
            return None
        return position.cross_nm

    def _crossing(self, station: Station, raw: dict, callsign: str, hello: str, faa: bool, asked: bool) -> Reply | None:
        """Clearance to cross the next runway on the taxi route, unless traffic is using it."""
        flight, airport = self.flight, station.airport
        if not flight.crossings:
            return None
        name = flight.crossings[0]
        runway = self._runway_named(airport, name)
        spoken = say.runway(name.split("/")[0])
        if self.traffic_enabled and runway:
            landing = any(
                traffic.on_final(raw, airport, end, CROSSING_FINAL_NM) is not None for end in (runway.end1, runway.end2)
            )
            if landing or traffic.on_runway(raw, airport, runway.end1):
                why = "landing traffic" if landing else "traffic on the runway"
                return Reply(f"{callsign}, {hello}hold short runway {spoken}, {why}") if asked else None
        flight.crossings.pop(0)
        flight.crossing_given, flight.crossing_refused = name, None
        goal = self._goal_words(faa)
        tail = self._limit() or (f", continue to {goal}" if goal else "")
        expect = {"hold_short": flight.crossings[0]} if flight.crossings else {"crossing": name}
        return Reply(f"{callsign}, {hello}cross runway {spoken}{tail}", expect)

    def _crossing_watch(self, station: Station, raw: dict, callsign: str, faa: bool) -> Reply | None:
        """Unprompted: the crossing clearance for an aircraft stopped at the runway, and a word to one that did not stop."""
        flight = self.flight
        offset = self._offset_nm(station.airport, flight.crossings[0], raw)
        if offset is None:
            return None
        spoken = say.runway(flight.crossings[0].split("/")[0])
        if offset < ON_RUNWAY_NM:
            flight.crossings.pop(0)
            flight.pending = None
            return Reply(f"{callsign}, you were to hold short of runway {spoken}, expedite crossing", event="runway_incursion")
        refused = flight.crossing_refused == flight.crossings[0]
        if offset < HOLDING_SHORT_NM and raw.get("groundspeed_ms", 99.0) < 1.0 and flight.pending is None and not refused:
            return self._crossing(station, raw, callsign, "", faa, asked=False)
        return None

    def _airline(self) -> str | None:
        """The filed airline's ICAO code ('BAW' of BAW123), or None for a registration."""
        plan = self.flight.plan
        filed = plan.callsign.upper() if plan and plan.callsign else ""
        return filed[:3] if say.airline_callsign(filed) else None

    def _airport_names(self, airport: Airport, route, exclude: str | None = None, start=None):
        """The runways a route crosses, named as the airport names them ('4R/22L', said "four right"), not as its
        taxi network may write them ('04R/22L'). `start` is where the aircraft is: a runway between it and where
        the route begins is crossed too, and the runway it is standing on is not."""
        if route is None:
            return None
        renamed = {}
        for name in route.crossings:
            runway = self._runway_named(airport, name)
            renamed[name] = f"{runway.end1}/{runway.end2}" if runway else name
        route.crossings = [renamed[name] for name in route.crossings]
        route.crossing_index = {renamed.get(name, name): index for name, index in route.crossing_index.items()}
        # A runway the scenery's taxi network does not tag as one is crossed all the same where the route goes over it.
        for runway in airport.runways:
            name = f"{runway.end1}/{runway.end2}"
            excluded = exclude is not None and end_key(exclude) in (end_key(runway.end1), end_key(runway.end2))
            if name not in route.crossing_index and not excluded:
                points = [start, *route.points] if start else route.points
                index = self._passes_over(runway, points)
                if index is not None:
                    route.crossing_index[name] = max(index - 1, 0) if start else index
        route.crossings = sorted(route.crossing_index, key=route.crossing_index.get)
        return route

    @staticmethod
    def _passes_over(runway, points: list) -> int | None:
        """Where a route first goes onto or across a runway (not counting where it starts or ends), or None."""
        heading, length = runway.ends()[0][1], runway.length_m / 1852
        first = locate(*points[0], (runway.lat2, runway.lon2), heading) if points else None
        if first and -RUNWAY_END_NM < first.along_nm < length + RUNWAY_END_NM and first.cross_nm < ON_RUNWAY_NM:
            return None  # starting on it: leaving the runway is not crossing it
        before = None
        for index, (lat, lon) in enumerate(points[:-1]):
            position = locate(lat, lon, (runway.lat2, runway.lon2), heading)
            abeam = -RUNWAY_END_NM < position.along_nm < length + RUNWAY_END_NM
            if index > 0 and abeam and (position.cross_nm < ON_RUNWAY_NM or (before and before != position.side)):
                return index
            before = position.side if abeam else None
        return None

    def _reachable_stand(self, network: TaxiNetwork, raw: dict, given, category: str):
        """(stand, route) for the nearest stand of the same kind, that takes this aircraft, that a route reaches."""
        alike = [p for p in network.parking if p.name and p.kind == given.kind and category in p.categories]
        alike.sort(key=lambda p: distance_nm(raw["lat"], raw["lon"], p.lat, p.lon))
        for stand in alike[:REACHABLE_TRIES]:
            route = self._route_to_stand(network, raw, (stand.lat, stand.lon))
            if route is not None:
                return stand, route
        return None

    @staticmethod
    def _route_to_stand(network: TaxiNetwork, raw: dict, stand: tuple[float, float]):
        route = route_to_point(network, raw["lat"], raw["lon"], stand)
        if route:
            route.points.append(stand)  # the last few metres, off the taxiway, are shown on the map too
        return route

    def _route_from(self, airport: Airport, raw: dict):
        """The route to the flight's taxi goal from where the aircraft is now."""
        goal = self.flight.taxi_goal
        network = self.network(airport)
        if goal[0] == "runway":
            route = route_to_runway(network, raw["lat"], raw["lon"], goal[1], self._threshold(airport, goal[1]))
        else:
            route = self._route_to_stand(network, raw, (goal[1], goal[2]))
        return self._airport_names(airport, route, exclude=goal[1] if goal[0] == "runway" else None)

    def _progressive(self, station: Station, raw: dict, callsign: str, hello: str, faa: bool) -> Reply | None:
        """The next part of the taxi route, from where the aircraft is now."""
        flight, airport = self.flight, station.airport
        goal = flight.taxi_goal
        if not goal or "lat" not in raw:
            return None
        route = self._route_from(airport, raw)
        flight.taxi_route, flight.taxi_airport = route, airport.ident
        where = self._goal_words(faa)
        if not route or not route.taxiways:
            return Reply(f"{callsign}, {hello}continue straight ahead to {where}")
        text = f"continue on {say.taxiway(route.taxiways[0])}"
        if len(route.taxiways) > 1:
            turn = route.turns[0] if route.turns else ""
            text += f", then {f'turn {turn} onto' if turn else 'continue onto'} {say.taxiway(route.taxiways[1])}"
        else:
            text += f" to {where}"
        return Reply(f"{callsign}, {hello}{text}{self._limit()}")

    def _ground(self, intent, station, issue, raw, callsign, hello, faa, pressure_tail, on_ground) -> Reply | None:
        airport, flight = station.airport, self.flight
        if not on_ground:
            return None
        arrived = flight.has_been_airborne
        if intent.intent == "request_pushback" and not arrived:
            return self._pushback(callsign, hello, faa, pressure_tail)
        if intent.intent == "runway_vacated" or (intent.intent == "request_taxi" and arrived):
            return self._taxi_in(station, raw, callsign, hello, faa)
        if intent.intent == "request_taxi":
            return self._taxi_out(station, issue, raw, callsign, hello, faa, pressure_tail)
        if intent.intent == "ready_for_departure":
            target = self._handoff(airport, "tower", to_aircraft=True)
            if target:
                return Reply(f"{callsign}, {hello}contact {target[0]} {target[1]}", {"frequency": target[2]})
        return None

    def _backtrack(self, airport: Airport, runway: str, raw: dict, callsign: str, hello: str, faa: bool) -> Reply:
        """Onto the runway where the taxiways reach it, and along it to the threshold: cleared only with nobody on
        final for long enough to do it, and the takeoff clearance once the aircraft has turned round (`tick`)."""
        flight = self.flight
        flight.runway = runway
        why = self._backtrack_blocked(airport, runway, raw)
        if why:
            flight.waiting_takeoff = True
            return Reply(f"{callsign}, {hello}hold short runway {say.runway(runway)}, {why}", {"runway": runway})
        flight.waiting_takeoff, flight.backtracking = False, True
        spoken = say.runway(runway)
        words = f"runway {spoken}, back-taxi, line up and wait" if faa else f"backtrack runway {spoken}, line up and wait"
        return Reply(f"{callsign}, {hello}{words}", {"runway": runway})

    def _exit_behind(self, airport: Airport, raw: dict) -> str | None:
        """After landing, when no taxiway leaves the runway ahead of the aircraft: the name of the nearest one behind
        it ('' when it has none). None when there is a way off ahead, or nothing is known about the taxiways."""
        runway, network = self.flight.runway, self.network(airport)
        if not runway or "lat" not in raw or not network.usable:
            return None
        name = next((n for n in network.runway_nodes if end_key(runway) in map(end_key, n.split("/"))), None)
        course = next((heading for rw in airport.runways for ident, heading in rw.ends() if ident == runway), None)
        if name is None or course is None:
            return None
        threshold = self._threshold(airport, runway)
        here = locate(raw["lat"], raw["lon"], threshold, course).along_nm
        exits = {node: locate(*network.nodes[node], threshold, course).along_nm for node in network.runway_nodes[name]}
        exits = {node: along for node, along in exits.items() if node in network.taxi_nodes()}
        # `along` is measured out from the threshold as on final: past it, down the runway, it falls below zero.
        if not exits or any(along < here - EXIT_AHEAD_NM for along in exits.values()):
            return None
        last = min(exits, key=exits.get)  # the nearest behind
        return next((edge.name for edge in network.both_ways.get(last, []) if not edge.runway and edge.name), "")

    def _backtrack_blocked(self, airport: Airport, runway: str, raw: dict) -> str:
        """Why a backtrack cannot be cleared now ('' when it can): it holds the runway for as long as it takes to
        taxi to the threshold, so traffic on final for either end is kept clear by that much more (at 15 knots on
        the ground against 140 in the air, ten miles of final for each mile of runway)."""
        if not self.traffic_enabled:
            return ""
        miles = distance_nm(raw["lat"], raw["lon"], *self._threshold(airport, runway)) if "lat" in raw else 0.0
        reach = max(BACKTRACK_BLOCKS_NM, traffic.FINAL_BLOCKS_NM + 10 * miles)
        for end in (runway, self._other_end(airport, runway)):
            final = traffic.on_final(raw, airport, end, reach) if end else None
            if final is not None:
                return f"traffic on a {traffic.miles_word(final)} mile final"
        return "traffic on the runway" if traffic.on_runway(raw, airport, runway) else ""

    def _departure_blocked(self, airport: Airport, runway: str, raw: dict) -> str:
        """Why an aircraft lined up cannot be cleared for takeoff now ('' when it can)."""
        if not self.traffic_enabled:
            return ""
        final = traffic.on_final(raw, airport, runway, traffic.FINAL_BLOCKS_NM)
        if final is not None:
            return f"traffic on a {traffic.miles_word(final)} mile final"
        return "traffic on the runway" if traffic.on_runway(raw, airport, runway) else ""

    @staticmethod
    def _other_end(airport: Airport, runway: str) -> str | None:
        for rw in airport.runways:
            if runway in (rw.end1, rw.end2):
                return rw.end2 if runway == rw.end1 else rw.end1
        return None

    def _lined_up(self, airport: Airport, runway: str, raw: dict, anywhere: bool = False) -> bool:
        """On the centreline of `runway`, pointing down it and stopped: at its threshold (a turn pad or a long
        aircraft may leave it a little way in), or with `anywhere`, wherever the pilot has chosen to line up."""
        if "lat" not in raw:
            return False
        course = next((heading for rw in airport.runways for ident, heading in rw.ends() if ident == runway), None)
        if course is None:
            return False
        position = locate(raw["lat"], raw["lon"], self._threshold(airport, runway), course)
        heading = raw.get("heading_true", course)
        on_it = position.along_nm < LINED_UP_NM if anywhere else abs(position.along_nm) < LINED_UP_NM
        return (
            on_it
            and position.cross_nm < ON_RUNWAY_NM
            and abs((heading - course + 180) % 360 - 180) < LINED_UP_DEG
            and raw.get("groundspeed_ms", 0.0) < 3.0
        )

    @staticmethod
    def _pushback(callsign: str, hello: str, faa: bool, pressure_tail: str) -> Reply:
        approved = "push back and start approved" if not faa else "push back approved"
        return Reply(f"{callsign}, {hello}{approved}{pressure_tail}")

    # ---- tower --------------------------------------------------------------

    def _tower(
        self, intent, station, issue, raw, variation, callsign, hello, faa, pressure_tail, on_ground, kinds
    ) -> Reply | None:
        airport, flight = station.airport, self.flight
        wind = self._wind(issue, variation, faa)
        wind_tail = f", {wind}" if wind else ""

        if on_ground:
            arrived = flight.has_been_airborne and flight.phase in (Phase.LANDED, Phase.TAXI_IN, Phase.LANDING_CLEARED)
            if intent.intent == "runway_vacated" or (intent.intent == "request_taxi" and arrived):
                target = self._handoff(airport, "ground", to_aircraft=True)
                if target:
                    return Reply(f"{callsign}, {hello}contact {target[0]} {target[1]}", {"frequency": target[2]})
                return self._taxi_in(station, raw, callsign, hello, faa)
            if intent.intent in ("request_pushback", "request_taxi") and "ground" in kinds:
                target = self._handoff(airport, "ground", to_aircraft=True)
                return Reply(f"{callsign}, {hello}contact {target[0]} {target[1]}", {"frequency": target[2]})
            if intent.intent == "request_pushback" and not flight.has_been_airborne:
                # No ground controller: the tower works the apron too, and offers this call.
                return self._pushback(callsign, hello, faa, pressure_tail)
            if intent.intent == "request_taxi":
                return self._taxi_out(station, issue, raw, callsign, hello, faa, pressure_tail)
            if intent.intent == "ready_for_departure":
                runway = flight.runway or self._departure_runway(airport, issue, raw)
                if not runway:
                    return None
                if flight.backtracking:
                    if not self._lined_up(airport, runway, raw, anywhere=True):
                        return Reply(f"{callsign}, {hello}roger, report lined up")
                    # Lined up on the runway, where the pilot chose to: the takeoff clearance, when nothing is in the way.
                    why = self._departure_blocked(airport, runway, raw)
                    if why:
                        flight.waiting_takeoff = True
                        return Reply(f"{callsign}, {hello}hold position, {why}")
                elif flight.backtrack:
                    near = distance_nm(raw["lat"], raw["lon"], *self._threshold(airport, runway)) * 1852 if "lat" in raw else 1e9
                    if near > FULL_LENGTH_M:
                        return self._backtrack(airport, runway, raw, callsign, hello, faa)
                    flight.backtrack = False  # at the threshold already, whatever the taxi network shows
                if self.traffic_enabled:
                    final = traffic.on_final(raw, airport, runway, traffic.FINAL_BLOCKS_NM)
                    if final is not None or traffic.on_runway(raw, airport, runway):
                        flight.runway, flight.waiting_takeoff = runway, True
                        why = (
                            f"traffic on a {traffic.miles_word(final)} mile final"
                            if final is not None
                            else "traffic on the runway"
                        )
                        return Reply(f"{callsign}, {hello}hold short runway {say.runway(runway)}, {why}", {"runway": runway})
                flight.runway, flight.phase, flight.waiting_takeoff = runway, Phase.TAKEOFF_CLEARED, False
                flight.backtrack = flight.backtracking = False
                after = ", report downwind" if flight.circuits else ""
                return Reply(
                    f"{callsign}, {hello}runway {say.runway(runway)}, cleared for takeoff{wind_tail}{after}", {"runway": runway}
                )
            return None

        # Airborne.
        if intent.intent == "request_transit":
            return vfr.transit(self, intent, station, issue, raw, callsign, hello, faa)
        if intent.intent == "report_leaving":
            left = vfr.leaving(self, station, raw, callsign, faa, asked=True)
            if left:
                return left
            if flight.rules == "IFR" and airport.ident == flight.origin_ident and not flight.told_departure:
                # An instrument departure asking to change: the hand-off to departure that was coming anyway.
                target = ifr.after_takeoff(self, airport, raw)
                if target:
                    flight.told_departure = True
                    return Reply(f"{callsign}, {hello}contact {target[0]} {target[1]}", {"frequency": target[2]})
            return answers.leave_frequency(self, station, raw, callsign, hello, faa)
        runway = self._landing_runway(airport, issue, raw, intent)
        if not runway:
            return None
        spoken = say.runway(runway)
        flight.low_approach = flight.low_approach or intent.intent == "request_low_approach"
        if intent.intent == "request_special_vfr":
            if flight.transit == airport.ident:
                return vfr.transit(self, intent, station, issue, raw, callsign, hello, faa)
            return vfr.inbound(self, intent, station, issue, raw, callsign, hello, faa, runway, pressure_tail)
        flight.touch_and_go = flight.touch_and_go or intent.touch_and_go or intent.intent == "request_touch_and_go"
        if intent.intent == "going_around":
            flight.phase, flight.go_around_told = Phase.AIRBORNE, False
            if flight.rules == "IFR" or flight.arrival:
                return ifr.go_around(self, station, callsign)
            return Reply(f"{callsign}, roger, rejoin the circuit runway {spoken}, report downwind", {"runway": runway})
        if intent.intent == "inbound" and intent.position != "final" and not flight.emergency:
            flight.runway = runway
            visual = (intent.flight_rules or flight.rules) != "IFR"
            if visual:
                return vfr.inbound(self, intent, station, issue, raw, callsign, hello, faa, runway, pressure_tail)
            return Reply(f"{callsign}, {hello}continue approach runway {spoken}{pressure_tail}", {"runway": runway})
        if intent.intent == "position_report" and intent.position in ("downwind", "base"):
            flight.runway = runway
            ahead = traffic.on_final(raw, airport, runway) if self.traffic_enabled else None
            if ahead is not None:
                follow = f"number two, follow the traffic on a {traffic.miles_word(ahead)} mile final"
                return Reply(f"{callsign}, {hello}{follow}, report final runway {spoken}", {"runway": runway})
            return Reply(f"{callsign}, {hello}number one, report final runway {spoken}", {"runway": runway})
        landing_calls = ("position_report", "inbound", "request_touch_and_go", "established", "check_in", "request_low_approach")
        if intent.intent in landing_calls:
            # An instrument arrival still well out is told to continue; the landing clearance follows.
            miles = distance_nm(raw["lat"], raw["lon"], *self._threshold(airport, runway)) if "lat" in raw else 0.0
            occupied = self.traffic_enabled and traffic.on_runway(raw, airport, runway)
            ahead = traffic.on_final(raw, airport, runway, max(miles - 0.5, 0.0)) if self.traffic_enabled else None
            far = miles > CONTINUE_BEYOND_NM and (flight.rules == "IFR" or flight.arrival) and not flight.touch_and_go
            if (far and not flight.emergency) or occupied or ahead is not None:
                flight.runway, flight.awaiting_landing = runway, True
                number = "number two, " if ahead is not None else ""
                reason = ", traffic on the runway" if occupied else wind_tail
                return Reply(f"{callsign}, {hello}{number}continue approach runway {spoken}{reason}", {"runway": runway})
            flight.awaiting_landing = False
            flight.runway, flight.phase = runway, Phase.LANDING_CLEARED
            cleared = "cleared touch and go" if flight.touch_and_go else "cleared to land"
            if flight.low_approach:
                cleared, flight.low_approach, flight.phase = "cleared low approach", False, Phase.AIRBORNE
            services = ""
            if flight.emergency and not flight.services_told:
                flight.services_told, services = True, ", emergency services standing by"
            return Reply(f"{callsign}, {hello}runway {spoken}, {cleared}{wind_tail}{services}", {"runway": runway})
        return None

    def _landing_runway(self, airport: Airport, issue: Issue | None, raw: dict, intent: Intent) -> str | None:
        """The runway the pilot asked for; the one the approach brought the flight to; the one already given or named;
        the plan's when it is in use; else the landing runway whose threshold is nearest, of those long enough."""
        active = self.active_runways(airport, issue)
        if not active:
            return None
        if self.requested_runway(airport, issue):
            return self.requested_runway(airport, issue)
        arrival = self.flight.arrival
        if (
            arrival is not None
            and arrival.airport == airport.ident
            and ifr._tailwind_kt(airport, arrival.runway, issue) <= ifr.MAX_TAILWIND_KT
        ):
            # The runway approach brought the flight to, even one not in use: straight in to the other end with
            # little tailwind, where the runway in use has no approach. Not once the wind has turned against it.
            return arrival.runway
        if self.flight.runway in active:
            return self.flight.runway
        if intent.runway in active:
            return intent.runway
        planned = self._planned_runway(airport, "arrival")
        if planned in (split_runways(active)[0] if isinstance(active, RunwaysInUse) else active):
            return planned
        landing = self._long_enough(airport, split_runways(active)[0], raw)
        if "lat" not in raw:
            return landing[0]
        return min(landing, key=lambda ident: distance_nm(raw["lat"], raw["lon"], *self._threshold(airport, ident)))

    # ---- sim-driven state and unprompted calls ------------------------------

    def _observe(self, raw: dict) -> None:
        """Follow the flight from the sim: takeoff, landing, and where it was parked."""
        flight = self.flight
        self.last_raw = raw
        if "on_ground" not in raw:
            return
        on_ground = bool(raw["on_ground"])
        speed = raw.get("groundspeed_ms", 0.0)
        if "lat" in raw:
            # On the ground in one look and somewhere else on the ground (or far away in the air) in the next:
            # the pilot moved the aircraft in the sim. That is a new flight.
            before, flight.seen_at = flight.seen_at, (raw["lat"], raw["lon"], on_ground)
            if before and before[2]:
                moved = distance_nm(before[0], before[1], raw["lat"], raw["lon"])
                if moved > (REPOSITION_NM if on_ground else REPOSITION_AIRBORNE_NM):
                    self.reset()
                    flight = self.flight
                    flight.seen_at = (raw["lat"], raw["lon"], on_ground)
        if on_ground and flight.origin is None and speed < 1 and "lat" in raw and not flight.has_been_airborne:
            flight.origin = (raw["lat"], raw["lon"])
        if not on_ground and raw.get("agl_m", 0.0) > 15:
            if flight.phase != Phase.LANDING_CLEARED and flight.phase != Phase.AIRBORNE:
                # New airborne segment: the departure runway no longer applies.
                flight.phase, flight.runway, flight.pending = Phase.AIRBORNE, None, None
                if flight.has_been_airborne:
                    flight.arrival = None  # off again after landing: the last arrival is over
                flight.crossings, flight.taxi_goal, flight.taxi_route = [], None, None
                flight.heading_assigned = None
                if flight.requested_runway and flight.requested_runway[2] == "departure":
                    flight.requested_runway = None  # used
                flight.told_leaving = False
                flight.told_vacate = False
            flight.has_been_airborne = True
        elif on_ground and flight.has_been_airborne and flight.phase in (Phase.AIRBORNE, Phase.LANDING_CLEARED):
            if not flight.touch_and_go:
                flight.phase, flight.heading_assigned = Phase.LANDED, None
                if flight.requested_runway and flight.requested_runway[2] == "arrival":
                    flight.requested_runway = None  # used

    def tick(self, station: Station, issue: Issue | None, raw: dict) -> Reply | None:
        """Calls the controller makes without being asked. Call about once a second."""
        with self.lock:
            reply = self._tick(station, issue, raw)
            if reply is not None and self.voiced and self.flight.pending is reply:
                # Decided, and seconds away from being heard: nothing to read back or to answer until `on_air`.
                self.flight.unsaid = reply
            elif reply is not None and not reply.correction:
                questions.record(self, station.key, reply)
            return reply

    def on_air(self, reply: Reply) -> None:
        """The controller's voice has started on this call: the wait for its readback starts now."""
        with self.lock:
            flight = self.flight
            if flight.pending is reply:
                flight.pending_at = self.now()
                if flight.unsaid is reply and not reply.correction:
                    questions.record(self, flight.pending_key, reply)
            if flight.unsaid is reply:
                flight.unsaid = None

    def not_yet_said(self) -> bool:
        """Whether the instruction waiting for a readback has not been heard yet: its voice is still being made, or
        the frequency was taken when it was ready."""
        flight = self.flight
        return flight.pending is not None and flight.pending is flight.unsaid

    def _tick(self, station: Station, issue: Issue | None, raw: dict) -> Reply | None:
        with self.lock:
            self._observe(raw)
            flight, airport = self.flight, station.airport
            if not raw.get("tail_number"):
                return None
            from .xpbridge import magnetic_variation

            callsign = self._callsign(raw)
            key = station.key
            reply = None
            if flight.away_from == key:
                return None  # the pilot has stepped away from this frequency and will call back
            # A new ATIS: tell an aircraft on frequency, as a controller would broadcast it.
            if airport is not None and issue is not None:
                heard = flight.atis_seen.get(airport.ident)
                flight.atis_seen[airport.ident] = issue.letter
                if heard and heard != issue.letter and key in flight.contacted:
                    pressure = self._pressure(issue, uses_faa_style(airport.ident))[0]
                    return Reply(f"{callsign}, information {say.letter(issue.letter)} is now current, {pressure}")
            # An instruction nobody read back is said once more.
            waiting = self.waiting_on(station, raw)
            if waiting and not flight.reminded and self.now() - flight.pending_at > READBACK_WAIT_S:
                flight.reminded, flight.pending_at = True, self.now()  # and the pilot has as long again to answer
                body = self.instruction_body(flight.pending.text, station.callsign)
                return Reply(
                    f"{callsign}, {station.callsign}, I say again, {body}", flight.pending.expect, correction=True,
                    event="readback_missed",
                )  # fmt: skip
            if station.kind in ifr.RADAR_KINDS:
                if waiting and self.now() - flight.pending_at <= READBACK_WAIT_S:
                    # One instruction at a time: the next waits for the readback of the last, or a new one would
                    # take its place and that readback would never be asked for. Not for ever: once the last has
                    # been said again and still not answered, the controller carries on. What needs no readback
                    # (a level bust, a low altitude alert, traffic) is said at once.
                    reply = ifr.alerts(self, station, raw, callsign)
                    if reply:
                        flight.last_said[key] = reply.text
                    return reply
                if self.now() < flight.handoffs_held_until:
                    return None  # the pilot could not take the last hand-off: it is given again a little later
                reply = ifr.tick(self, station, issue, raw, magnetic_variation(raw), callsign)
                if reply:
                    flight.last_said[key] = reply.text
                    if reply.expect:
                        self._await_readback(reply, key)
                return reply
            airborne = not bool(raw.get("on_ground", True))
            if station.kind in ("ground", "tower") and flight.crossings and not airborne and key in flight.contacted:
                reply = self._crossing_watch(station, raw, callsign, uses_faa_style(airport.ident))
                if reply:
                    flight.last_said[key] = reply.text
                    if reply.expect:
                        self._await_readback(reply, key)
                    return reply
            if station.kind != "tower":
                return None
            miles = (
                distance_nm(raw["lat"], raw["lon"], *self._threshold(airport, flight.runway))
                if flight.runway and "lat" in raw
                else None
            )
            wind = self._wind(issue, magnetic_variation(raw), uses_faa_style(airport.ident))
            wind_tail = f", {wind}" if wind else ""
            if vfr.service(station) is None and key in flight.contacted:
                gone = vfr.tick_tower(self, station, raw, callsign, uses_faa_style(airport.ident))
                if gone:
                    flight.last_said[key] = gone.text
                    return gone
            occupied = self.traffic_enabled and flight.runway is not None and traffic.on_runway(raw, airport, flight.runway)
            if (
                flight.phase == Phase.LANDING_CLEARED
                and airborne
                and occupied
                and miles is not None
                and miles <= LATE_GO_AROUND_NM
                and not flight.go_around_told
            ):
                # Cleared to land, and now something is on the runway: the clearance does not stand.
                flight.go_around_told, flight.phase, flight.pending = True, Phase.AIRBORNE, None
                reply = Reply(f"{callsign}, go around, I say again, go around, traffic on the runway", event="go_around")
            elif flight.phase == Phase.TAKEOFF_CLEARED and not airborne and occupied and raw.get("groundspeed_ms", 0.0) < 15:
                flight.phase, flight.waiting_takeoff, flight.pending = Phase.TAXI_OUT, True, None
                reply = Reply(
                    f"{callsign}, hold position, cancel takeoff clearance, I say again, cancel takeoff clearance, "
                    "traffic on the runway",
                    event="takeoff_cancelled",
                )
            elif flight.awaiting_landing and airborne and miles is not None and miles <= CLEAR_TO_LAND_NM:
                blocked = self.traffic_enabled and (
                    traffic.on_runway(raw, airport, flight.runway)
                    or traffic.on_final(raw, airport, flight.runway, max(miles - 0.5, 0.0)) is not None
                )
                if not blocked:
                    flight.awaiting_landing, flight.phase = False, Phase.LANDING_CLEARED
                    reply = Reply(
                        f"{callsign}, runway {say.runway(flight.runway)}, cleared to land{wind_tail}", {"runway": flight.runway}
                    )
                elif miles <= GO_AROUND_NM and not flight.go_around_told:
                    flight.awaiting_landing, flight.go_around_told = False, True
                    reply = Reply(f"{callsign}, go around, I say again, go around, traffic on the runway", event="go_around")
            elif flight.backtracking and not airborne and flight.runway and self._lined_up(airport, flight.runway, raw):
                # Backtracked and turned round at the threshold: the takeoff clearance, as for any departure.
                if not self._departure_blocked(airport, flight.runway, raw):
                    flight.backtrack = flight.backtracking = flight.waiting_takeoff = False
                    flight.phase = Phase.TAKEOFF_CLEARED
                    reply = Reply(
                        f"{callsign}, runway {say.runway(flight.runway)}, cleared for takeoff{wind_tail}",
                        {"runway": flight.runway},
                    )
            elif flight.waiting_takeoff and not airborne and flight.runway and flight.backtrack and not flight.backtracking:
                if not self._backtrack_blocked(airport, flight.runway, raw):
                    reply = self._backtrack(airport, flight.runway, raw, callsign, "", uses_faa_style(airport.ident))
            elif flight.waiting_takeoff and not airborne and flight.runway:
                clear = traffic.on_final(raw, airport, flight.runway, traffic.FINAL_BLOCKS_NM) is None and not traffic.on_runway(
                    raw, airport, flight.runway
                )
                if clear:
                    flight.waiting_takeoff, flight.phase = False, Phase.TAKEOFF_CLEARED
                    reply = Reply(
                        f"{callsign}, runway {say.runway(flight.runway)}, cleared for takeoff{wind_tail}",
                        {"runway": flight.runway},
                    )
            elif (
                flight.phase == Phase.AIRBORNE
                and flight.rules == "IFR"
                and not flight.told_departure
                and airport.ident == flight.origin_ident  # a departure: not an arrival at another tower
                and raw.get("agl_m", 0.0) > 300
                and distance_nm(raw["lat"], raw["lon"], airport.lat, airport.lon) < RELEASE_DEPARTURE_NM
            ):
                # Where no centre covers the airport itself, the tower keeps looking as the flight climbs out, and
                # hands it over once it is in a centre's airspace, rather than letting it fly out of range unheard.
                target = ifr.after_takeoff(self, airport, raw)
                if target:
                    flight.told_departure = True
                    reply = Reply(f"{callsign}, contact {target[0]} {target[1]}", {"frequency": target[2]})
            elif flight.phase == Phase.LANDED and not flight.told_vacate and raw.get("groundspeed_ms", 99.0) < 20:
                flight.told_vacate = True
                target = self._handoff(airport, "ground", to_aircraft=True)
                tail = f", when vacated contact {target[0]} {target[1]}" if target else " and taxi to parking"
                behind = self._exit_behind(airport, raw)
                expect = {"frequency": target[2]} if target else {}
                if behind is None:
                    vacate = "vacate at the next available exit"
                else:
                    # No way off the runway ahead: back along it to the last taxiway passed, read back.
                    spoken, faa = say.runway(flight.runway), uses_faa_style(airport.ident)
                    where = f"via {say.taxiway(behind)}" if behind else "at the first taxiway"
                    vacate = f"back-taxi runway {spoken}, exit {where}" if faa else f"backtrack runway {spoken}, vacate {where}"
                    expect["runway"] = flight.runway
                    final = traffic.on_final(raw, airport, flight.runway, BACKTRACK_BLOCKS_NM) if self.traffic_enabled else None
                    if final is not None:
                        vacate += f", expedite, traffic on a {traffic.miles_word(final)} mile final"
                reply = Reply(f"{callsign}, {vacate}{tail}", expect)
            if reply:
                flight.last_said[key] = reply.text
                if reply.expect:
                    self._await_readback(reply, key)
            return reply

    def opening(self, station: Station, raw: dict) -> str:
        """How a call to this station begins: its name on first contact, then the callsign alone."""
        with self.lock:
            callsign = self._callsign(raw)
            return f"{callsign}, " if station.key in self.flight.contacted else f"{station.callsign}, {callsign}, "

    def suggestions(self, station: Station, issue: Issue | None, raw: dict) -> list[dict]:
        """Calls the pilot could make right now, for the app's click-to-transmit buttons.

        Each has a short label, the full transmission, and the intent it stands for, so a
        clicked call skips speech recognition and the language model. `needs` names a value
        the app must ask for and put in place of {destination}. The answers to a question the
        controller asked come first and carry `answers`; `primary` marks the call most expected now.
        """
        with self.lock:
            items = self._suggestions(station, issue, raw)
            return questions.arrange(self, station, issue, raw, items, self.opening(station, raw))

    def _suggestions(self, station: Station, issue: Issue | None, raw: dict) -> list[dict]:
        with self.lock:
            flight, airport = self.flight, station.airport
            callsign = self._callsign(raw)
            key = station.key
            opening = f"{callsign}, " if key in flight.contacted else f"{station.callsign}, {callsign}, "
            on_ground = bool(raw.get("on_ground", True))
            kinds = {freq.kind for freq in airport.freqs} if airport else set()
            information = f"information {say.letter(issue.letter)}, " if issue else ""
            items: list[dict] = []
            informing = vfr.service(station) if airport else None
            if informing in ("ctaf", "unicom"):
                return vfr.ctaf_suggestions(self, station, issue, raw, callsign, informing)

            def add(label: str, body: str, intent: str, **fields) -> None:
                needs = fields.pop("needs", None)
                item = {"label": label, "text": f"{opening}{body}", "intent": {"intent": intent, **fields}}
                if needs:
                    item["needs"] = needs
                items.append(item)

            if self.waiting_on(station, raw):  # read back to the controller who gave it
                said = self.instruction_body(flight.pending.text, station.callsign)
                said = re.sub(r", information [\w-]+ is current", "", said)  # the pilot does not read that back
                items.append(
                    {
                        "label": "Read back",
                        "text": f"{said[0].upper()}{said[1:]}, {callsign}",
                        "intent": self._readback_intent(flight.pending.expect),
                        "primary": True,
                    }
                )

            if informing == "afis":
                vfr.afis_suggestions(self, station, issue, raw, add)
                add("Wind and pressure", "request the wind and Q N H", "request_weather")
                add("Radio check", "radio check", "radio_check")
                return items
            if station.kind in ifr.RADAR_KINDS:
                ifr.suggestions(self, station, raw, add, callsign)
                if flight.pending:
                    add("Unable", "unable", "unable")  # the controller cancels the instruction or gives another
                if flight.last_said.get(key):
                    add("Say again", "say again", "say_again")
                add("Radio check", "radio check", "radio_check")
                return items
            arrived = flight.has_been_airborne and on_ground
            if on_ground and not arrived:
                # Offered on every ground-side frequency: the right station gives the clearance,
                # any other one says who to call (and the app offers to tune it).
                if station.kind in ("delivery", "ground", "tower") and not flight.squawk:
                    if flight.plan and flight.plan.destination:
                        add("Request IFR clearance", f"{information}request I F R clearance to {flight.plan.destination}",
                            "request_clearance", flight_rules="IFR")  # fmt: skip
                    elif flight.destination:
                        add("Request IFR clearance", f"{information}request I F R clearance to {flight.destination}",
                            "request_clearance", flight_rules="IFR", destination=flight.destination)  # fmt: skip
                    else:
                        add("Request IFR clearance…", f"{information}request I F R clearance to {{destination}}",
                            "request_clearance", flight_rules="IFR", needs="destination")  # fmt: skip
                    add("Request VFR departure", f"{information}request V F R departure", "request_clearance", flight_rules="VFR")
                handles_ground = station.kind == "ground" or (station.kind == "tower" and "ground" not in kinds)
                back_at_stand = flight.phase == Phase.TAXI_IN  # returned to the stand before departing
                if handles_ground and (flight.phase == Phase.PARKED or back_at_stand):
                    add("Request push and start", "request push and start", "request_pushback")
                    add("Request taxi", "request taxi", "request_taxi")
                if (handles_ground and flight.phase == Phase.TAXI_OUT) or station.kind == "tower":
                    holding = (
                        f"holding short runway {say.runway(flight.runway)}, " if flight.runway and station.kind == "tower" else ""
                    )
                    if flight.backtracking and station.kind == "tower":
                        add("Lined up", f"lined up runway {say.runway(flight.runway)}", "ready_for_departure")
                    else:
                        add("Ready for departure", f"{holding}ready for departure", "ready_for_departure")
            elif arrived:
                if station.kind in ("tower", "ground"):
                    add("Runway vacated", "runway vacated", "runway_vacated")
            if on_ground and station.kind in ("tower", "ground"):
                if flight.crossings:
                    short = say.runway(flight.crossings[0].split("/")[0])
                    add("Holding short", f"holding short runway {short}, request crossing", "request_crossing")
                if flight.taxi_goal and flight.phase in (Phase.TAXI_OUT, Phase.TAXI_IN):
                    add("Progressive taxi", "request progressive taxi", "request_progressive")
                handles_ground = station.kind == "ground" or (station.kind == "tower" and "ground" not in kinds)
                if handles_ground and flight.phase == Phase.TAXI_OUT and not flight.has_been_airborne:
                    faa = uses_faa_style(airport.ident)
                    add(f"Return to {'gate' if faa else 'stand'}", f"request return to {'the gate' if faa else 'stand'}",
                        "request_return")  # fmt: skip
            elif station.kind == "tower":
                runway = f" runway {say.runway(flight.runway)}" if flight.runway else ""
                if flight.arrival and flight.arrival.stage == "tower":
                    add("Check in", f"established {flight.arrival.approach}{runway}", "established")
                add("Inbound for landing", f"{information}inbound for landing", "inbound")
                add("Downwind", f"downwind{runway}", "position_report", position="downwind")
                add("Final", f"final{runway}", "position_report", position="final")
                add("Touch and go", "request touch and go", "request_touch_and_go", touch_and_go=True)
                add("Going around", "going around", "going_around")
                add("Wind check", "request wind check", "request_information", item="wind")

            if station.kind == "tower":
                vfr.tower_suggestions(self, station, issue, raw, add)
            if flight.pending:
                add("Unable", "unable", "unable")  # the controller cancels the instruction or gives another
            if flight.last_said.get(key):
                add("Say again", "say again", "say_again")
            add("Radio check", "radio check", "radio_check")
            return items

    def _level_label(self, flight) -> str | None:
        """The cleared level as the controller means it: a flight level above the transition altitude, else feet."""
        if flight.cleared_ft is None:
            return None
        near = flight.arrival.airport if flight.arrival else flight.origin_ident
        if flight.descent_given and flight.plan and flight.plan.destination:
            near = flight.plan.destination
        faa = uses_faa_style(near or "")
        procedures = self.procedures(near) if near else None
        transition = 18000 if faa else ((procedures.transition_altitude_ft if procedures else None) or 6000)
        if flight.cleared_ft > transition:
            return f"FL{flight.cleared_ft // 100:03d}"
        return f"{flight.cleared_ft:,} ft"

    # ---- the airport map ----------------------------------------------------

    def chart(self, ident: str) -> dict | None:
        """The map of an airport: what does not change while taxiing."""
        with self.lock:
            if ident not in self._charts:
                airport = self.db.airport(ident)
                if airport is None:
                    return None
                self._charts[ident] = chart.build_chart(airport, self.db.record_lines(airport), self.network(airport))
            return self._charts[ident]

    def _airport_under(self, lat: float, lon: float) -> Airport | None:
        """The airport the aircraft is standing on: the one whose runway is nearest, not whose reference point is."""
        cached = self._map_airport
        if cached and distance_nm(lat, lon, cached[0], cached[1]) < 0.3:
            return cached[2]
        best, best_nm = None, 99.0
        for _, airport in self.db.nearest(lat, lon, 5, 6):
            for runway in airport.runways:
                middle = ((runway.lat1 + runway.lat2) / 2, (runway.lon1 + runway.lon2) / 2)
                for point in ((runway.lat1, runway.lon1), (runway.lat2, runway.lon2), middle):
                    gap = distance_nm(lat, lon, *point)
                    if gap < best_nm:
                        best, best_nm = airport, gap
        self._map_airport = (lat, lon, best)
        return best

    def taxi_map(self, raw: dict) -> dict | None:
        """Where the aircraft is on the airport's map, and the taxi route it was given: what is still ahead,
        cleared as far as the next runway it must hold short of."""
        if "lat" not in raw or not raw.get("on_ground"):
            return None
        with self.lock:
            airport = self._airport_under(raw["lat"], raw["lon"])
            if airport is None:
                return None
            origin = (airport.lat, airport.lon)
            here = chart.project(origin, raw["lat"], raw["lon"])
            view: dict = {"ident": airport.ident, "position": list(here), "heading": round(raw.get("heading_true", 0.0)) % 360}
            flight = self.flight
            taxiing = flight.phase in (Phase.TAXI_OUT, Phase.TAXI_IN) and flight.taxi_goal
            if not taxiing or flight.taxi_airport != airport.ident:
                return view
            route = flight.taxi_route
            path = [chart.project(origin, *point) for point in route.points] if route else []
            moved = self._rerouted_at is None or distance_nm(raw["lat"], raw["lon"], *self._rerouted_at) * 1852 > 50
            if moved and (not path or chart.next_point(path, here)[1] > chart.OFF_ROUTE_M):
                # Off the route that was given (a wrong turn, or pushed back somewhere else): show the way from here.
                self._rerouted_at = (raw["lat"], raw["lon"])
                route = self._route_from(airport, raw)
                if route:
                    flight.taxi_route = route
                    path = [chart.project(origin, *point) for point in route.points]
            if not path:
                return view
            hold = route.crossing_index.get(flight.crossings[0]) if flight.crossings else None
            cleared, beyond, stop = chart.split_route(path, here, hold)
            goal = flight.taxi_goal
            if goal[0] == "runway":
                label = f"Holding point {goal[1]}"
            else:
                label = f"Stand {flight.stand.name}" if flight.stand and flight.stand.name else "Parking"
            view["route"] = {
                "cleared": [list(point) for point in cleared],
                "beyond": [list(point) for point in beyond],
                "goal": {"label": label, "at": list(path[-1])},
            }
            if stop:
                view["route"]["hold"] = {"runway": flight.crossings[0].split("/")[0], "at": list(stop)}
            return view

    def summary(self) -> dict:
        """What the engine knows, for the app and for the free-form fallback's context."""
        flight = self.flight
        facts = {
            "phase": flight.phase.value,
            "flight_rules": flight.rules,
            "destination": flight.destination,
            "assigned_squawk": flight.squawk,
            "assigned_runway": flight.runway,
            "sid": flight.sid,
            "star": flight.star,
            "cleared_altitude_ft": flight.cleared_ft,
            "approach": f"{flight.arrival.approach} runway {flight.arrival.approach_runway or flight.arrival.runway}"
            if flight.arrival
            else None,
            "heading": flight.arrival.heading
            if flight.arrival and flight.arrival.stage in ("downwind", "base", "intercept")
            else None,
            "speed_kt": flight.arrival.speed_kt if flight.arrival else None,
            "direct_to": flight.direct_to,
            "emergency": flight.emergency,
            "next_frequency": flight.next_frequency,
            "holding": flight.arrival.hold.fix if flight.arrival and flight.arrival.hold else None,
            "service": flight.service if flight.following else None,
            "cleared_level": self._level_label(flight),
            "stand": flight.stand.name if flight.stand and flight.stand.name else None,
            "hold_short": flight.crossings[0].split("/")[0] if flight.crossings else None,
            "question": flight.question.text if flight.question else None,
        }
        return {key: value for key, value in facts.items() if value}
