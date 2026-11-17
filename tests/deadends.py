"""Where the controller has no answer: every intent, on every kind of station, in every main state of a flight.

`engine.handle` returning None means the rules engine has no rule for that call, and the app falls back to a
free-form reply from the language model, which may only inform or decline. For the pilot that is a dead end.
This builds the whole matrix on invented airports, without X-Plane and without a language model:

    uv run python -m tests.deadends            # the matrix, state by state, and the counts
    uv run python -m tests.deadends --missing  # only the unanswered calls a pilot plausibly makes

A cell is two characters, the instrument flight then the visual one: "." a specific answer, "r" a bare
"roger", "u" a bare "unable ..." or "say again", "c" "contact ..." (sent to another controller), "s" silence (a
broadcast, an acknowledgement), "X" no rule, "!" an answer that is FALSE, blank where the station cannot be heard.

An answer counts only when it is true. Every reply is checked against the flight's state at that moment: a level
after "climb", "descend" or "maintain" must be the level the flight is now cleared to, a climb is never given to an
aircraft above that level nor a descent to one below it, a heading, squawk or speed said must be the one assigned,
a frequency said must be the one handed off to, a runway cleared for takeoff or landing must exist and suit the wind,
and an approach is never "cleared" on the ground. Each reply also records the rule (the function) that made it.
`tests/test_answers.py` holds the false answers and the plausible gaps at zero.
"""

from __future__ import annotations

import copy
import random
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from proatc import engine as engine_module
from proatc.answers import acceptable_runway
from proatc.atis import Issue
from proatc.centers import CenterIndex, parse_atc
from proatc.engine import Engine
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
    "parked", "cleared", "taxiing", "holding short", "departing", "climbing", "cruising", "descending",
    "approach, before vectors", "approach, vectors", "diverting", "final", "landed", "taxiing in",
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
    if rules == "IFR":
        # A diversion back to Westfield from the vectors: measured, then the flight goes on to land as before.
        saved = copy.deepcopy(engine.flight)
        say(engine, Intent("request_diversion", destination="EGXX"), approach, near)
        snap("diverting", {"approach": (approach, near), "centre": (east, near)})
        engine.flight = saved
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
    if intent in SITUATIONAL:
        return False
    if intent in GROUND_ONLY and state not in ON_GROUND:
        return False
    return not (intent in AIR_ONLY and state in ON_GROUND)


@dataclass
class Matrix:
    cells: dict = field(default_factory=dict)  # (call, state, kind, rules) -> "." | "r" | "u" | "c" | "s" | "X" | "!"
    texts: dict = field(default_factory=dict)  # same key -> what the controller said
    rules: dict = field(default_factory=dict)  # same key -> the function that made the reply ("answers.unable")
    false: dict = field(default_factory=dict)  # same key -> why the reply is not true

    def unanswered(self, only_plausible: bool = False) -> list[tuple]:
        return [key for key, mark in self.cells.items() if mark == "X" and (not only_plausible or plausible(key[0], key[1]))]

    def kinds(self, only_plausible: bool = True) -> dict[str, int]:
        counts = {name: 0 for name in KIND_NAMES.values()}
        for key, mark in self.cells.items():
            if not only_plausible or plausible(key[0], key[1]):
                counts[KIND_NAMES[mark]] += 1
        return counts


KIND_NAMES = {".": "specific", "r": "roger", "u": "unable", "c": "contact", "s": "silence", "X": "no rule", "!": "false"}
NUMBER = {
    word: str(i) for i, word in enumerate(["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "niner"])
}


def _digits(words: list[str]) -> tuple[str, int]:
    """The digits spoken at the start of these words, and how many words they took."""
    taken = 0
    while taken < len(words) and words[taken] in NUMBER:
        taken += 1
    return "".join(NUMBER[word] for word in words[:taken]), taken


def spoken_level(words: list[str]) -> int | None:
    """'flight level two four zero' -> 24000; 'altitude three thousand five hundred feet' or 'three thousand
    five hundred' -> 3500. None when the words are not a level."""
    if words[:2] == ["flight", "level"]:
        value, taken = _digits(words[2:])
        return int(value) * 100 if taken == 3 else None
    if words[:1] == ["altitude"]:
        words = words[1:]
    thousands, taken = _digits(words)
    feet = 0
    if taken and words[taken : taken + 1] == ["thousand"]:
        feet, words = int(thousands) * 1000, words[taken + 1 :]
        hundreds, taken = _digits(words)
    if taken == 1 and words[1:2] == ["hundred"]:
        feet += int(hundreds if feet else thousands) * 100
    return feet or None


def _words(text: str) -> list[str]:
    return re.sub(r"[^a-z ]", " ", text.lower()).split()


def truth(engine, station, raw: dict, issue, reply) -> list[str]:
    """What in this reply is not true of the flight now. Empty when everything said holds."""
    flight, problems = engine.flight, []
    words = _words(reply.text)
    airborne = not bool(raw.get("on_ground", True))
    feet = raw.get("elevation_m", 0.0) / 0.3048
    for at_word, word in enumerate(words):
        if word not in ("climb", "descend", "maintain") or (at_word and words[at_word - 1] == "and"):
            continue
        rest = words[at_word + 1 :]
        if rest[:2] == ["and", "maintain"]:
            rest = rest[2:]
        level = spoken_level(rest)
        if level is None:
            continue
        if flight.cleared_ft is not None and abs(level - flight.cleared_ft) > 50:
            problems.append(f"says {word} {level} but the flight is cleared to {flight.cleared_ft}")
        if airborne and word == "climb" and level < feet - 300:
            problems.append(f"climb to {level} for an aircraft at {feet:.0f} ft")
        if airborne and word == "descend" and level > feet + 300:
            problems.append(f"descend to {level} for an aircraft at {feet:.0f} ft")
    for match in re.finditer(r"\bheading ((?:\w+ ){2}\w+)", reply.text):
        heading, taken = _digits(match.group(1).split())
        if taken == 3 and int(heading) % 360 not in {
            (getattr(flight, "heading_assigned", None) or -1) % 360,
            (getattr(flight.arrival, "heading", None) or -1) % 360,
        }:
            problems.append(f"heading {heading} but {getattr(flight, 'heading_assigned', None)} is assigned")
    for match in re.finditer(r"\bsquawk ((?:\w+ ){3}\w+)", reply.text):
        code, taken = _digits(match.group(1).split())
        if taken == 4 and code != flight.squawk:
            problems.append(f"squawk {code} but {flight.squawk} is assigned")
    for match in re.finditer(
        r"(?<!restriction )\b(?:reduce speed to|reduce speed|speed|maintain) ((?:\w+ ){2}\w+) knots(?! or less)", reply.text
    ):
        knots, taken = _digits(match.group(1).split())
        if taken == 3 and int(knots) != getattr(flight.arrival, "speed_kt", None):
            problems.append(f"speed {knots} but {getattr(flight.arrival, 'speed_kt', None)} is assigned")
    for match in re.finditer(r"contact [A-Z][\w ]*? ((?:one|two) (?:\w+ ){2}decimal(?: \w+)+)", reply.text):
        before, after = match.group(1).split(" decimal ")
        said, _ = _digits(before.split())
        decimals, _ = _digits(after.split())
        khz = int(said) * 1000 + int(decimals.ljust(3, "0")[:3])
        # The hand-off just given, or one given earlier that "say again" repeats word for word.
        given = [entry[1].get("frequency") for entry in flight.last_instruction.values()]
        told = [value for value in (reply.expect.get("frequency"), flight.next_frequency, *given) if value]
        # Or a station of this airport ("readback correct, contact Ground when ready"): a real frequency of its own.
        told += [f"{freq.khz / 1000:.3f}" for freq in (station.airport.freqs if station.airport else [])]
        if not any(abs(float(value) * 1000 - khz) <= 6 for value in told):
            problems.append(f"contact on {khz} kHz, which was not the hand-off")
    cleared = re.search(
        r"runway ((?:\w+ ){1}\w+(?: left| right| center)?), cleared (?:to land|for takeoff|touch and go)", reply.text
    )
    if cleared and station.airport:
        ident = next((end for rw in station.airport.runways for end, _ in rw.ends() if say_runway(end) == cleared.group(1)), None)
        if ident is None:
            problems.append(f"cleared on runway {cleared.group(1)}, which this airport does not have")
        elif issue and acceptable_runway(station.airport, ident, issue.weather) not in (None, "runway too short"):
            problems.append(f"cleared on runway {ident} against the wind")
    if "cleared" in words and "approach" in words and not airborne and " approach runway" in reply.text:
        problems.append("an approach is cleared on the ground")
    return problems


def say_runway(ident: str) -> str:
    from proatc import phraseology

    return phraseology.runway(ident)


def kind_of(reply, station, callsign: str) -> str:
    """'.' specific, 'r' roger, 'u' unable, 'c' contact, 's' silence."""
    if not reply.text:
        return "s"
    body = reply.text.removeprefix(f"{callsign}, ").removeprefix(f"{station.callsign}, ")
    for lead in ("identified, ", "radar contact, "):
        body = body.removeprefix(lead)
    if body in ("roger", "identified", "radar contact"):
        return "r"
    if body in ("say again", "unable, no information") or re.fullmatch(r"unable [a-z ]+", body):
        return "u"
    if body.startswith("contact "):
        return "c"
    return "."


class _Traced(engine_module.Reply):
    """A reply that remembers which function made it: the rule behind every answer in the matrix."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        frame = sys._getframe(1)
        while frame.f_back is not None and frame.f_code.co_name in ("_reply", "__init__"):
            frame = frame.f_back
        self.rule = f"{frame.f_globals.get('__name__', '').rsplit('.', 1)[-1]}.{frame.f_code.co_name}"


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
    original, engine_module.Reply = engine_module.Reply, _Traced
    try:
        _fill(matrix, engine, flights)
    finally:
        engine_module.Reply = original
    return matrix


def _fill(matrix: Matrix, engine, flights: dict) -> None:
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
                        matrix.texts[key] = reply.text if reply else None
                        if reply is None:
                            matrix.cells[key] = "X"
                            continue
                        matrix.rules[key] = getattr(reply, "rule", "?")
                        wrong = truth(engine, station, raw, issue, reply)
                        if wrong:
                            matrix.false[key] = wrong
                        callsign = engine._callsign(raw)
                        matrix.cells[key] = "!" if wrong else kind_of(reply, station, callsign)


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
    kinds = ", ".join(f"{name} {count}" for name, count in matrix.kinds().items())
    return (
        f"{total} cells, {every} unanswered; {counted} a pilot plausibly makes, {likely} of them unanswered\n"
        f"plausible cells by kind of answer: {kinds}; false answers in all cells: {len(matrix.false)}"
    )


def main(argv: list[str]) -> None:
    matrix = measure()
    if "--missing" in argv:
        for name, state, kind, rules in sorted(matrix.unanswered(only_plausible=True), key=lambda k: (k[0], k[1], k[2])):
            print(f"{name:34} {state:26} {kind:10} {rules}")
    elif "--false" in argv:
        for key, why in sorted(matrix.false.items()):
            print(f"{' / '.join(key)}: {'; '.join(why)}\n    {matrix.texts[key]}  [{matrix.rules[key]}]")
    else:
        render(matrix)
    print()
    print(summary(matrix))


if __name__ == "__main__":
    main(sys.argv[1:])
