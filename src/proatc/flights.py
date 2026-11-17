"""Whole flights, anywhere in the world, on this machine's X-Plane data: `proatc verify-flights`.

Unit tests hand the engine the station they mean. A real flight does not: the station is whatever
answers on the tuned frequency where the aircraft is, and that is where flights have gone wrong
(a centre that fell silent at its boundary, a neighbouring airport's approach controller answering,
a runway whose approach was never found). This flies hundreds of flights the way the radio loop and a
copilot do, and checks what must hold on every one of them:

- once in contact, never more than a few miles with nobody on the tuned frequency;
- the flight is sent to the destination's own approach or tower, and that controller sets up the arrival;
- no centre sends the flight to another centre a few miles from the airport;
- no bouncing between the same controllers;
- no sentence with a hole in it, and no exception.

`fly_full` flies the whole flight instead, from a stand at the origin to a stand at the destination: see
`GateToGate` below.
"""

from __future__ import annotations

import math
import random
import re
import time
import traceback
import zlib
from collections import Counter
from dataclasses import dataclass

from . import ifr, vfr
from . import phraseology as say
from .airways import open_airways
from .atis import Issue
from .centers import CenterIndex
from .engine import ON_RUNWAY_NM, Engine
from .flightplan import FlightPlan
from .intents import Intent
from .navdata import AirportDB, bearing, distance_nm
from .navfix import open_navfix
from .procedures import NavaidNames, choose_approach
from .radio import current_station
from .vectors import locate
from .verify import BAD_TEXT, Report
from .vfr import INFORMATION
from .weather import Cloud, Weather

SPEED_MS = 230.0
WEATHER = Weather(wind_dir_true=270, wind_kt=9, clouds=[Cloud(0.4, 3500)], qnh_hpa=1009)
SILENT_MILES = 6  # this long with nobody on the tuned frequency, after first contact, is a fault
TOO_MANY_HANDOFFS = 14
CLOSE_IN_NM = 30
BOUNCE_WITHIN_NM = 60  # sent to the same controller twice within this distance


def pick_flights(db: AirportDB, count: int, seed: int = 1, towered: bool = False) -> list[tuple[str, str]]:
    """Pairs of airports that have a tower or an approach controller, 120 to 1,200 miles apart, worldwide. With
    `towered`, both have a tower that controls traffic (not an information service), to start and end on the ground."""
    rows = db.db.execute(
        "SELECT a.ident, a.lat, a.lon, f.kind, f.label FROM airports a JOIN freqs f ON f.ident = a.ident "
        "WHERE f.kind IN ('tower', 'approach') ORDER BY a.ident, f.rowid"
    ).fetchall()
    usable = (lambda kind, label: kind == "tower" and not INFORMATION.search(label or "")) if towered else (lambda *_: True)
    rows = list({ident: (ident, lat, lon) for ident, lat, lon, kind, label in rows if usable(kind, label)}.values())
    rng = random.Random(seed)
    rng.shuffle(rows)
    pairs: list[tuple[str, str]] = []
    for ident, lat, lon in rows:
        if len(pairs) >= count:
            break
        for _ in range(30):
            other, other_lat, other_lon = rng.choice(rows)
            if other != ident and 120 <= distance_nm(lat, lon, other_lat, other_lon) <= 1200:
                pairs.append((ident, other))
                break
    return pairs


def shared_data(db: AirportDB) -> dict:
    """What every engine of a sweep shares: read once."""
    root = db.xplane_root
    db.centers = CenterIndex.open(root)
    return {"centers": db.centers, "navaids": NavaidNames(root), "navfix": open_navfix(root), "airways": open_airways(root)}


def fly(db: AirportDB, shared: dict, origin_ident: str, destination_ident: str) -> tuple[list[tuple[str, str]], list[str]]:
    """One flight from the climb to the destination controller's first instruction.

    Each hand-off is tuned and checked in on, as a copilot does. Returns (problems as (kind, detail),
    the radio work as lines of text)."""
    origin, destination = db.airport(origin_ident), db.airport(destination_ident)
    engine = Engine(db, seed=1, **shared)
    total = distance_nm(origin.lat, origin.lon, destination.lat, destination.lon)
    cruise = 36000 if total > 300 else 24000 if total > 160 else 12000
    engine.set_plan(FlightPlan(origin_ident, destination_ident, [], cruise))
    engine.flight.has_been_airborne, engine.flight.origin_ident = True, origin_ident
    heading = bearing(origin.lat, origin.lon, destination.lat, destination.lon)
    problems: list[tuple[str, str]] = []
    said: list[str] = []
    feet, khz, silent, handoffs, in_contact = float(cruise), None, 0, 0, False
    sent_to: Counter = Counter()
    last_sent: dict[str, float] = {}
    for step in range(int(min(40.0, total * 0.25)), int(total) - 3):
        part = step / total
        lat = origin.lat + (destination.lat - origin.lat) * part
        lon = origin.lon + (destination.lon - origin.lon) * part
        remaining = total - step
        cleared = engine.flight.cleared_ft or cruise
        feet = max(cleared, feet - 320) if feet > cleared else feet
        raw = {
            "tail_number": "N123AB", "lat": lat, "lon": lon, "on_ground": 0, "elevation_m": feet * 0.3048,
            "agl_m": max(feet - destination.elev_ft, 500) * 0.3048, "groundspeed_ms": SPEED_MS, "heading_true": heading,
            "heading_mag": heading, "ias_kt": 280.0 if feet > 12000 else 240.0, "aircraft_type": "A20N", "zulu_seconds": 43200.0,
        }  # fmt: skip
        if khz is None:
            # First contact: the centre for where the aircraft is. Where the data has none, nobody is called
            # until the destination is in range, as a pilot would do from the frequency list.
            here = engine.centers.at(lat, lon, feet) if engine.centers else None
            if here and here.freqs_khz:
                khz = here.freqs_khz[0]
            elif remaining <= 45:
                # Tuned from the chart: the engine is not told who should answer.
                khz = _listed_khz(db, destination, "approach", lat, lon) or _listed_khz(db, destination, "tower", lat, lon)
                if not khz:
                    return problems, said
            else:
                continue
        raw["com1_khz"] = khz
        _, station = current_station(db, raw)
        if station is None:
            silent += 1
            if silent == SILENT_MILES and in_contact:
                here = engine.centers.at(lat, lon, feet) if engine.centers else None
                if here and here.freqs_khz and not engine.flight.told_approach:
                    # There is a centre for this airspace and the flight was never sent to it.
                    problems.append(
                        ("left on a dead frequency", f"{khz / 1000:.3f}, {remaining:.0f} NM out, in {here.name}'s airspace")
                    )
                elif engine.flight.told_approach:
                    problems.append(("sent to a frequency nobody answers on", f"{khz / 1000:.3f}, {remaining:.0f} NM out"))
                # Otherwise the sim's data has no centre here. The pilot looks for the next one, as from the side panel.
                khz, in_contact = None, False
            continue
        silent = 0
        reply = None
        issue = Issue("K", WEATHER, 43200.0) if station.airport is not None else None
        if station.key not in engine.flight.contacted:
            reply = engine.handle(Intent("check_in"), station, issue, raw, 0.0)
            in_contact = True
        reply = reply or engine.tick(station, issue, raw)
        if station.airport is not None and station.airport.ident != destination_ident and engine.flight.arrival is not None:
            problems.append(
                ("vectored to another airport", f"{station.callsign} of {station.airport.ident} for {destination_ident}")
            )
            return problems, said
        if station.airport is not None and station.airport.ident == destination_ident:
            # The destination's controller: it must have something to say, and set up the arrival.
            said.append(f"{remaining:5.0f} NM FL{feet / 100:03.0f} {station.callsign}: {reply.text if reply else '(nothing)'}")
            if reply is None or not reply.text:
                problems.append(("the destination's controller gave no instruction", station.callsign))
            elif BAD_TEXT.search(reply.text):
                problems.append(("reads wrong", reply.text))
            elif station.kind in ("approach", "departure") and engine.flight.arrival is None:
                problems.append(("approach did not set up the arrival", f"{station.callsign}: {reply.text[:90]}"))
            return problems, said
        if reply is None or not reply.text:
            continue
        said.append(f"{remaining:5.0f} NM FL{feet / 100:03.0f} {station.callsign}: {reply.text}")
        if BAD_TEXT.search(reply.text):
            problems.append(("reads wrong", reply.text))
        engine.flight.pending = None
        if "altitude" in reply.expect:
            engine.flight.cleared_ft = reply.expect["altitude"]
        if "frequency" in reply.expect:
            handoffs += 1
            khz = round(float(reply.expect["frequency"]) * 1000)
            to = reply.text.split("contact ", 1)[-1]
            sent_to[to] += 1
            if not engine.flight.told_approach and remaining < CLOSE_IN_NM:
                problems.append(("sent to another centre close in", f"{to}, {remaining:.0f} NM out"))
            again_soon = to in last_sent and last_sent[to] - remaining < BOUNCE_WITHIN_NM
            last_sent[to] = remaining
            if handoffs > TOO_MANY_HANDOFFS or again_soon:
                problems.append(("bounced between controllers", "; ".join(f"{name} x{n}" for name, n in sent_to.most_common(3))))
                return problems, said
    if in_contact and khz and (engine._handoff(destination, "approach") or engine._handoff(destination, "tower")):
        problems.append(("never reached the destination's controller", f"last tuned {khz / 1000:.3f}"))
    return problems, said


def sweep_flights(db: AirportDB, pairs: list[tuple[str, str]], progress=None) -> Report:
    """Fly each pair and collect what broke, in the same shape of report as the airport sweep."""
    report = Report()
    shared = shared_data(db)
    started = time.perf_counter()
    for number, (origin, destination) in enumerate(pairs):
        if progress and number % 50 == 0:
            progress(f"{number}/{len(pairs)} {origin} to {destination}")
        report.airports += 1
        report.checks["flight"] += 1
        name = f"{origin}-{destination}"
        try:
            problems, _ = fly(db, shared, origin, destination)
        except Exception:
            report.failures.append((name, "flight", traceback.format_exc(limit=5).strip()))
            continue
        for kind, detail in problems:
            report.failures.append((name, "flight", f"{kind}: {detail}"))
    report.seconds = time.perf_counter() - started
    return report


# ---- gate to gate -----------------------------------------------------------------------------------------------
#
# `fly` starts in the climb and stops at the destination controller's first instruction. `fly_full` flies the whole
# flight, from a stand at the origin to a stand at the destination, as a competent pilot with a copilot would: the
# engine's suggested calls are the menu, every instruction is read back with the "Read back" call, every hand-off is
# tuned and checked in on, and the aircraft does what it was told. The aircraft is a simple kinematic model: it moves
# along its heading at a plausible speed, turns at rate one, and climbs or descends toward its cleared level.

GROUND_STEP_S = 2.0
AIR_STEP_S = 4.0
CRUISE_STEP_S = 8.0  # high and far from both airports: nothing there needs a finer step
TAXI_KT = 18.0
TURN_DEG_S = 3.0  # a rate one turn
ROTATE_KT = 150.0
LANDING_KT = 140.0
HOLD_SHORT_NM = 0.05  # where the pilot stops short of a runway: inside the engine's "holding short", outside "on it"
WAYPOINT_NM = 2.0  # a waypoint this close is passed
CAPTURE_NM = 0.6  # the final approach course is captured this close to it
OWN_FINAL_NM = 8.0  # without radar vectors, the pilot joins final this far out
REPORT_FINAL_NM = 6.0
GLIDE_FT_PER_NM = 318.0  # three degrees
TOO_MANY_VECTORS = 12
ARRIVAL_TOO_LONG_NM = 150.0  # flown with the destination's controllers without reaching final
HANDED_BY_NM = 20.0  # this close and still not with the destination's own controller
NOT_VACATED_S = 120.0
STUCK_S = 1800.0  # this long in one stage on the ground
STAND_NM = 0.15  # a taxi route to a stand must end this close to it
RECENT = 5  # lines of radio work given with a problem
JOIN_NM = 8  # flying an approach from its initial fix, the pilot joins final this far out
# Winds a flight may have, (from, knots): light, a strong crosswind for most runways, a tailwind for others.
WINDS = ((270, 9), (90, 14), (200, 30), (330, 22), (45, 18), (160, 25))
# Events by which a controller says the pilot did something wrong. A pilot that does as told should hear none.
COMPLAINTS = ("readback_missed", "readback_incomplete", "level_bust", "heading_not_flown", "off_route",
              "runway_incursion", "low_altitude", "restriction_missed")  # fmt: skip


@dataclass
class Aircraft:
    lat: float
    lon: float
    feet: float
    heading: float  # true: the flight is flown with no magnetic variation
    knots: float = 0.0  # ground speed
    on_ground: bool = True
    vs_fpm: float = 0.0


def _moved(lat: float, lon: float, heading: float, miles: float) -> tuple[float, float]:
    track = math.radians(heading)
    return lat + miles / 60 * math.cos(track), lon + miles / 60 * math.sin(track) / max(math.cos(math.radians(lat)), 0.05)


def _turned(current: float, wanted: float, most: float) -> float:
    change = (wanted - current + 180) % 360 - 180
    return (current + max(-most, min(most, change))) % 360


def _runway_course(airport, ident: str | None) -> float | None:
    return next((heading for runway in airport.runways for end, heading in runway.ends() if end == ident), None)


def _on_centreline(threshold: tuple[float, float], course: float, miles_out: float) -> tuple[float, float]:
    return _moved(*threshold, (course + 180) % 360, miles_out)


def _listed_khz(db: AirportDB, airport, kind: str, lat: float, lon: float) -> int | None:
    """The frequency a pilot reads off the chart for this airport's station of this kind (the sector for where the
    aircraft is): tuned by hand, which tells the engine nothing about who should answer."""
    station = db.station_of(airport, kind, bearing(airport.lat, airport.lon, lat, lon))
    return station.khz if station else None


DIGIT_WORDS = {word: str(number) for number, word in enumerate(say.DIGITS)}
SPOKEN_NUMBER = r"((?:(?:zero|one|two|three|four|five|six|seven|eight|niner) ?)+)"


def _number(words: str) -> int:
    return int("".join(DIGIT_WORDS[word] for word in words.split()))


def _runway_said(text: str, airport, lead: str) -> str | None:
    """The runway of this airport named in `text` right after `lead` ("approach runway", "cleared to land"...)."""
    ends = [end for runway in airport.runways for end in (runway.end1, runway.end2)]
    for end in sorted(ends, key=lambda end: -len(say.runway(end))):
        if re.search(rf"\b{lead} {say.runway(end)}(?![a-z ]*(?:left|right|center)\b)", text):
            return end
    return None


def _weathers(origin: str, destination: str) -> tuple[Weather, Weather | None]:
    """The wind for this flight, and for one flight in three a different wind from the middle of the flight on: so
    runway choice is flown in crosswinds, tailwinds and a change of runway, not only in one wind."""
    pick = zlib.crc32(f"{origin}-{destination}".encode())
    first = WINDS[pick % len(WINDS)]
    second = WINDS[(pick // len(WINDS)) % len(WINDS)] if pick % 3 == 0 else None
    weather = lambda wind: Weather(wind_dir_true=wind[0], wind_kt=wind[1], clouds=[Cloud(0.4, 3500)], qnh_hpa=1009)  # noqa: E731
    return weather(first), (weather(second) if second and second != first else None)


class GateToGate:
    """One flight from a stand at the origin to a stand at the destination. `run()` gives (problems, transcript).

    The pilot acts on what it hears: headings, levels, speeds, the approach, the runways and the frequencies come from
    the controllers' words and the readback items, never from the engine's state. It tunes by frequency, from what
    it was told or from the chart, and reads charts for what a pilot has them for (taxi route on the map, the fixes of
    an approach, the minimum safe altitudes). A problem is (kind, detail); the detail names the stage of the flight
    and ends with the last radio work."""

    def __init__(self, db: AirportDB, shared: dict, origin_ident: str, destination_ident: str):
        self.db = db
        self.origin, self.destination = db.airport(origin_ident), db.airport(destination_ident)
        self.t = 0.0
        self.engine = Engine(db, seed=1, clock=lambda: self.t, **shared)
        self.total = distance_nm(self.origin.lat, self.origin.lon, self.destination.lat, self.destination.lon)
        self.cruise = 36000 if self.total > 300 else 24000 if self.total > 160 else 12000
        self.engine.set_plan(FlightPlan(origin_ident, destination_ident, [], self.cruise))
        self.first_weather, self.second_weather = _weathers(origin_ident, destination_ident)
        self.problems: list[tuple[str, str]] = []
        self.said: list[str] = []
        self.stage, self.stage_since = "clearance", 0.0  # what the pilot is doing: see _next_call
        self.stopped = False
        self.step = GROUND_STEP_S
        self.khz: int | None = None
        self.station = None  # the last station that answered
        self.handed_to = ""  # the station named in the last hand-off, until somebody answers
        self.cleared: float | None = None  # the level read back last
        self.speed_kt: float | None = None  # the last speed limit given
        # ("path", points) on the ground; ("route", points), ("heading", h), ("intercept", h), ("procedure", points),
        # ("own", None) or ("final", None) in the air
        self.lateral: tuple = ("hold", None)
        self.holding_short: str | None = None  # the runway ("09R/27L") the aircraft is stopped short of
        self.hold_short_said: set[str] = set()  # runways the controller said to hold short of
        self.crossing_cleared: set[str] = set()  # runways the controller cleared it to cross
        self.departure_runway: str | None = None  # as said in the taxi clearance
        self.takeoff_cleared = False
        self.approach_runway: str | None = None  # the runway of the approach it was cleared for, as said
        self.landing_runway: str | None = None  # the runway it is to land on, as said (a circling runway, say)
        self.expected_runway: str | None = None  # "expect ... approach runway X"
        self.landing_cleared: str | None = None
        self.side: str | None = None  # side of the final approach course while intercepting it
        self.silent_nm = 0.0
        self.in_contact = False
        self.vectors = 0
        self.arrival_nm = 0.0
        self.stopped_at: float | None = None  # when the landing roll ended
        self.seen_kinds: set[tuple[str, str]] = set()
        self.plane = self._parked_at_origin()

    # ---- what went wrong ------------------------------------------------------------------------------------------

    def problem(self, kind: str, detail: str, stop: bool = False) -> None:
        if (kind, detail) in self.seen_kinds:
            return
        self.seen_kinds.add((kind, detail))
        recent = "".join(f"\n      {line}" for line in self.said[-RECENT:])
        self.problems.append((kind, f"{self.stage}: {detail}{recent}"))
        self.stopped = self.stopped or stop

    def note(self, who: str, text: str) -> None:
        plane = self.plane
        where = "ground" if plane.on_ground else f"{plane.feet:,.0f} ft"
        miles = distance_nm(plane.lat, plane.lon, self.destination.lat, self.destination.lon)
        self.said.append(f"{self.stage:<12} {where:>9} {miles:5.0f} NM  {who}: {text}")

    def set_stage(self, stage: str) -> None:
        self.stage, self.stage_since = stage, self.t

    # ---- the start ------------------------------------------------------------------------------------------------

    def _parked_at_origin(self) -> Aircraft:
        """At a stand for jets when the scenery has one, else any stand, else the airport's reference point."""
        airport = self.origin
        network = self.engine.network(airport)
        stands = sorted(network.parking, key=lambda p: (p.kind != "gate", "jets" not in p.categories, p.name))
        if stands and network.usable:
            return Aircraft(stands[0].lat, stands[0].lon, airport.elev_ft, 0.0)
        return Aircraft(airport.lat, airport.lon, airport.elev_ft, 0.0)

    # ---- the loop -------------------------------------------------------------------------------------------------

    def run(self) -> tuple[list[tuple[str, str]], list[str]]:
        try:
            self._run()
        except Exception:
            self.problem("exception", traceback.format_exc(limit=6).strip(), stop=True)
        return self.problems, self.said

    def _run(self) -> None:
        first = next((s for s in (self.db.station_of(self.origin, k) for k in ("delivery", "ground", "tower")) if s), None)
        if first is None:
            self.problem("no way forward", f"{self.origin.ident} has no delivery, ground or tower", stop=True)
            return
        self.khz = first.khz
        limit = (self.total / 250 + 2) * 3600
        while self.stage != "parked" and not self.stopped:
            if self.t > limit:
                self.problem("no way forward", "the flight did not finish in time", stop=True)
                return
            if self.plane.on_ground and self.t - self.stage_since > STUCK_S:
                self.problem("no way forward", f"stuck on the ground ({self.stage})", stop=True)
                return
            self.step = self._step_seconds()
            self.t += self.step
            self._fly()
            if not self.stopped:
                self._radio(self._raw())
        if self.stage == "parked":
            self._check_parked()

    def _step_seconds(self) -> float:
        plane = self.plane
        if plane.on_ground:
            return GROUND_STEP_S
        near = min(
            distance_nm(plane.lat, plane.lon, self.origin.lat, self.origin.lon),
            distance_nm(plane.lat, plane.lon, self.destination.lat, self.destination.lon),
        )
        return CRUISE_STEP_S if plane.feet > 15000 and near > 60 else AIR_STEP_S

    def _ground_elevation(self) -> float:
        plane = self.plane
        to_origin = distance_nm(plane.lat, plane.lon, self.origin.lat, self.origin.lon)
        to_destination = distance_nm(plane.lat, plane.lon, self.destination.lat, self.destination.lon)
        return self.origin.elev_ft if to_origin < to_destination else self.destination.elev_ft

    def _raw(self) -> dict:
        plane, flight = self.plane, self.engine.flight
        raw = {
            "tail_number": "N123AB", "lat": plane.lat, "lon": plane.lon, "on_ground": int(plane.on_ground),
            "elevation_m": plane.feet * 0.3048, "agl_m": max(plane.feet - self._ground_elevation(), 0.0) * 0.3048,
            "groundspeed_ms": plane.knots / 1.944, "heading_true": plane.heading, "heading_mag": plane.heading,
            "ias_kt": plane.knots if plane.on_ground else self._ias(), "vs_fpm": plane.vs_fpm, "aircraft_type": "A20N",
            "zulu_seconds": 43200.0 + self.t,
        }  # fmt: skip
        if self.khz:
            raw["com1_khz"] = self.khz
        if flight.squawk:
            raw["squawk"] = int(flight.squawk)  # set as soon as it is given
        return raw

    # ---- the radio ------------------------------------------------------------------------------------------------

    def _shifted(self) -> bool:
        """Past the middle of the flight, for a flight whose wind changes."""
        if self.second_weather is None:
            return False
        plane = self.plane
        return distance_nm(plane.lat, plane.lon, self.destination.lat, self.destination.lon) < self.total / 2

    def _issue(self, station):
        if station.airport is None:
            return None
        if self._shifted():
            return Issue("L", self.second_weather, 43200.0)
        return Issue("K", self.first_weather, 43200.0)

    def _radio(self, raw: dict) -> None:
        _, station = current_station(self.db, raw) if self.khz else (None, None)
        if station is None:
            self._silent(raw)
            return
        self._answered(station)
        if self.stopped:
            return
        flight = self.engine.flight
        issue, tuned = self._issue(station), self.khz
        reply = self.engine.tick(station, issue, raw)
        if reply is not None and reply.text:
            self._heard(station, issue, raw, reply)
        if self.stopped or self.khz != tuned or flight.pending is not None:
            return
        call = self._next_call(station, issue, raw)
        if call is not None:
            self._transmit(station, issue, raw, call)

    def _answered(self, station) -> None:
        """A station answers on the tuned frequency: it must be the one the flight was sent to."""
        self.silent_nm, self.in_contact, self.station = 0.0, True, station
        if self.handed_to and not self.handed_to.startswith(station.callsign):
            self.problem(
                "handed to one controller, another answers", f"sent to {self.handed_to}, {station.callsign} answers", stop=True
            )
        self.handed_to = ""
        airport = station.airport
        if not self.plane.on_ground and airport is not None and airport.ident not in (self.origin.ident, self.destination.ident):
            if station.kind in ("approach", "departure") and self.stage == "arrival":
                self.problem("vectored to another airport", f"{station.callsign} of {airport.ident}", stop=True)
        if not self.plane.on_ground and self.stage in ("departure", "en route"):
            if airport is not None and airport.ident == self.destination.ident:
                self.set_stage("arrival")
            elif station.kind == "center" and self.stage == "departure":
                self.set_stage("en route")

    def _silent(self, raw: dict) -> None:
        """Nobody on the tuned frequency. After a hand-off that is a fault, and the flight stops there. In a gap the
        sim's data leaves between controllers, the pilot looks for whoever works this airspace, from the side panel."""
        plane = self.plane
        if plane.on_ground:
            self.problem("sent to a frequency nobody answers on", f"{(self.khz or 0) / 1000:.3f} on the ground", stop=True)
            return
        if self.khz is not None:
            self.silent_nm += plane.knots * self.step / 3600
            if self.silent_nm < SILENT_MILES or not self.in_contact:
                if not self.in_contact and self.silent_nm >= SILENT_MILES:
                    self._look_for_a_station()  # tuned by the pilot, and nobody there: look again
                return
            here = self.engine.centers.at(plane.lat, plane.lon, plane.feet) if self.engine.centers else None
            if self.handed_to:
                self.problem("sent to a frequency nobody answers on", f"{self.khz / 1000:.3f}, to {self.handed_to}", stop=True)
                return
            if here and here.freqs_khz and not self.engine.flight.told_approach:
                self.problem("left on a dead frequency", f"{self.khz / 1000:.3f}, in {here.name}'s airspace", stop=True)
                return
            self.khz, self.in_contact, self.handed_to = None, False, ""
        self._look_for_a_station()

    def _look_for_a_station(self) -> None:
        """From the chart and the side panel, as a pilot does: nothing the engine is told."""
        plane, engine = self.plane, self.engine
        self.silent_nm = 0.0
        remaining = distance_nm(plane.lat, plane.lon, self.destination.lat, self.destination.lon)
        if remaining <= 45:
            khz = _listed_khz(self.db, self.destination, "approach", plane.lat, plane.lon)
            if khz is None and remaining <= 25:
                khz = _listed_khz(self.db, self.destination, "tower", plane.lat, plane.lon)
            if khz:
                self.khz = khz
                return
        here = engine.centers.at(plane.lat, plane.lon, plane.feet) if engine.centers else None
        if here and here.freqs_khz:
            self.khz = here.freqs_khz[0]

    def _calls(self, station, issue, raw) -> dict[str, dict]:
        calls = self.engine.suggestions(station, issue, raw)
        for call in calls:
            if BAD_TEXT.search(call["text"]):
                self.problem("a suggested call reads wrong", call["text"])
        return {call["label"]: call for call in calls}

    def _transmit(self, station, issue, raw, call: dict) -> None:
        self.note("pilot", call["text"])
        reply = self.engine.handle(Intent.from_dict(call["intent"]), station, issue, raw, 0.0)
        if reply is None:
            self.problem("a suggested call is not answered", f"'{call['label']}' to {station.callsign}", stop=True)
            return
        if reply.text:
            self._heard(station, issue, raw, reply)

    def _heard(self, station, issue, raw, reply) -> None:
        """A controller's transmission: checked, read back if it must be, and done."""
        flight = self.engine.flight
        self.note(station.callsign, reply.text)
        if BAD_TEXT.search(reply.text):
            self.problem("reads wrong", reply.text)
        if reply.event in COMPLAINTS:
            self.problem(f"controller says the pilot erred ({reply.event})", reply.text)
        if reply.correction:
            self.problem("a correct readback is refused", reply.text)
            flight.pending = None
            return
        if reply.expect and flight.pending is reply:
            readback = self._calls(station, issue, raw).get("Read back")
            if readback is None:
                self.problem("no way forward", f"no 'Read back' offered for: {reply.text}", stop=True)
                return
            self.note("pilot", readback["text"])
            answer = self.engine.handle(Intent.from_dict(readback["intent"]), station, issue, raw, 0.0)
            if answer is not None and answer.text:
                self.note(station.callsign, answer.text)
                if answer.correction:
                    self.problem("a correct readback is refused", answer.text)
                    flight.pending = None
        self._follow(station, reply)

    # ---- doing what was said --------------------------------------------------------------------------------------

    def _follow(self, station, reply) -> None:
        expect, text, plane = reply.expect, reply.text, self.plane
        if not plane.on_ground:
            self._approach_words(station, text)
        if "altitude" in expect:
            self._check_altitude(float(expect["altitude"]), text)
            self.cleared = float(expect["altitude"])
        speed = re.search(rf"(?:reduce speed(?: to)?|speed) {SPOKEN_NUMBER}|maintain {SPOKEN_NUMBER}knots", text)
        if speed and not plane.on_ground:
            self.speed_kt = float(_number(speed.group(1) or speed.group(2)))
        if "heading" in expect and not plane.on_ground:
            heading = float(expect["heading"])
            ours = station.airport is not None and station.airport.ident == self.destination.ident
            if ours and station.kind in ("approach", "departure"):
                self.vectors += 1
                if self.vectors > TOO_MANY_VECTORS:
                    self.problem("vectors never converge", f"{self.vectors} headings from {station.callsign}", stop=True)
            if self.approach_runway and re.search(r"cleared .*approach runway", text):
                self._check_intercept(heading)
                self.lateral, self.side = ("intercept", heading), None
            else:
                self.lateral = ("heading", heading)
        if "frequency" in expect:
            self.khz = round(float(expect["frequency"]) * 1000)
            self.handed_to = text.split("contact ", 1)[-1]
        self._ground_instruction(reply)

    def _approach_words(self, station, text: str) -> None:
        """The approach and the runways, as the controller names them."""
        airport = self.destination
        expected = _runway_said(text, airport, r"expect [^,]*approach runway")
        if expected:
            self.expected_runway = expected
        cleared = _runway_said(text, airport, r"cleared [^,]*approach runway")
        if cleared and re.search(r"cleared [^,]*approach runway", text):
            self.approach_runway = cleared
            self.landing_runway = _runway_said(text, airport, "circle to land runway") or cleared
            via = re.search(r" via ([A-Za-z ]+?), report", text)
            if via and self.lateral[0] not in ("procedure", "final"):
                self._fly_procedure(via.group(1), cleared)
            elif "visual approach" in text and self.lateral[0] not in ("own", "final"):
                self.lateral = ("own", None)
        if station.kind == "tower" and self.approach_runway is None:
            # No approach clearance: the tower's runway, flown as the pilot's own approach.
            given = _runway_said(text, airport, "continue approach runway") or _runway_said(text, airport, "runway")
            if given and self.lateral[0] not in ("own", "final"):
                self.landing_runway = given
                self.lateral = ("own", None)
        if "cleared to land" in text:
            self.landing_cleared = _runway_said(text, airport, "runway")
            if self.landing_runway and self.landing_cleared != self.landing_runway:
                self.problem("cleared to land on another runway", f"approach runway {self.landing_runway}: {text}", stop=True)

    def _fly_procedure(self, fix_words: str, runway: str) -> None:
        """Cleared for the approach via its initial fix: the fix, from the approach chart, then onto final."""
        engine, airport = self.engine, self.destination
        found = None
        for approach in engine.procedures(airport.ident).approaches.values():
            for fix, _ in approach.initial_fixes() if approach.runway == runway else []:
                if ifr._fix_word(engine, fix, airport.lat, airport.lon) == fix_words.strip():
                    found = fix
        where = engine.navfix.position(found, airport.lat, airport.lon, 80) if found and engine.navfix else None
        if where is None:
            self.problem("approach fix not on the chart", f"via {fix_words} for runway {runway}", stop=True)
            return
        threshold, course = engine._threshold(airport, runway), _runway_course(airport, runway)
        self.lateral = ("procedure", [where, _on_centreline(threshold, course, JOIN_NM)])

    def _check_altitude(self, feet: float, text: str) -> None:
        """No descent below the minimum safe altitude where the aircraft is: the sector altitude near an airport
        (or the approach's own published altitude for the runway expected), the terrain below further out."""
        plane = self.plane
        if plane.on_ground or feet >= plane.feet - 200:
            return
        navfix, engine = self.engine.navfix, self.engine
        airport = self.destination if self.engine.flight.has_been_airborne else self.origin
        near = distance_nm(plane.lat, plane.lon, airport.lat, airport.lon) <= 25
        if near:
            sector = (navfix.airport_safe_ft(airport.ident) if navfix else None) or 0
            runway = self.approach_runway or self.expected_runway
            approach = choose_approach(engine.procedures(airport.ident), runway) if runway else None
            published = approach.platform_ft if approach and approach.platform_ft else None
            floor = min(sector, published) if published and sector else (sector or published or 0)
            floor = max(floor, airport.elev_ft + 1000)
        else:
            floor = (navfix.grid_safe_ft(plane.lat, plane.lon) if navfix else None) or 0
        if feet < floor - 100:
            self.problem("cleared below the minimum safe altitude", f"{feet:,.0f} ft where the minimum is {floor:,.0f}: {text}")

    def _check_intercept(self, heading: float) -> None:
        """An intercept must join final with room to settle, and not far above the glide path."""
        final = self._final()
        if final is None:
            return
        position = locate(self.plane.lat, self.plane.lon, *final)
        angle = abs((heading - final[1] + 180) % 360 - 180)
        joins = position.along_nm if angle < 5 else position.along_nm - position.cross_nm / math.tan(math.radians(angle))
        feet = min(self.cleared or self.plane.feet, self.plane.feet)
        above = feet - self.destination.elev_ft - max(joins, 0.0) * GLIDE_FT_PER_NM
        if joins < 4 or above > 1000:
            self.problem("intercept too close or too high", f"joins final {joins:.1f} NM out, {above:,.0f} ft above the path")

    def _ground_instruction(self, reply) -> None:
        """What the pilot does on the ground with what was said: push, taxi, cross, line up, vacate."""
        text, airport = reply.text, self._ground_airport()
        for end in [end for runway in airport.runways for end in (runway.end1, runway.end2)]:
            name = self._runway_name(airport, end)
            if re.search(rf"\bhold short runway {say.runway(end)}\b(?! (?:left|right|center))", text):
                self.hold_short_said.add(name)
            if re.search(rf"\bcross runway {say.runway(end)}\b(?! (?:left|right|center))", text):
                self.crossing_cleared.add(name)
        if self.stage == "clearance" and "squawk" in reply.expect:
            self.set_stage("push")
        elif self.stage == "push" and "push back" in text:
            self.set_stage("taxi request")
        elif self.stage == "taxi request" and "taxi" in text:
            self.departure_runway = _runway_said(text, airport, "runway")
            self._start_taxi("runway")
        elif self.stage in ("ready", "taxi out") and "cleared for takeoff" in text:
            self.takeoff_cleared = True
            self.set_stage("lining up")
        elif self.stage == "landing roll" and "vacate" in text:
            self._vacate()
        elif self.stage == "vacated" and "taxi to" in text:
            self._start_taxi("stand")

    @staticmethod
    def _runway_name(airport, end: str) -> str:
        runway = next(runway for runway in airport.runways if end in (runway.end1, runway.end2))
        return f"{runway.end1}/{runway.end2}"

    def _next_call(self, station, issue, raw) -> dict | None:
        """The call that moves the flight on from here, from the engine's menu; None when there is nothing to say."""
        flight, plane = self.engine.flight, self.plane
        wanted: str | None = None
        if self.stage == "clearance":
            wanted = "Request IFR clearance"
        elif self.stage == "push":
            if station.kind == "delivery":
                self._tune_own("ground", "tower")  # "contact ground when ready for push and start"
                return None
            wanted = "Request push and start"
        elif self.stage == "taxi request":
            wanted = "Request taxi"
        elif self.stage in ("taxi out", "taxi in") and self.holding_short:
            if self.holding_short not in self.hold_short_said:
                # A runway on the route that the controller never named: a pilot does not cross it unasked.
                self.problem("taxi route crosses a runway not given as a crossing", self.holding_short, stop=True)
                return None
            wanted = "Holding short"
        elif self.stage == "ready":
            wanted = "Ready for departure"
        elif self.stage == "vacated":
            wanted = "Runway vacated"
        elif not plane.on_ground and station.key not in flight.contacted:
            calls = self._calls(station, issue, raw)
            if station.kind == "tower":
                found = calls.get("Check in") or calls.get("Inbound for landing") or calls.get("Inbound")  # AFIS: "Inbound"
                if found is None:
                    self.problem("no way forward", f"no call to check in with {station.callsign}", stop=True)
                return found
            # Oceanic airspace has no radar: the first call there is a position report.
            wanted = "Position report" if station.kind == "center" and station.center.oceanic else "Check in"
        elif not plane.on_ground and station.kind == "tower" and self._ready_to_report_final():
            wanted = "Final"
        if wanted is None:
            return None
        calls = self._calls(station, issue, raw)
        if wanted not in calls:
            self.problem("no way forward", f"no '{wanted}' call offered by {station.callsign}", stop=True)
        return calls.get(wanted)

    def _tune_own(self, *kinds: str) -> None:
        """The pilot tunes the airport's next station from the chart."""
        for kind in kinds:
            station = self.db.station_of(self.origin, kind)
            if station:
                self.khz = station.khz
                return
        self.problem("no way forward", f"{self.origin.ident} has no {' or '.join(kinds)}", stop=True)

    def _ready_to_report_final(self) -> bool:
        """Flying its own approach, with no landing clearance yet: the pilot reports final once established on it."""
        if self.lateral[0] != "final" or self.landing_cleared or self.approach_runway:
            return False
        if self.said and "continue approach" in self.said[-1]:
            return False  # told to continue, already: the landing clearance comes by itself
        position = self._final_position()
        return position is not None and position.cross_nm < 0.5 and position.along_nm < REPORT_FINAL_NM

    # ---- on the ground --------------------------------------------------------------------------------------------

    def _ground_airport(self):
        return self.origin if not self.engine.flight.has_been_airborne else self.destination

    def _start_taxi(self, goal: str) -> None:
        """Follow the taxi route the controller gave, as the map shows it; without a taxiway network, straight on."""
        flight, engine = self.engine.flight, self.engine
        airport = self._ground_airport()
        route = flight.taxi_route
        usable = engine.network(airport).usable
        points: list[tuple[float, float]] = list(route.points) if route and route.points else []
        if goal == "runway":
            if self.departure_runway is None:
                self.problem("no way forward", "the taxi clearance names no runway", stop=True)
                return
            if not points and usable:
                self.problem("no taxi route", f"to runway {self.departure_runway} at {airport.ident}", stop=True)
                return
            if not points:
                points = [engine._threshold(airport, self.departure_runway)]
            else:
                self._check_reaches_runway(airport, points[-1])
            self.set_stage("taxi out")
        else:
            stand = flight.stand
            if not points and usable and stand is not None:
                self.problem("no taxi route", f"to the stand at {airport.ident}", stop=True)
                return
            if points and stand is not None and len(points) > 1:
                gap = distance_nm(*points[-2], stand.lat, stand.lon)
                if gap > STAND_NM:
                    self.problem("taxi route does not reach its goal", f"ends {gap * 1852:.0f} m from stand {stand.name}")
            self.set_stage("taxi in")
        self.lateral, self.holding_short = ("path", points), None

    def _check_reaches_runway(self, airport, end: tuple[float, float]) -> None:
        runway = self.departure_runway
        offset = self.engine._offset_nm(airport, runway, {"lat": end[0], "lon": end[1]})
        if offset is None or offset > 0.03:
            far = distance_nm(*end, *self.engine._threshold(airport, runway)) * 1852
            self.problem("taxi route does not reach its goal", f"ends {far:.0f} m from the threshold of {runway}, off the runway")

    def _runway_ahead(self, lat: float, lon: float, target: tuple[float, float]) -> str | None:
        """The runway the next bit of taxiing would enter without a clearance to: any runway the route goes onto, not
        cleared to cross, and the departure runway until cleared for takeoff."""
        engine, airport = self.engine, self._ground_airport()
        for runway in airport.runways:
            name = f"{runway.end1}/{runway.end2}"
            if name in self.crossing_cleared:
                continue
            if self.takeoff_cleared and self.departure_runway in (runway.end1, runway.end2):
                continue
            onto = engine._offset_nm(airport, name, {"lat": target[0], "lon": target[1]})
            if onto is None or onto > ON_RUNWAY_NM:
                continue  # the route does not go onto this runway here
            after = engine._offset_nm(airport, name, {"lat": lat, "lon": lon})
            now = engine._offset_nm(airport, name, {"lat": self.plane.lat, "lon": self.plane.lon})
            if after is not None and after < HOLD_SHORT_NM and (now is None or after < now):
                return name
        return None

    def _taxi(self) -> None:
        plane, points = self.plane, self.lateral[1]
        budget = TAXI_KT * self.step / 3600
        self.holding_short = None
        while budget > 1e-6 and points:
            gap = distance_nm(plane.lat, plane.lon, *points[0])
            if gap < 1e-5:
                points.pop(0)
                continue
            piece = min(budget, gap, 0.005)  # about ten metres at a time, so no hold line is passed
            heading = bearing(plane.lat, plane.lon, *points[0])
            lat, lon = points[0] if piece >= gap else _moved(plane.lat, plane.lon, heading, piece)
            blocking = self._runway_ahead(lat, lon, points[0])
            if blocking:
                self.holding_short = blocking
                break
            plane.lat, plane.lon, plane.heading = lat, lon, heading
            budget -= piece
        plane.knots = 0.0 if self.holding_short or not points else TAXI_KT
        departure = self.stage == "taxi out" and self._runway_name(self.origin, self.departure_runway)
        if self.stage == "taxi out" and (not points or self.holding_short == departure):
            self.holding_short = None
            self.set_stage("ready")
        elif self.stage == "lining up" and not points:
            self._line_up()
        elif self.stage == "taxi in" and not points:
            self.set_stage("parked")

    def _line_up(self) -> None:
        """At the end of the taxi route, on the runway: turned onto its heading, where the aircraft is."""
        plane, runway = self.plane, self.departure_runway
        course = _runway_course(self.origin, runway)
        if self.engine._offset_nm(self.origin, runway, {"lat": plane.lat, "lon": plane.lon}) is None:
            self.problem("lined up off the runway", f"runway {runway}", stop=True)
            return
        plane.heading, plane.knots = course, 0.0
        self.set_stage("takeoff")

    def _take_off(self) -> None:
        plane = self.plane
        plane.knots += 6.0 * self.step
        plane.lat, plane.lon = _moved(plane.lat, plane.lon, plane.heading, plane.knots * self.step / 3600)
        if plane.knots >= ROTATE_KT:
            plane.on_ground, plane.feet = False, self.origin.elev_ft + 50
            flight = self.engine.flight
            route = [point for point in flight.sid_path[1:]] if flight.sid_path else []
            self.lateral = ("route", [*route, (self.destination.lat, self.destination.lon)])
            self.set_stage("departure")

    def _landing_roll(self) -> None:
        plane = self.plane
        if plane.knots > 0:
            plane.lat, plane.lon = _moved(plane.lat, plane.lon, plane.heading, plane.knots * self.step / 3600)
            plane.knots = max(0.0, plane.knots - 5.0 * self.step)
            return
        if self.stopped_at is None:
            self.stopped_at = self.t
        elif self.t - self.stopped_at > NOT_VACATED_S:
            self.problem("not told to vacate", f"stopped on runway {self.landing_cleared} for two minutes", stop=True)

    def _vacate(self) -> None:
        """Off the runway to the nearest taxiway node clear of every runway; without a taxiway network, to the side."""
        plane, engine = self.plane, self.engine
        network = engine.network(self.destination)
        on_runways = set().union(*network.runway_nodes.values()) if network.runway_nodes else set()
        runways = [f"{runway.end1}/{runway.end2}" for runway in self.destination.runways]

        def clear_of_runways(node: int) -> bool:
            lat, lon = network.nodes[node]
            offsets = (engine._offset_nm(self.destination, name, {"lat": lat, "lon": lon}) for name in runways)
            return all(offset is None or offset > HOLD_SHORT_NM for offset in offsets)

        beside = [node for node in network.taxi_nodes() - on_runways if clear_of_runways(node)]
        node = min(beside, key=lambda n: distance_nm(plane.lat, plane.lon, *network.nodes[n]), default=None)
        target = network.nodes[node] if node is not None else _moved(plane.lat, plane.lon, (plane.heading + 90) % 360, 0.1)
        self.lateral = ("path", [target])
        self.set_stage("vacating")

    def _fly_on_ground(self) -> None:
        if self.stage in ("taxi out", "taxi in", "lining up"):
            self._taxi()
        elif self.stage == "takeoff":
            self._take_off()
        elif self.stage == "landing roll":
            self._landing_roll()
        elif self.stage == "vacating":
            plane, points = self.plane, self.lateral[1]
            target = points[0]
            gap = distance_nm(plane.lat, plane.lon, *target)
            plane.heading = bearing(plane.lat, plane.lon, *target)
            step = TAXI_KT * self.step / 3600
            plane.lat, plane.lon = target if gap <= step else _moved(plane.lat, plane.lon, plane.heading, step)
            plane.knots = TAXI_KT
            if gap <= step:
                plane.knots = 0.0
                self.set_stage("vacated")
        else:
            self.plane.knots = 0.0

    def _check_parked(self) -> None:
        stand, plane = self.engine.flight.stand, self.plane
        if stand is not None and distance_nm(plane.lat, plane.lon, stand.lat, stand.lon) > STAND_NM:
            self.problem(
                "not parked at the end",
                f"{distance_nm(plane.lat, plane.lon, stand.lat, stand.lon) * 1852:.0f} m from {stand.name}",
            )
        self.note("pilot", "parked")

    # ---- in the air -----------------------------------------------------------------------------------------------

    def _fly(self) -> None:
        if self.plane.on_ground:
            self._fly_on_ground()
        else:
            self._fly_in_the_air()

    def _final(self) -> tuple[tuple[float, float], float] | None:
        """(threshold, course) of the final approach being flown: the runway the approach clearance named, or for
        an approach of the pilot's own, the runway the tower gave."""
        runway = self.approach_runway or self.landing_runway
        course = _runway_course(self.destination, runway)
        if course is None:
            return None
        return self.engine._threshold(self.destination, runway), course

    def _final_position(self):
        final = self._final()
        return locate(self.plane.lat, self.plane.lon, *final) if final else None

    def _wanted_heading(self) -> float:
        plane, (mode, value) = self.plane, self.lateral
        if mode in ("route", "procedure"):
            while value and distance_nm(plane.lat, plane.lon, *value[0]) < WAYPOINT_NM:
                value.pop(0)
            if mode == "procedure" and (self._joins_final() or not value):
                self.lateral = ("final", None)
                return self._wanted_heading()
            if not value:
                return plane.heading
            return bearing(plane.lat, plane.lon, *value[0])
        if mode == "heading":
            return value
        if mode == "intercept":
            position = self._final_position()
            if position.cross_nm < CAPTURE_NM or (self.side is not None and position.side != self.side):
                self.lateral = ("final", None)
                return self._wanted_heading()
            self.side = position.side
            return value
        if mode == "own":
            return self._own_approach_heading()
        if mode == "final":
            final, position = self._final(), self._final_position()
            toward = 1 if position.side == "left" else -1
            return (final[1] + toward * min(30.0, position.cross_nm * 40)) % 360
        return plane.heading

    def _joins_final(self) -> bool:
        final, position = self._final(), self._final_position()
        inbound = abs((self.plane.heading - final[1] + 180) % 360 - 180) < 90  # not crossing it outbound
        return inbound and position.cross_nm < 3 and 2 < position.along_nm <= JOIN_NM + 0.5

    def _own_approach_heading(self) -> float:
        """No vectors: to a point on the extended centreline, then down final."""
        final, position = self._final(), self._final_position()
        if final is None:
            return self.plane.heading
        if position.along_nm >= 4 and position.cross_nm < 1.5:
            self.lateral = ("final", None)
            return self._wanted_heading()
        gate = _on_centreline(final[0], final[1], OWN_FINAL_NM)
        if distance_nm(self.plane.lat, self.plane.lon, *gate) < 1.0:
            self.lateral = ("final", None)
            return self._wanted_heading()
        return bearing(self.plane.lat, self.plane.lon, *gate)

    def _ias(self) -> float:
        plane = self.plane
        ias = 250.0 if plane.feet < 10000 else 290.0
        if self.speed_kt and self.stage == "arrival":
            ias = min(ias, self.speed_kt)
        if self.lateral[0] == "final":
            position = self._final_position()
            ias = 160.0 if position is None or position.along_nm > 4 else LANDING_KT
        elif self.lateral[0] == "own":
            ias = min(ias, 200.0)
        return ias

    def _target_feet(self) -> float:
        """The cleared level; on the approach, the glide path, but never below what was cleared before established."""
        plane = self.plane
        target = self.cleared if self.cleared is not None else plane.feet
        elevation = self.destination.elev_ft
        if not self.in_contact and not plane.on_ground and self.engine.flight.has_been_airborne:
            target = min(target, self._own_descent(plane, elevation))
        if self.lateral[0] == "own":
            target = min(target, elevation + 3000)
        elif self.lateral[0] == "procedure" and len(self.lateral[1]) <= 1:
            # Past the initial fix, cleared for the approach: down the procedure's profile, about three degrees.
            joining = self.lateral[1][0] if self.lateral[1] else (plane.lat, plane.lon)
            remaining = distance_nm(plane.lat, plane.lon, *joining) + JOIN_NM
            target = min(target, elevation + 50 + remaining * GLIDE_FT_PER_NM)
        elif self.lateral[0] == "final":
            position = self._final_position()
            if position.cross_nm < 1.0:
                target = min(target, elevation + 50 + max(position.along_nm, 0.0) * GLIDE_FT_PER_NM)
            elif self.approach_runway is None:
                target = min(target, elevation + 3000)
        return target

    def _own_descent(self, plane: Aircraft, elevation: float) -> float:
        """Where the sim's data has no controller (a hole in the centres' coverage), nobody gives a descent: the pilot
        starts down on its own on a three degree path, to the minimum safe altitude around the destination."""
        navfix = self.engine.navfix
        safe = (navfix.airport_safe_ft(self.destination.ident) if navfix else None) or 0
        floor = max(elevation + 4000, safe + 500)
        remaining = distance_nm(plane.lat, plane.lon, self.destination.lat, self.destination.lon)
        if remaining > (plane.feet - floor) / GLIDE_FT_PER_NM + 15:
            return plane.feet if self.cleared is None else self.cleared
        return floor + max(remaining - 15, 0) * GLIDE_FT_PER_NM

    def _fly_in_the_air(self) -> None:
        plane, step = self.plane, self.step
        plane.heading = _turned(plane.heading, self._wanted_heading(), TURN_DEG_S * step)
        plane.knots = min(480.0, self._ias() * (1 + plane.feet / 1000 * 0.017))
        miles = plane.knots * step / 3600
        plane.lat, plane.lon = _moved(plane.lat, plane.lon, plane.heading, miles)
        target = self._target_feet()
        climbing = 2500.0 if plane.feet < 10000 else 1800.0
        descending = 1500.0 if self.lateral[0] == "final" else 2500.0  # no more than a stable approach allows
        change = max(-descending * step / 60, min(climbing * step / 60, target - plane.feet))
        plane.feet += change
        plane.vs_fpm = change * 60 / step
        if plane.feet - self._ground_elevation() < 0:
            self.problem("flown into the ground", f"{plane.feet:,.0f} ft, as cleared", stop=True)
        if self.stage == "arrival":
            self.arrival_nm += miles
            if self.arrival_nm > ARRIVAL_TOO_LONG_NM and self.lateral[0] != "final":
                self.problem(
                    "vectors never converge", f"{self.arrival_nm:.0f} NM with the destination without reaching final", stop=True
                )
        self._check_destination_controller()
        if self.lateral[0] == "final":
            self._check_final()

    def _check_destination_controller(self) -> None:
        """Close in, the flight must be with the destination's own approach or tower."""
        plane, station = self.plane, self.station
        remaining = distance_nm(plane.lat, plane.lon, self.destination.lat, self.destination.lon)
        ours = station is not None and station.airport is not None and station.airport.ident == self.destination.ident
        if remaining > HANDED_BY_NM or ours or self.handed_to:
            return
        where = station.callsign if station else "nobody"
        self.problem("never handed to the destination", f"still with {where} {remaining:.0f} NM out", stop=True)

    def _check_final(self) -> None:
        position = self._final_position()
        station = self.station
        if position.along_nm < 4 and position.cross_nm < 1 and station is not None and station.kind != "tower":
            self.problem("not handed to tower", f"{position.along_nm:.1f} NM out, still with {station.callsign}", stop=True)
            return
        if self.landing_runway and self.landing_runway != self.approach_runway and self.approach_runway:
            if position.along_nm < 2:
                self._land()  # circling: not flown, see the docs
            return
        if position.along_nm <= 0.2:
            low = self.plane.feet < self.destination.elev_ft + 500
            if position.along_nm > -0.5 and position.cross_nm < 0.5 and low:
                self._land()
            else:
                # Over the threshold off the centreline or high: the approach did not bring the aircraft to the runway.
                self.problem(
                    "no way to land",
                    f"over the threshold {position.cross_nm:.1f} NM off, at {self.plane.feet:,.0f} ft",
                    stop=True,
                )

    def _land(self) -> None:
        plane = self.plane
        information = self.station is not None and vfr.service(self.station) is not None  # lands at its own discretion
        runway = self.landing_runway
        if self.landing_cleared is None and not information:
            self.problem("landed without a landing clearance", f"runway {runway}", stop=True)
            return
        course = _runway_course(self.destination, runway)
        if course is None:
            self.problem("no runway to land on", f"runway {runway} at {self.destination.ident}", stop=True)
            return
        if runway != self.approach_runway and self.approach_runway:
            # Circling to another runway is not flown: the aircraft is put on that runway's threshold.
            plane.lat, plane.lon = _moved(*self.engine._threshold(self.destination, runway), course, 0.15)
        plane.heading, plane.feet, plane.on_ground, plane.vs_fpm = course, self.destination.elev_ft, True, 0.0
        plane.knots = LANDING_KT - 10
        self.lateral = ("hold", None)
        self.note("pilot", f"landed runway {runway}")
        self.set_stage("landing roll")


def fly_full(db: AirportDB, shared: dict, origin_ident: str, destination_ident: str) -> tuple[list[tuple[str, str]], list[str]]:
    """One flight from a stand at the origin to a stand at the destination: (problems as (kind, detail), radio work)."""
    return GateToGate(db, shared, origin_ident, destination_ident).run()


def sweep_full(db: AirportDB, pairs: list[tuple[str, str]], progress=None) -> tuple[list[tuple[str, list, list]], float]:
    """Fly each pair gate to gate. Returns ([(flight, problems, radio work)], seconds)."""
    shared = shared_data(db)
    started = time.perf_counter()
    results = []
    for number, (origin, destination) in enumerate(pairs):
        if progress and number % 25 == 0:
            progress(f"{number}/{len(pairs)} {origin} to {destination}")
        problems, said = fly_full(db, shared, origin, destination)
        results.append((f"{origin}-{destination}", problems, said))
    return results, time.perf_counter() - started


def full_summary(results: list[tuple[str, list, list]], seconds: float) -> str:
    """Problems grouped by kind, most common first, with example flights and the first one's detail."""
    failed = sum(1 for _, problems, _ in results if problems)
    lines = [f"{len(results)} flights gate to gate in {seconds:.0f} s, {failed} with a problem"]
    kinds = Counter(kind for _, problems, _ in results for kind in {kind for kind, _ in problems})
    for kind, count in kinds.most_common():
        flights = [(name, detail) for name, problems, _ in results for found, detail in problems if found == kind]
        names = list(dict.fromkeys(name for name, _ in flights))
        lines.append(f"  x{count:<4} {kind}   e.g. {' '.join(names[:6])}")
        lines.append("        " + flights[0][1].replace("\n", "\n  "))
    return "\n".join(lines)
