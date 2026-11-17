"""Golden flights: whole flights flown through the engine, compared word for word with a saved transcript.

A unit test pins one answer. A golden flight pins a hundred of them in order, with the state each
leaves behind, so a change anywhere in the engine that alters what a controller says shows up as a
diff in a file a person can read (tests/golden/*.txt).

When a difference is intended, look at the diff, then regenerate:

    PROATC_UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py

and commit the changed transcripts with the code. A reviewer then sees exactly what the change does
to a flight. Also here: a saved flight log replayed through the engine gives the same answers.
"""

import os
from dataclasses import asdict
from pathlib import Path

import pytest

from proatc.airspace import AirspaceIndex, parse_openair
from proatc.atis import AtisBook, Issue
from proatc.centers import CenterIndex, parse_atc
from proatc.engine import Engine
from proatc.flightlog import FlightLog
from proatc.flightplan import FlightPlan
from proatc.intents import Intent
from proatc.navdata import AirportDB, build_index
from proatc.procedures import parse_cifp
from proatc.radio import observe
from proatc.replay import replay, snapshot
from proatc.weather import Cloud, Weather
from tests.test_ifr import APT, ATC
from tests.test_procedures import FIXES, SID, STAR
from tests.test_realism import CROSSING
from tests.test_vfr import FIELDS, OPENAIR

GOLDEN = Path(__file__).parent / "golden"
WESTERLY = Issue("C", Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1013.2), 12 * 3600)
MURK = Issue("D", Weather(wind_dir_true=270, wind_kt=6, visibility_sm=2, clouds=[Cloud(0.8, 700)], qnh_hpa=1005), 12 * 3600)


def build(folder: Path, name: str, apt: str) -> AirportDB:
    path = folder / f"{name}.dat"
    path.write_text(apt, encoding="utf-8")
    build_index(path, folder / f"{name}.sqlite")
    return AirportDB(folder / f"{name}.sqlite")


class Flight:
    """Flies a script through an engine and writes down everything that is said, and the state after it."""

    def __init__(self, engine: Engine, db: AirportDB):
        self.engine, self.db, self.lines, self.clock = engine, db, [], 0.0
        engine.now = lambda: self.clock

    def note(self, text: str) -> None:
        self.lines.append(f"\n-- {text}")

    def station(self, ident: str, kind: str):
        return self.db.station_of(self.db.airport(ident), kind)

    def _state(self) -> str:
        keep = ("phase", "assigned_runway", "cleared_level", "approach", "heading", "hold_short", "stand", "service", "holding")
        summary = self.engine.summary()
        return "  ".join(f"{key}={summary[key]}" for key in keep if key in summary)

    def _readback(self, station, reply, raw, issue) -> None:
        """Read back whatever must be read back, with the button the app offers, as the copilot does."""
        if not reply or not reply.expect or self.engine.flight.pending is None:
            return
        answer = self.engine.handle(Intent.from_dict(self.engine._readback_intent(reply.expect)), station, issue, raw)
        if answer is not None and answer.text:
            self.lines.append(f"  ATC      {answer.text}")

    def call(self, station, raw: dict, name: str, issue=WESTERLY, **fields) -> None:
        self.clock += 20
        intent = Intent(name, **fields)
        said = " ".join(
            f"{key}={value}" for key, value in asdict(intent).items() if value not in (None, False) and key != "intent"
        )
        self.lines.append(f"PILOT -> {station.callsign}: {name}{' ' + said if said else ''}")
        reply = self.engine.handle(intent, station, issue, raw)
        if reply is None:
            self.lines.append("  ATC      (no rule)")
        else:
            self.lines.append(f"  ATC      {reply.text or '(silent' + (': ' + reply.note if reply.note else '') + ')'}")
            self._readback(station, reply, raw, issue)
        self.lines.append(f"           [{self._state()}]")

    def fly(self, station, raws: list[dict], issue=WESTERLY) -> None:
        """Look at the aircraft at each of these moments and let the controller speak if there is something to say."""
        for raw in raws:
            self.clock += 30
            reply = self.engine.tick(station, issue, raw)
            if reply is not None and reply.text:
                tag = f" ({reply.event})" if reply.event else ""
                self.lines.append(f"  ATC*     {reply.text}{tag}")
                self._readback(station, reply, raw, issue)
                self.lines.append(f"           [{self._state()}]")

    def transcript(self) -> str:
        return "\n".join(self.lines).strip() + "\n"


def at(lat, lon, feet=100.0, heading=270.0, ground=False, ias=250.0, speed=120.0):
    return {
        "tail_number": "G-ABCD", "lat": lat, "lon": lon, "on_ground": int(ground), "elevation_m": feet * 0.3048,
        "agl_m": 0.0 if ground else feet * 0.3048, "groundspeed_ms": 0.0 if ground else speed, "heading_mag": heading,
        "heading_true": heading, "ias_kt": 0.0 if ground else ias, "aircraft_type": "A20N", "zulu_seconds": 43200.0,
    }  # fmt: skip


def compare(name: str, flight: Flight) -> None:
    path = GOLDEN / f"{name}.txt"
    now = flight.transcript()
    if os.environ.get("PROATC_UPDATE_GOLDEN") or not path.exists():
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(now, encoding="utf-8", newline="\n")
        if not os.environ.get("PROATC_UPDATE_GOLDEN"):
            pytest.fail(f"{path.name} did not exist and was written: look at it, and commit it if it is right")
        return
    assert now == path.read_text(encoding="utf-8"), (
        f"the flight in tests/golden/{path.name} no longer goes the same way. If the change is intended, "
        "regenerate with PROATC_UPDATE_GOLDEN=1 and commit the transcript."
    )


def ifr_engine(tmp_path, **options):
    db = build(tmp_path, "ifr", "\n".join(APT))
    procedures = {"EGXX": parse_cifp(SID), "LFXX": parse_cifp(STAR)}
    engine = Engine(
        db, seed=1, procedures=lambda ident: procedures.get(ident, parse_cifp([])), centers=CenterIndex(parse_atc(ATC)),
        navfix=FIXES, **options,
    )  # fmt: skip
    return db, engine


def test_golden_ifr_gate_to_gate(tmp_path):
    db, engine = ifr_engine(tmp_path)
    engine.set_plan(
        FlightPlan(
            "EGXX", "LFXX", "ALFAX UL9 BRAVO UM605 DELTA".split(), 36000, arrival_runway="27", callsign="BAW123", alternate="EGXX"
        )
    )
    flight = Flight(engine, db)
    gate = at(51.0, -1.0, ground=True)
    flight.note("Westfield: clearance, push, taxi, takeoff")
    flight.call(flight.station("EGXX", "delivery"), gate, "request_clearance", flight_rules="IFR", atis="C")
    ground = flight.station("EGXX", "ground")
    flight.call(ground, gate, "request_pushback")
    flight.call(ground, gate, "request_taxi")
    tower = flight.station("EGXX", "tower")
    flight.call(tower, at(51.0, -0.98, ground=True), "ready_for_departure")
    flight.fly(tower, [at(51.0, -1.02, feet=600, ias=160), at(51.0, -1.06, feet=1600, ias=190)])
    flight.note("Departure: the SID and its restrictions, then the centre")
    departure = flight.station("EGXX", "departure")
    flight.call(departure, at(51.0, -1.08, feet=2200, ias=200), "check_in", altitude=5000)
    flight.fly(departure, [at(51.0, lon, feet=feet, ias=ias) for lon, feet, ias in (
        (-1.1, 2600, 210), (-1.3, 4800, 215), (-1.45, 6500, 250), (-1.6, 8200, 250), (-1.8, 11000, 280), (-2.0, 14000, 290),
    )])  # fmt: skip
    west = engine.centers.on_frequency(127500, 51.0, -2.0)
    flight.call(west, at(51.0, -1.9, feet=15000, heading=90, ias=290), "check_in", altitude=14000)
    flight.fly(west, [at(51.0, lon, feet=feet, heading=90, ias=290) for lon, feet in (
        (-1.5, 22000), (-1.0, 23500), (-0.2, 31000), (0.5, 33500), (1.2, 36000), (1.9, 36000), (2.1, 36000),
    )])  # fmt: skip
    flight.note("Eastland Control: cruise, then down the STAR")
    east = engine.centers.on_frequency(132100, 51.0, 3.0)
    flight.call(east, at(51.0, 2.3, feet=36000, heading=90, ias=280), "check_in", altitude=36000)
    flight.fly(east, [at(51.0, lon, feet=feet, heading=270, ias=ias) for lon, feet, ias in (
        (7.95, 36000, 280), (7.8, 30000, 280), (7.6, 20000, 275), (7.4, 19000, 275), (7.1, 13000, 270), (6.9, 12000, 270),
        (6.7, 11500, 260), (6.5, 10000, 250), (6.4, 9000, 245), (6.3, 8000, 240), (6.2, 7000, 230),
    )])  # fmt: skip
    flight.note("Eastfield: the R NAV approach by its initial fix, the tower, the stand")
    approach = flight.station("LFXX", "approach")
    flight.call(approach, at(51.2, 6.15, feet=7000, heading=250, ias=220), "check_in", altitude=7000)
    flight.fly(approach, [at(51.2, 5.75, feet=4000, heading=200, ias=200), at(51.0, 5.55, feet=2800, ias=170)])
    tower = flight.station("LFXX", "tower")
    flight.call(tower, at(51.0, 5.5, feet=2400, ias=160), "established")
    flight.fly(tower, [at(51.0, 5.45, feet=1500, ias=140), at(51.0, 5.42, feet=900, ias=135)])
    flight.fly(tower, [at(51.0, 5.36, ground=True, speed=30) | {"groundspeed_ms": 15.0}])
    flight.call(tower, at(51.0, 5.35, ground=True), "runway_vacated")
    compare("ifr_gate_to_gate", flight)


def test_golden_missed_approach_and_diversion(tmp_path):
    db, engine = ifr_engine(tmp_path)
    engine.set_plan(FlightPlan("EGXX", "LFXX", [], 30000, callsign="BAW123", alternate="EGXX"))
    engine.flight.has_been_airborne, engine.flight.origin_ident, engine.flight.cleared_ft, engine.flight.rules = (
        True,
        "EGXX",
        9000,
        "IFR",
    )
    flight = Flight(engine, db)
    approach, tower = flight.station("LFXX", "approach"), flight.station("LFXX", "tower")
    flight.note("Vectors, a request for another approach, the weather closes in")
    flight.call(approach, at(51.1, 4.9, feet=7000, heading=90), "check_in", altitude=7000)
    flight.call(approach, at(51.1, 5.0, feet=6000, heading=90), "request_approach", approach="NDB")
    flight.call(approach, at(51.1, 5.1, feet=5000, heading=90), "request_visual", issue=MURK)
    flight.fly(approach, [at(51.1, 5.5, feet=3800, heading=90, ias=200), at(51.1, 5.75, feet=3500, heading=90, ias=190),
                          at(51.05, 5.78, feet=2600, heading=180, ias=180), at(51.0, 5.6, feet=2500, ias=160)])  # fmt: skip
    flight.call(tower, at(51.0, 5.5, feet=2000, ias=150), "established")
    flight.fly(tower, [at(51.0, 5.44, feet=1200, ias=140)])
    flight.note("Going around, back to approach, and a diversion")
    flight.call(tower, at(51.0, 5.4, feet=600, ias=140), "going_around")
    flight.call(approach, at(51.0, 5.2, feet=2400, ias=200), "going_around")
    flight.call(approach, at(51.0, 5.0, feet=3000, ias=220), "request_diversion", destination="EGXX")
    flight.fly(approach, [at(51.0, 4.4, feet=12000, ias=260), at(51.0, 3.9, feet=23500, ias=280)])
    compare("missed_approach_and_diversion", flight)


def test_golden_ground_movement(tmp_path):
    db = build(tmp_path, "ground", "\n".join(CROSSING))
    engine = Engine(db, seed=1, procedures=lambda ident: parse_cifp([]))
    engine.set_plan(FlightPlan("EGXX", "LFXX", callsign="BAW123"))
    flight = Flight(engine, db)
    ground, tower = flight.station("EGXX", "ground"), flight.station("EGXX", "tower")

    def taxi(lat, lon=-1.0, speed=0.0):
        return at(lat, lon, ground=True) | {"groundspeed_ms": speed}

    flight.note("Out: across a runway, lost on the way, takeoff cancelled and given again")
    flight.call(ground, taxi(50.996), "request_clearance", flight_rules="IFR", destination="Paris")
    flight.call(ground, taxi(50.996), "request_taxi")
    flight.call(ground, taxi(50.997, speed=6.0), "request_progressive")
    flight.fly(ground, [taxi(50.998, speed=6.0), taxi(50.999)])
    flight.call(ground, taxi(51.002, -0.99, speed=6.0), "request_progressive")
    flight.call(tower, taxi(51.004, -0.981), "ready_for_departure")
    blocked = taxi(51.004, -0.981) | {"tcas_lat": [51.004, 51.004], "tcas_lon": [-0.981, -1.0], "tcas_ele_m": [30.0, 30.0],
                                      "tcas_heading": [270.0, 270.0], "tcas_on_ground": [1, 1]}  # fmt: skip
    flight.fly(tower, [blocked, taxi(51.004, -0.981)])
    flight.note("An incursion: a second flight that does not stop at the runway")
    engine.reset()
    flight.call(ground, taxi(50.996), "request_taxi")
    flight.fly(ground, [taxi(51.0, speed=7.0), taxi(51.001, speed=7.0)])
    compare("ground_movement", flight)


def test_golden_vfr_day(tmp_path):
    db = build(tmp_path, "vfr", FIELDS)
    engine = Engine(db, seed=1, procedures=lambda ident: parse_cifp([]), airspace=AirspaceIndex(parse_openair(OPENAIR)),
                    centers=CenterIndex(parse_atc(ATC)))  # fmt: skip
    flight = Flight(engine, db)
    tower = flight.station("EGXX", "tower")
    flight.note("Westfield tower: circuits, then away to the north")
    flight.call(tower, at(51.0, -1.0, ground=True), "request_circuits")
    flight.call(tower, at(51.0, -0.98, ground=True), "ready_for_departure")
    flight.call(tower, at(50.97, -1.0, feet=1100, heading=90, ias=90), "position_report", position="downwind")
    flight.call(tower, at(51.0, -0.94, feet=500, ias=70), "position_report", position="final", touch_and_go=True)
    flight.call(tower, at(51.0, -0.94, feet=500, ias=70), "request_low_approach")
    engine.reset()
    flight.call(tower, at(51.0, -1.0, ground=True), "request_clearance", flight_rules="VFR", compass="north", atis="C")
    flight.call(tower, at(51.0, -0.98, ground=True), "ready_for_departure")
    flight.fly(tower, [at(51.05, -1.0, feet=1400, heading=0, ias=100), at(51.15, -1.0, feet=1800, heading=0, ias=110)])
    flight.note("A radar service, and controlled airspace ahead")
    radar = flight.station("EGXX", "approach")
    flight.call(radar, at(51.3, -1.3, feet=2000, heading=180, ias=110), "request_flight_following", service="traffic")
    flight.fly(radar, [at(51.18, -1.0, feet=2000, heading=180, ias=110), at(51.25, -1.0, feet=3000, heading=0, ias=110)])
    flight.call(tower, at(51.17, -1.0, feet=1800, heading=180, ias=110), "request_transit")
    flight.fly(tower, [at(51.0, -1.0, feet=1800, heading=180, ias=110), at(50.85, -1.0, feet=1800, heading=180, ias=110)])
    flight.note("Below minima; an airfield with nobody there; an information service")
    flight.call(tower, at(50.85, -1.0, feet=1500, heading=0, ias=100), "inbound", issue=MURK, flight_rules="VFR")
    flight.call(tower, at(50.85, -1.0, feet=1500, heading=0, ias=100), "request_special_vfr", issue=MURK)
    engine.reset()
    quiet = flight.station("EGZZ", "unicom")
    flight.call(quiet, at(51.87, 1.0, feet=2000, heading=0, ias=100), "traffic_call")
    flight.call(quiet, at(51.87, 1.0, feet=2000, heading=0, ias=100), "request_weather")
    information = flight.station("ESXX", "tower")
    flight.call(information, at(60.0, 15.0, ground=True), "request_taxi")
    flight.call(information, at(60.0, 15.0, ground=True), "ready_for_departure")
    flight.call(information, at(60.05, 15.0, feet=1500, heading=180, ias=100), "inbound")
    flight.call(information, at(60.0, 15.06, feet=600, ias=80), "position_report", position="final")
    compare("vfr_day", flight)


def test_a_saved_log_replays_to_the_same_answers(tmp_path):
    """Record a short flight the way the app's log does, then replay it: nothing differs. Change one logged
    answer, and the replay says which."""
    db = build(tmp_path, "ground", "\n".join(CROSSING))

    def make(seed):
        return Engine(db, seed=seed, procedures=lambda ident: parse_cifp([]))

    engine, log, book = make(77), FlightLog(), AtisBook()
    weather = {
        "wx_qnh_pas": 101320.0,
        "wx_wind_alt_m": [30.0],
        "wx_wind_kt": [10.0],
        "wx_wind_dir": [270.0],
        "wx_visibility_sm": 10.0,
    }

    def record(raw, khz, name, **fields):
        raw = {**raw, **weather, "com1_khz": khz}
        station = db.station(khz, raw["lat"], raw["lon"])
        issue = book.current(station.airport, observe(db, station.airport, raw), raw["zulu_seconds"])
        intent = Intent(name, **fields)
        context = {"raw": snapshot(raw)}
        log.record({"type": "pilot", "text": name, "source": "clicked"}, context)
        sent = {key: value for key, value in asdict(intent).items() if value not in (None, False)}
        log.record({"type": "understood", "intent": name, "fields": sent}, context)
        reply = engine.handle(intent, station, issue, raw)
        log.record(
            {"type": "atc", "station": station.callsign, "text": reply.text, "unprompted": False}
            if reply.text
            else {"type": "silent"},
            context,
        )
        return reply

    parked = at(50.996, -1.0, ground=True)
    clearance = record(parked, 121700, "request_clearance", flight_rules="IFR", destination="Paris")
    record(parked, 121700, "readback", squawk=clearance.expect["squawk"])
    record(parked, 121700, "request_taxi")
    record(parked, 121700, "readback", runway="09R")
    record(at(50.999, -1.0, ground=True), 121700, "request_crossing")
    payload = {"seed": engine.seed, "events": list(log.records)}

    result = replay(payload, db, make)
    assert (result.calls, result.skipped, result.differences) == (5, 0, []), result.summary()
    assert "5 calls" in result.summary()

    tampered = {"seed": engine.seed, "events": [dict(event) for event in log.records]}
    taxi = next(event for event in tampered["events"] if "taxi to holding point" in event.get("text", ""))
    taxi["text"] = taxi["text"].replace("via Charlie", "via Delta")
    changed = replay(tampered, db, make)
    assert len(changed.differences) == 1 and changed.differences[0].said == "request_taxi"
    assert "via Charlie" in changed.differences[0].now and "via Delta" in changed.differences[0].logged

    # A different seed gives a different squawk: the replay notices, which is why the seed is saved with the log.
    assert replay({**payload, "seed": engine.seed + 1}, db, make).differences
    # A log from before replay data was kept is skipped, not guessed at.
    old = {"events": [{"type": "understood", "intent": "request_taxi"}]}
    assert replay(old, db, make).skipped == 1
