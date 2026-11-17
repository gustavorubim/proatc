"""The controller's decisions: a rules engine for delivery, ground and tower.

The language model turns speech into an Intent; this module decides what the controller
says, from the real airport data, the weather and where the flight is in its sequence.
Every instruction is built from templates, so runways, routes, frequencies and pressure
are never invented. `handle` returns None for requests it does not cover, and the caller
may then fall back to a free-form reply.
"""

from __future__ import annotations

import random
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from . import ifr, traffic, vfr
from . import phraseology as say
from .atis import Issue, runways_in_use, split_runways, uses_faa_style
from .intents import Intent
from .navdata import Airport, AirportDB, Station, distance_nm
from .taxi import (
    TaxiNetwork,
    aircraft_category,
    assign_stand,
    nearest_parking,
    parse_taxi,
    route_to_point,
    route_to_runway,
)
from .vectors import locate

READBACK_WAIT_S = 30  # how long an instruction waits for a readback before it is repeated
CONTINUE_BEYOND_NM = 5  # further out than this, tower says "continue approach"
CLEAR_TO_LAND_NM = 4.5
GO_AROUND_NM = 1.0  # still no landing clearance this close in: go around
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
HOLDING_SHORT_NM = 0.075  # stopped this close to a runway to be crossed: holding short of it
CROSSING_FINAL_NM = 3.0  # an aircraft on final this close in keeps a crossing waiting


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
    direct_to: str | None = None
    next_frequency: str | None = None  # the frequency of the last hand-off, until it is tuned
    star_procedure: object | None = None  # procedures.Procedure
    constraints: list | None = None  # restrictions.Constraint along the STAR; None until looked up
    deviating: bool = False  # off track around weather
    low_altitude_called: bool = False
    emergency: str | None = None  # "mayday" or "pan pan"
    services_told: bool = False
    following: bool = False  # VFR flight following
    waiting_takeoff: bool = False  # held short for traffic; the takeoff clearance follows
    go_around_told: bool = False
    sid_constraints: list | None = None  # restrictions.Constraint: the SID's 'at or above' crossings
    atis_seen: dict[str, str] = field(default_factory=dict)  # airport -> information letter last heard
    awaiting_landing: bool = False  # told to continue approach; landing clearance still to come
    squawk_asked: bool = False
    bust_called_for: int | None = None  # the cleared level a bust was already called for
    pending_at: float = 0.0  # when the pending instruction was given (engine clock)
    pending_key: str | None = None  # the station that gave it
    reminded: bool = False
    # Taxi (see _taxi_out, _taxi_in)
    crossings: list[str] = field(default_factory=list)  # runways ("09R/27L") still to be crossed, each needing a clearance
    taxi_goal: tuple | None = None  # ("runway", ident) or ("stand", lat, lon, spoken name)
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
    off_route_since: float | None = None
    off_route_called: set[str] = field(default_factory=set)
    qnh_told: bool = False  # the destination's pressure was given with a descent to an altitude


def _squawk(rng: random.Random) -> str:
    while True:
        code = "".join(str(rng.randint(0, 7)) for _ in range(4))
        if code not in ("7500", "7600", "7700", "7000", "1200", "2000") and not code.endswith("00"):
            return code


class Engine:
    def __init__(
        self, db: AirportDB, seed: int | None = None, procedures=None, centers=None, navaids=None, clock=None,
        navfix=None, metars=None, holding: str = "never", traffic: bool = True, airspace=None,
    ):  # fmt: skip
        self.db = db
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
        self.last_raw: dict = {}  # the latest sim snapshot the engine was shown
        self.lock = threading.RLock()
        self._networks: dict[str, TaxiNetwork] = {}
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
        return self._procedures[ident]

    def set_plan(self, plan) -> None:
        """A new plan for a flight that has not started, or has ended, begins a new flight."""
        with self.lock:
            flight = self.flight
            parked = bool(self.last_raw.get("on_ground", True)) and self.last_raw.get("groundspeed_ms", 0.0) < 1
            if parked and (flight.has_been_airborne or flight.squawk or flight.contacted):
                self.reset()
            self.flight.plan = plan

    def reset(self) -> None:
        """Start a new flight: every clearance, contact and counter is forgotten. The plan is kept."""
        with self.lock:
            plan = self.flight.plan
            self.flight = Flight(plan=plan)
            self._traffic_seen.clear()
            self._rng = random.Random(self.seed)  # a new flight begins the same sequence again

    @staticmethod
    def active_runways(airport: Airport, issue: Issue | None) -> list[str]:
        return runways_in_use(airport, issue.weather.wind_dir_true, issue.weather.wind_kt) if issue else []

    def network(self, airport: Airport) -> TaxiNetwork:
        if airport.ident not in self._networks:
            self._networks[airport.ident] = parse_taxi(self.db.record_lines(airport))
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
        return f"Q N H {say.digits(int(issue.weather.qnh_hpa))}", float(int(issue.weather.qnh_hpa))

    def _departure_runway(self, airport: Airport, issue: Issue | None, raw: dict) -> str | None:
        """The departure runway whose threshold is nearest the aircraft."""
        active = runways_in_use(airport, issue.weather.wind_dir_true, issue.weather.wind_kt) if issue else []
        active = split_runways(active)[1]
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

    def _handoff(self, airport: Airport, kind: str) -> tuple[str, str, str] | None:
        """(spoken callsign, spoken frequency, 'nnn.nnn') of the airport's station of this kind."""
        station = self.db.station_of(airport, kind)
        if not station:
            return None
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

    def _check_readback(self, intent: Intent, callsign: str, station) -> Reply | None:
        pending = self.flight.pending
        # A request that happens to carry a number (a level asked for, a runway named) is not a readback.
        if pending is None or intent.intent not in ("readback", "other"):
            return None
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
            if intent.intent == "say_again":
                previous = self.flight.last_said.get(key)
                return Reply(previous, self.flight.pending.expect if self.flight.pending else {}) if previous else None
            if intent.intent in ("mayday", "pan_pan", "cancel_emergency"):
                reply = self._emergency(intent, station, callsign)
                if reply and reply.text:
                    self.flight.last_said[key] = reply.text
                return reply

            reply = self._check_readback(intent, callsign, station)
            if reply is None:
                if intent.intent == "readback":
                    reply = Reply("")  # an acknowledgement with nothing outstanding
                else:
                    contacted = set(self.flight.contacted)
                    reply = self._decide(intent, station, issue, raw, variation, callsign)
                    if reply is None:
                        self.flight.contacted = contacted  # the free-form fallback will do the greeting
            if reply is None:
                return None
            if reply.correction:
                return reply  # the original instruction stays pending and is what "say again" repeats
            if reply.expect:
                self._await_readback(reply, key)
            if reply.text:
                self.flight.last_said[key] = reply.text
            return reply

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
                return None
            flight.emergency = None
            return Reply(f"{callsign}, roger, emergency cancelled")
        flight.emergency = "mayday" if intent.intent == "mayday" else "pan pan"
        flight.squawk, flight.services_told = "7700", False
        hello = self._greeting(station)
        return Reply(f"{callsign}, {hello}roger {flight.emergency}, squawk seven seven zero zero, say intentions")

    def _await_readback(self, reply: Reply, key: str) -> None:
        flight = self.flight
        if "frequency" in reply.expect:
            flight.next_frequency = reply.expect["frequency"]  # offered in the app as a one-click tune
        flight.pending, flight.pending_key, flight.pending_at, flight.reminded = reply, key, self.now(), False

    def _decide(
        self, intent: Intent, station: Station, issue: Issue | None, raw: dict, variation: float, callsign: str
    ) -> Reply | None:
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
                target = self._handoff(airport, "delivery") or self._handoff(airport, "ground")
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
                target = self._handoff(airport, "delivery") or self._handoff(airport, "ground")
                return Reply(f"{callsign}, {hello}contact {target[0]} {target[1]} for clearance", {"frequency": target[2]})
            if not gives_clearance:
                return None
            runway = self._departure_runway(airport, issue, raw)
            runway_part = f", departure runway {say.runway(runway)}" if runway else ""
            atis_part = f", information {say.letter(issue.letter)} is current" if issue and intent.atis != issue.letter else ""
            has_plan = bool(flight.plan and flight.plan.destination)
            visual = intent.flight_rules == "VFR" or (intent.flight_rules is None and not intent.destination and not has_plan)
            flight.runway = runway
            next_station = self._handoff(airport, "ground") if station.kind == "delivery" else None
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
                target = self._handoff(airport, "ground") or self._handoff(airport, "tower")
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
            route = route_to_runway(self.network(airport), raw["lat"], raw["lon"], runway, self._threshold(airport, runway))
        via = self._route_words(route)
        flight.taxi_goal, flight.crossings = ("runway", runway), list(route.crossings) if route else []
        limit = self._limit()
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
                plan = flight.plan
                filed = plan.callsign.upper() if plan and plan.callsign else ""
                airline = filed[:3] if say.airline_callsign(filed) else None
                category = aircraft_category(raw.get("aircraft_type"), bool(airline))
                stand = flight.stand or assign_stand(network, category, airline, (raw["lat"], raw["lon"]), self._rng)
                stand = stand or nearest_parking(network, raw["lat"], raw["lon"])
            if stand:
                route = route_to_point(network, raw["lat"], raw["lon"], (stand.lat, stand.lon))
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
        ends = name.split("/")
        return next((r for r in airport.runways if r.end1 in ends or r.end2 in ends), None)

    def _offset_nm(self, airport: Airport, name: str, raw: dict) -> float | None:
        """Distance from the centreline of this runway, while abeam it; None when not beside it."""
        runway = self._runway_named(airport, name)
        if not runway or "lat" not in raw:
            return None
        position = locate(raw["lat"], raw["lon"], (runway.lat2, runway.lon2), runway.ends()[0][1])
        if not -0.05 < position.along_nm < runway.length_m / 1852 + 0.05:
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
        if offset < HOLDING_SHORT_NM and raw.get("groundspeed_ms", 99.0) < 1.0 and flight.pending is None:
            return self._crossing(station, raw, callsign, "", faa, asked=False)
        return None

    def _progressive(self, station: Station, raw: dict, callsign: str, hello: str, faa: bool) -> Reply | None:
        """The next part of the taxi route, from where the aircraft is now."""
        flight, airport = self.flight, station.airport
        goal = flight.taxi_goal
        if not goal or "lat" not in raw:
            return None
        network = self.network(airport)
        if goal[0] == "runway":
            route = route_to_runway(network, raw["lat"], raw["lon"], goal[1], self._threshold(airport, goal[1]))
        else:
            route = route_to_point(network, raw["lat"], raw["lon"], (goal[1], goal[2]))
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
            approved = "push back and start approved" if not faa else "push back approved"
            return Reply(f"{callsign}, {hello}{approved}{pressure_tail}")
        if intent.intent == "runway_vacated" or (intent.intent == "request_taxi" and arrived):
            return self._taxi_in(station, raw, callsign, hello, faa)
        if intent.intent == "request_taxi":
            return self._taxi_out(station, issue, raw, callsign, hello, faa, pressure_tail)
        if intent.intent == "ready_for_departure":
            target = self._handoff(airport, "tower")
            if target:
                return Reply(f"{callsign}, {hello}contact {target[0]} {target[1]}", {"frequency": target[2]})
        return None

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
                target = self._handoff(airport, "ground")
                if target:
                    return Reply(f"{callsign}, {hello}contact {target[0]} {target[1]}", {"frequency": target[2]})
                return self._taxi_in(station, raw, callsign, hello, faa)
            if intent.intent in ("request_pushback", "request_taxi") and "ground" in kinds:
                target = self._handoff(airport, "ground")
                return Reply(f"{callsign}, {hello}contact {target[0]} {target[1]}", {"frequency": target[2]})
            if intent.intent == "request_taxi":
                return self._taxi_out(station, issue, raw, callsign, hello, faa, pressure_tail)
            if intent.intent == "ready_for_departure":
                runway = flight.runway or self._departure_runway(airport, issue, raw)
                if not runway:
                    return None
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
                after = ", report downwind" if flight.circuits else ""
                return Reply(
                    f"{callsign}, {hello}runway {say.runway(runway)}, cleared for takeoff{wind_tail}{after}", {"runway": runway}
                )
            return None

        # Airborne.
        if intent.intent == "request_transit":
            return vfr.transit(self, intent, station, issue, raw, callsign, hello, faa)
        if intent.intent == "report_leaving":
            return vfr.leaving(self, station, raw, callsign, faa, asked=True)
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
        active = runways_in_use(airport, issue.weather.wind_dir_true, issue.weather.wind_kt) if issue else []
        if not active:
            return None
        if self.flight.runway in active:
            return self.flight.runway
        if intent.runway in active:
            return intent.runway
        landing = split_runways(active)[0]
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
                flight.crossings, flight.taxi_goal = [], None
                flight.told_leaving = False
                flight.told_vacate = False
            flight.has_been_airborne = True
        elif on_ground and flight.has_been_airborne and flight.phase in (Phase.AIRBORNE, Phase.LANDING_CLEARED):
            if not flight.touch_and_go:
                flight.phase = Phase.LANDED

    def tick(self, station: Station, issue: Issue | None, raw: dict) -> Reply | None:
        """Calls the controller makes without being asked. Call about once a second."""
        with self.lock:
            self._observe(raw)
            flight, airport = self.flight, station.airport
            if not raw.get("tail_number"):
                return None
            from .xpbridge import magnetic_variation

            callsign = self._callsign(raw)
            key = station.key
            reply = None
            # A new ATIS: tell an aircraft on frequency, as a controller would broadcast it.
            if airport is not None and issue is not None:
                heard = flight.atis_seen.get(airport.ident)
                flight.atis_seen[airport.ident] = issue.letter
                if heard and heard != issue.letter and key in flight.contacted:
                    pressure = self._pressure(issue, uses_faa_style(airport.ident))[0]
                    return Reply(f"{callsign}, information {say.letter(issue.letter)} is now current, {pressure}")
            # An instruction nobody read back is said once more.
            if (
                flight.pending
                and not flight.reminded
                and flight.pending_key == key
                and self.now() - flight.pending_at > READBACK_WAIT_S
            ):
                flight.reminded = True
                body = self.instruction_body(flight.pending.text, station.callsign)
                return Reply(
                    f"{callsign}, {station.callsign}, I say again, {body}", flight.pending.expect, correction=True,
                    event="readback_missed",
                )  # fmt: skip
            if station.kind in ifr.RADAR_KINDS:
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
                and raw.get("agl_m", 0.0) > 300
                and distance_nm(raw["lat"], raw["lon"], airport.lat, airport.lon) < 15
            ):
                flight.told_departure = True
                target = ifr.after_takeoff(self, airport, raw)
                if target:
                    reply = Reply(f"{callsign}, contact {target[0]} {target[1]}", {"frequency": target[2]})
            elif flight.phase == Phase.LANDED and not flight.told_vacate and raw.get("groundspeed_ms", 99.0) < 20:
                flight.told_vacate = True
                target = self._handoff(airport, "ground")
                tail = f", when vacated contact {target[0]} {target[1]}" if target else " and taxi to parking"
                reply = Reply(f"{callsign}, vacate at the next available exit{tail}", {"frequency": target[2]} if target else {})
            if reply:
                flight.last_said[key] = reply.text
                if reply.expect:
                    self._await_readback(reply, key)
            return reply

    def suggestions(self, station: Station, issue: Issue | None, raw: dict) -> list[dict]:
        """Calls the pilot could make right now, for the app's click-to-transmit buttons.

        Each has a short label, the full transmission, and the intent it stands for, so a
        clicked call skips speech recognition and the language model. `needs` names a value
        the app must ask for and put in place of {destination}.
        """
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

            if flight.pending:
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
                if handles_ground and flight.phase == Phase.PARKED:
                    add("Request push and start", "request push and start", "request_pushback")
                    add("Request taxi", "request taxi", "request_taxi")
                if (handles_ground and flight.phase == Phase.TAXI_OUT) or station.kind == "tower":
                    holding = (
                        f"holding short runway {say.runway(flight.runway)}, " if flight.runway and station.kind == "tower" else ""
                    )
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
            elif station.kind == "tower":
                runway = f" runway {say.runway(flight.runway)}" if flight.runway else ""
                if flight.arrival and flight.arrival.stage == "tower":
                    add("Check in", f"established {flight.arrival.approach}{runway}", "established")
                add("Inbound for landing", f"{information}inbound for landing", "inbound")
                add("Downwind", f"downwind{runway}", "position_report", position="downwind")
                add("Final", f"final{runway}", "position_report", position="final")
                add("Touch and go", "request touch and go", "request_touch_and_go", touch_and_go=True)
                add("Going around", "going around", "going_around")

            if station.kind == "tower":
                vfr.tower_suggestions(self, station, issue, raw, add)
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
        }
        return {key: value for key, value in facts.items() if value}
