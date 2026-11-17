"""Where the controller has no answer: every intent, on every kind of station, in every main state of a flight.

`engine.handle` returning None means the rules engine has no rule for that call, and the app falls back to a
free-form reply from the language model, which may only inform or decline. For the pilot that is a dead end.
This builds the whole matrix on invented airports, without X-Plane and without a language model:

    uv run python -m tests.deadends            # the matrix, state by state, and the counts
    uv run python -m tests.deadends --missing  # only the unanswered calls a pilot plausibly makes

A cell is two characters, the instrument flight then the visual one: "." answered, "s" answered with
silence (a broadcast, an acknowledgement), "X" no rule, blank where the station cannot be heard in that state.
`tests/test_deadends.py` holds the count down.
"""

from __future__ import annotations

import copy
import random
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from proatc import questions
from proatc.atis import Issue
from proatc.centers import CenterIndex, parse_atc
from proatc.engine import Engine, Reply
from proatc.flightplan import FlightPlan
from proatc.intents import INTENTS, Intent
from proatc.navdata import AirportDB, build_index
from proatc.procedures import parse_cifp
from proatc.weather import Weather
from tests.test_airways import OCEAN
from tests.test_ifr import ATC, CIFP_EAST, CIFP_WEST

RUNWAY = "100 45.00 1 1 0.25 1 3 0  09 {lat:.4f} {west:.4f} 0 0 3 2 1 0 27 {lat:.4f} {east:.4f} 0 0 3 2 1 0"
APT = "\n".join(
    [
        "A",
        "1200 test",
        "",
        "1    100 0 0 EGXX Westfield",
        RUNWAY.format(lat=51.0, west=-1.02, east=-0.98),
        "1052 121900 Westfield Delivery",
        "1053 121700 Westfield Ground",
        "1054 118100 Westfield Tower",
        "1056 125500 Westfield Departure",
        "",
        "1    300 0 0 LFXX Eastfield",
        RUNWAY.format(lat=51.0, west=5.33, east=5.37),
        "1053 121800 Eastfield Ground",
        "1054 119100 Eastfield Tower",
        "1055 124300 Eastfield Approach",
        "",
        "1    50 0 0 ESXX Fjordby",
        RUNWAY.format(lat=60.0, west=14.98, east=15.02),
        "1054 119100 AFIS",
        "",
        "1    2000 0 0 KXAA Smallville Muni",
        RUNWAY.format(lat=41.0, west=-100.02, east=-99.98),
        "1051 122800 CTAF/UNICOM",
        "",
        # The same two airports in the United States, for FAA wording.
        "1    700 0 0 KWST Westtown",
        RUNWAY.format(lat=41.0, west=-90.02, east=-89.98),
        "1052 121900 Westtown Delivery",
        "1053 121700 Westtown Ground",
        "1054 118100 Westtown Tower",
        "1056 125500 Westtown Departure",
        "",
        "1    700 0 0 KEST Easttown",
        RUNWAY.format(lat=41.0, west=-88.02, east=-87.98),
        "1053 121800 Easttown Ground",
        "1054 119100 Easttown Tower",
        "1055 124300 Easttown Approach",
    ]
)
ISSUE = Issue("C", Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1013.2), 12 * 3600)
PLAN = FlightPlan("EGXX", "LFXX", "ALFAX UL9 BRAVO UM605 DELTA".split(), 36000, source="simbrief")
KINDS = ("delivery", "ground", "tower", "departure", "approach", "centre", "oceanic", "unicom", "afis")
STATES = (
    "parked", "cleared", "taxiing", "holding short", "departing", "climbing", "climbing, asked", "cruising", "descending",
    "approach, before vectors", "approach, vectors", "final", "landed", "taxiing in",
)  # fmt: skip
ON_GROUND = {"parked", "cleared", "taxiing", "holding short", "departing", "landed", "taxiing in"}
# The values a pilot would say with each call, where the call needs one.
SAID = {
    "request_direct": {"fix": "BRAVO"},
    "request_approach": {"approach": "RNAV"},
    "request_deviation": {"direction": "left"},
    "request_diversion": {"destination": "EGXX"},
    "request_runway": {"runway": "09"},
    "position_report": {"position": "downwind"},
}
# Calls made only on the ground, or only in the air: elsewhere they are not what a pilot says.
GROUND_ONLY = {
    "request_clearance", "request_pushback", "request_taxi", "ready_for_departure", "request_crossing",
    "request_progressive", "runway_vacated",
}  # fmt: skip
AIR_ONLY = {
    "inbound", "position_report", "request_climb", "request_descent", "established", "request_direct", "request_hold",
    "cancel_hold", "request_deviation", "clear_of_weather", "request_approach", "request_visual", "request_diversion",
    "request_transit", "request_low_approach", "request_touch_and_go", "going_around", "cancel_flight_following",
}  # fmt: skip
# Said only in particular circumstances: an emergency to cancel, a hold to leave, a blind call to other traffic.
SITUATIONAL = {"cancel_emergency", "cancel_hold", "clear_of_weather", "cancel_flight_following", "traffic_call", "other"}
# A yes or a no answers a question: plausible where the controller has just asked one.
ANSWERS = {"affirm", "negative"}
# What departure asked in the state "climbing, asked": the call from a real flight that had no answer.
OFF_TRACK = "{callsign}, you appear to be six miles off the departure track, confirm you are following the departure"


def at(lat, lon, feet=0.0, heading=270.0, on_ground=False, speed=120.0):
    return {"tail_number": "G-ABCD", "lat": lat, "lon": lon, "on_ground": int(on_ground), "elevation_m": feet * 0.3048,
            "agl_m": 0.0 if on_ground else feet * 0.3048, "groundspeed_ms": 0.0 if on_ground else speed,
            "heading_mag": heading, "heading_true": heading, "squawk": 7000, "wx_source": 0}  # fmt: skip


@dataclass
class World:
    db: AirportDB
    centers: CenterIndex
    snapshots: dict = field(default_factory=dict)  # (world, rules, state) -> (flight, {kind: (station, raw)})

    def engine(self) -> Engine:
        procedures = {"EGXX": parse_cifp(CIFP_WEST), "LFXX": parse_cifp(CIFP_EAST)}
        return Engine(self.db, seed=1, procedures=lambda ident: procedures.get(ident, parse_cifp([])), centers=self.centers)

    def station(self, ident: str, kind: str):
        return self.db.station_of(self.db.airport(ident), kind)


def build_world(folder: Path) -> World:
    apt = folder / "apt.dat"
    apt.write_text(APT, encoding="utf-8")
    build_index(apt, folder / "a.sqlite")
    return World(AirportDB(folder / "a.sqlite"), CenterIndex(parse_atc(ATC + OCEAN)))


def read_back(engine: Engine, station, raw: dict) -> None:
    pending = engine.flight.pending
    if pending is not None:
        engine.handle(Intent.from_dict(engine._readback_intent(pending.expect)), station, ISSUE, raw)


def say(engine: Engine, intent: Intent, station, raw: dict, issue=ISSUE) -> None:
    engine.handle(intent, station, issue, raw)
    read_back(engine, station, raw)


def ask(engine: Engine, station, text: str) -> None:
    """The controller asks the pilot something, as `Engine.tick` records it."""
    engine.flight.last_said[station.key] = text
    questions.record(engine, station.key, Reply(text))


def controlled(world: World, rules: str) -> dict:
    """An instrument or visual flight from Westfield to Eastfield, every station talked to on the way."""
    engine, snaps = world.engine(), {}
    if rules == "IFR":
        engine.set_plan(PLAN)
    delivery, ground, tower = (world.station("EGXX", kind) for kind in ("delivery", "ground", "tower"))
    departure = world.station("EGXX", "departure")
    west = world.centers.on_frequency(127500, 51.0, 0.0)
    east = world.centers.on_frequency(132100, 51.0, 3.0)
    ocean = world.centers.on_frequency(127900, 51.0, -8.0)
    approach, arrival_tower, arrival_ground = (world.station("LFXX", kind) for kind in ("approach", "tower", "ground"))
    cruise_ft = 36000 if rules == "IFR" else 5500

    def snap(state, stations):
        snaps[state] = (copy.deepcopy(engine.flight), stations)

    parked = at(51.003, -1.0, 100, on_ground=True)
    engine.tick(ground, ISSUE, parked)
    snap("parked", {"delivery": (delivery, parked), "ground": (ground, parked), "tower": (tower, parked)})
    say(engine, Intent("request_clearance", flight_rules=rules), delivery, parked)
    snap("cleared", {"delivery": (delivery, parked), "ground": (ground, parked), "tower": (tower, parked)})
    engine.handle(Intent("request_pushback"), ground, ISSUE, parked)
    moving = {**parked, "groundspeed_ms": 5.0}
    say(engine, Intent("request_taxi"), ground, moving)
    snap("taxiing", {"delivery": (delivery, moving), "ground": (ground, moving), "tower": (tower, moving)})
    short = at(51.0006, -0.979, 100, on_ground=True)
    say(engine, Intent("ready_for_departure"), ground, short)
    snap("holding short", {"delivery": (delivery, short), "ground": (ground, short), "tower": (tower, short)})
    say(engine, Intent("ready_for_departure"), tower, short)
    rolling = {**at(51.0, -0.981, 100, on_ground=True), "groundspeed_ms": 20.0}
    snap("departing", {"ground": (ground, rolling), "tower": (tower, rolling)})
    climbing = at(51.0, -1.12, 3000, heading=270)
    engine.tick(tower, ISSUE, climbing)
    read_back(engine, tower, climbing)
    say(engine, Intent("check_in"), departure, climbing)
    snap("climbing", {"tower": (tower, climbing), "departure": (departure, climbing), "centre": (west, climbing)})
    ask(engine, departure, OFF_TRACK.format(callsign=engine._callsign(climbing)))
    snap("climbing, asked", {"departure": (departure, climbing)})
    engine.flight.question = None
    cruising = at(51.0, 0.5, cruise_ft, heading=90)
    say(engine, Intent("check_in"), west, cruising)
    engine.flight.cleared_ft = cruise_ft
    oceanic = at(51.0, -8.0, cruise_ft, heading=270)
    snap("cruising", {"centre": (west, cruising), "oceanic": (ocean, oceanic)})
    descending = at(51.0, 3.5, cruise_ft, heading=90)
    say(engine, Intent("check_in"), east, descending)
    say(engine, Intent("request_descent"), east, descending)
    snap("descending", {"centre": (east, descending), "approach": (approach, descending)})
    near = at(51.0, 4.7, 10000 if rules == "IFR" else 4500, heading=90)
    engine.tick(east, None, near)
    read_back(engine, east, near)
    snap("approach, before vectors", {"centre": (east, near), "approach": (approach, near), "tower": (arrival_tower, near)})
    say(engine, Intent("check_in"), approach, near)
    snap("approach, vectors", {"approach": (approach, near), "tower": (arrival_tower, near)})
    final = at(51.0, 5.43, 1200, heading=270)
    if engine.flight.arrival is not None:
        engine.flight.arrival.stage = "intercept"
        say(engine, Intent("established"), approach, final)
    say(engine, Intent("position_report", position="final"), arrival_tower, final)
    snap("final", {"tower": (arrival_tower, final), "approach": (approach, final)})
    landed = {**at(51.0, 5.36, 300, heading=270, on_ground=True), "groundspeed_ms": 15.0}
    engine.tick(arrival_tower, ISSUE, landed)
    read_back(engine, arrival_tower, landed)
    snap("landed", {"tower": (arrival_tower, landed), "ground": (arrival_ground, landed)})
    taxiing_in = {**at(51.002, 5.35, 300, on_ground=True), "groundspeed_ms": 5.0}
    say(engine, Intent("runway_vacated"), arrival_ground, taxiing_in)
    snap("taxiing in", {"ground": (arrival_ground, taxiing_in), "tower": (arrival_tower, taxiing_in)})
    return snaps


def uncontrolled(world: World, rules: str, ident: str, kind: str) -> dict:
    """The same flight at an airfield with an information service, or with nobody but a unicom operator."""
    engine, snaps = world.engine(), {}
    if rules == "IFR":
        engine.set_plan(FlightPlan(ident, "LFXX", [], 9000))
    airport = world.db.airport(ident)
    station = world.db.station_of(airport, "tower" if kind == "afis" else "unicom")
    lat, lon, elev = airport.lat, airport.lon, airport.elev_ft

    def snap(state, raw):
        snaps[state] = (copy.deepcopy(engine.flight), {kind: (station, raw)})

    parked = at(lat + 0.003, lon, elev, on_ground=True)
    engine.tick(station, ISSUE, parked)
    snap("parked", parked)
    moving = {**parked, "groundspeed_ms": 5.0}
    say(engine, Intent("request_taxi"), station, moving)
    snap("taxiing", moving)
    short = at(lat + 0.0006, lon + 0.021, elev, on_ground=True)
    snap("holding short", short)
    say(engine, Intent("ready_for_departure"), station, short)
    rolling = {**at(lat, lon + 0.019, elev, on_ground=True), "groundspeed_ms": 20.0}
    snap("departing", rolling)
    climbing = at(lat, lon - 0.1, elev + 1500)
    engine.tick(station, ISSUE, climbing)
    snap("climbing", climbing)
    inbound = at(lat - 0.13, lon, elev + 2000, heading=0)
    say(engine, Intent("inbound"), station, inbound)
    snap("approach, before vectors", inbound)
    final = at(lat, lon + 0.06, elev + 800)
    say(engine, Intent("position_report", position="final"), station, final)
    snap("final", final)
    landed = {**at(lat, lon + 0.005, elev, on_ground=True), "groundspeed_ms": 15.0}
    engine.tick(station, ISSUE, landed)
    snap("landed", landed)
    taxiing_in = {**at(lat + 0.002, lon, elev, on_ground=True), "groundspeed_ms": 5.0}
    say(engine, Intent("runway_vacated"), station, taxiing_in)
    snap("taxiing in", taxiing_in)
    return snaps


def calls() -> list[tuple[str, Intent]]:
    """Every intent the app knows, once for each item it can be about."""
    from proatc import intents

    items = getattr(intents, "ITEMS", ())
    found = []
    for name in INTENTS:
        if name == "request_information" and items:
            found += [(f"{name}:{item}", Intent(name, item=item)) for item in items]
        else:
            found.append((name, Intent(name, **SAID.get(name, {}))))
    return found


def plausible(name: str, state: str) -> bool:
    """Whether a pilot would make this call in this state: the cells a dead end matters in."""
    intent = name.split(":")[0]
    if intent in ANSWERS:
        return state.endswith("asked")
    if intent in SITUATIONAL:
        return False
    if intent in GROUND_ONLY and state not in ON_GROUND:
        return False
    return not (intent in AIR_ONLY and state in ON_GROUND)


@dataclass
class Matrix:
    cells: dict = field(default_factory=dict)  # (call, state, kind, rules) -> "." | "s" | "X"
    texts: dict = field(default_factory=dict)  # same key -> what the controller said

    def unanswered(self, only_plausible: bool = False) -> list[tuple]:
        return [key for key, mark in self.cells.items() if mark == "X" and (not only_plausible or plausible(key[0], key[1]))]


def measure(folder: Path | None = None) -> Matrix:
    if folder is None:
        # The airport database stays open until the process ends: Windows cannot delete it before then.
        folder = Path(tempfile.mkdtemp(prefix="proatc-deadends-"))
    world = build_world(folder)
    flights = {}
    for rules in ("IFR", "VFR"):
        flights[rules] = [controlled(world, rules), uncontrolled(world, rules, "ESXX", "afis"),
                          uncontrolled(world, rules, "KXAA", "unicom")]  # fmt: skip
    matrix = Matrix()
    engine = world.engine()
    for rules, worlds in flights.items():
        for snaps in worlds:
            for state, (flight, stations) in snaps.items():
                for kind, (station, raw) in stations.items():
                    if station is None:
                        continue
                    for name, intent in calls():
                        if name == "request_clearance":
                            intent = Intent(name, flight_rules=rules)
                        engine.flight, engine._rng = copy.deepcopy(flight), random.Random(1)
                        engine._traffic_seen.clear()
                        issue = ISSUE if getattr(station, "airport", None) else None
                        reply = engine.handle(copy.copy(intent), station, issue, dict(raw))
                        key = (name, state, kind, rules)
                        matrix.cells[key] = "X" if reply is None else ("." if reply.text else "s")
                        matrix.texts[key] = reply.text if reply else None
    return matrix


def render(matrix: Matrix, report=print) -> None:
    names = [name for name, _ in calls()]
    for state in STATES:
        kinds = [kind for kind in KINDS if any((n, state, kind, r) in matrix.cells for n in names for r in ("IFR", "VFR"))]
        if not kinds:
            continue
        report(f"\n== {state} ==")
        report(f"{'':34}" + "".join(f"{kind[:9]:>10}" for kind in kinds))
        for name in names:
            row = "".join(
                f"{''.join(matrix.cells.get((name, state, kind, rules), ' ') for rules in ('IFR', 'VFR')):>10}" for kind in kinds
            )
            report(f"{name:34}{row}")


def summary(matrix: Matrix) -> str:
    total = len(matrix.cells)
    every = len(matrix.unanswered())
    likely = len(matrix.unanswered(only_plausible=True))
    counted = sum(1 for key in matrix.cells if plausible(key[0], key[1]))
    return f"{total} cells, {every} unanswered; {counted} a pilot plausibly makes, {likely} of them unanswered"


def main(argv: list[str]) -> None:
    matrix = measure()
    if "--missing" in argv:
        for name, state, kind, rules in sorted(matrix.unanswered(only_plausible=True), key=lambda k: (k[0], k[1], k[2])):
            print(f"{name:34} {state:26} {kind:10} {rules}")
    else:
        render(matrix)
    print()
    print(summary(matrix))


if __name__ == "__main__":
    main(sys.argv[1:])
