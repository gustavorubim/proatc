"""Robustness: the engine fed nonsense, the parsers fed garbage, several threads at once, a long flight.

None of these tests says what the right answer is. They say what must never happen: an exception,
a sentence with a hole in it, a deadlock, a slow tick, memory that grows without bound. Every random
choice is seeded, so a failure names the seed that reproduces it.
"""

import json
import random
import threading
import time

import pytest

from proatc import config as config_module
from proatc.airspace import AirspaceIndex, parse_openair
from proatc.atis import Issue
from proatc.centers import CenterIndex, parse_atc
from proatc.config import Config
from proatc.engine import Engine
from proatc.flightlog import KEPT, FlightLog
from proatc.flightplan import FlightPlan, from_simbrief
from proatc.intents import INTENTS, ITEMS, Intent, parse_json
from proatc.metar import parse_metar
from proatc.navdata import AirportDB, build_index, parse_apt
from proatc.navfix import parse_fixes, parse_holds, parse_mora, parse_msa
from proatc.procedures import parse_cifp
from proatc.server import AppServer
from proatc.taxi import parse_taxi
from proatc.verify import BAD_TEXT
from proatc.weather import Cloud, Weather
from tests.test_ifr import ATC
from tests.test_procedures import FIXES, SID, STAR
from tests.test_realism import CROSSING
from tests.test_vfr import FIELDS, OPENAIR

ISSUES = (
    Issue("C", Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1013.2), 12 * 3600),
    Issue(
        "D",
        Weather(wind_dir_true=90, wind_kt=25, gust_kt=38, visibility_sm=1.5, clouds=[Cloud(0.9, 400)], qnh_hpa=985),
        12 * 3600,
    ),
    Issue("E", Weather(wind_dir_true=0, wind_kt=0, visibility_sm=0.2, fog=True, qnh_hpa=1040), 12 * 3600),
    None,
)
COMPASS = ("north", "south east", "west", None)
VALUES = {
    "runway": ("27", "09", "27L", "36", None),
    "squawk": ("4521", "7000", None),
    "frequency": ("118.100", "121.700", "999.999", None),
    "altitude": (0, 1000, 6000, 24000, 60000, None),
    "heading": (0, 90, 360, None),
    "qnh": (1013.0, 29.92, None),
    "position": ("downwind", "base", "final", None),
    "destination": ("EGXX", "LFXX", "Paris", "", None),
    "flight_rules": ("IFR", "VFR", None),
    "fix": ("ALFAX", "NOWHERE", None),
    "direction": ("left", "right", None),
    "approach": ("ILS", "RNAV", "NDB", "visual", None),
    "service": ("basic", "traffic", None),
    "compass": COMPASS,
    "item": (*ITEMS, None),
}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    folder = tmp_path_factory.mktemp("robust")
    apt = folder / "apt.dat"
    apt.write_text(
        FIELDS + "\n\n" + "\n".join(CROSSING[3:]).replace("EGXX", "EGTT").replace("Testfield", "Crossfield"), encoding="utf-8"
    )
    build_index(apt, folder / "a.sqlite")
    db = AirportDB(folder / "a.sqlite")
    procedures = {"EGXX": parse_cifp(SID), "LFXX": parse_cifp(STAR)}
    stations = []
    for ident in ("EGXX", "EGZZ", "ESXX", "KXAA", "KXBB", "LFXX", "EGTT"):
        airport = db.airport(ident)
        for kind in sorted({freq.kind for freq in airport.freqs}):
            stations.append(db.station_of(airport, kind))
    centers = CenterIndex(parse_atc(ATC))
    stations.append(centers.on_frequency(127500, 51.0, 0.0))
    stations.append(centers.on_frequency(132100, 51.0, 3.0))

    def make(seed: int, **options) -> Engine:
        return Engine(
            db, seed=seed, procedures=lambda ident: procedures.get(ident, parse_cifp([])), centers=centers, navfix=FIXES,
            airspace=AirspaceIndex(parse_openair(OPENAIR)), **options,
        )  # fmt: skip

    return db, stations, make


def random_intent(rng: random.Random) -> Intent:
    fields = {key: rng.choice(options) for key, options in VALUES.items() if rng.random() < 0.3}
    return Intent(rng.choice(list(INTENTS)), touch_and_go=rng.random() < 0.1, **fields)


def random_raw(rng: random.Random, station, with_traffic: bool) -> dict:
    airport = getattr(station, "airport", None)
    lat, lon = (airport.lat, airport.lon) if airport else (51.0, rng.uniform(-2.5, 7.5))
    ground = rng.random() < 0.35
    reach = 0.02 if ground else rng.choice((0.05, 0.3, 1.0))
    feet = (airport.elev_ft if airport else 0) + (0 if ground else rng.choice((300, 1500, 4000, 12000, 36000)))
    raw = {
        "tail_number": rng.choice(("G-ABCD", "N123AB", "")),
        "lat": lat + rng.uniform(-reach, reach),
        "lon": lon + rng.uniform(-reach, reach),
        "on_ground": int(ground),
        "elevation_m": feet * 0.3048,
        "agl_m": 0.0 if ground else max(feet, 50) * 0.3048,
        "groundspeed_ms": rng.choice((0.0, 5.0)) if ground else rng.uniform(30, 250),
        "heading_mag": rng.uniform(0, 360),
        "heading_true": rng.uniform(0, 360),
        "ias_kt": rng.uniform(0, 340),
        "vs_fpm": rng.uniform(-3000, 3000),
        "squawk": rng.choice((1200, 7000, 4521)),
        "zulu_seconds": rng.uniform(0, 86400),
        "wx_source": rng.choice((0, 1)),
        "aircraft_type": rng.choice(("A20N", "C172", "B77W", "")),
    }
    for key in ("ias_kt", "vs_fpm", "squawk", "heading_true", "agl_m", "aircraft_type", "zulu_seconds"):
        if rng.random() < 0.08:
            del raw[key]  # the sim has not sent every value yet
    if with_traffic:
        count = rng.randint(1, 4)
        raw["tcas_lat"] = [raw["lat"]] + [lat + rng.uniform(-0.1, 0.1) for _ in range(count)]
        raw["tcas_lon"] = [raw["lon"]] + [lon + rng.uniform(-0.1, 0.1) for _ in range(count)]
        raw["tcas_ele_m"] = [raw["elevation_m"]] + [rng.uniform(0, 3000) for _ in range(count)]
        raw["tcas_heading"] = [0.0] + [rng.uniform(0, 360) for _ in range(count)]
        raw["tcas_on_ground"] = [0] + [rng.randint(0, 1) for _ in range(count)]
    return raw


def check_text(text: str, where: str) -> None:
    assert not BAD_TEXT.search(text), f"{where}: reads wrong: {text!r}"


def one_flight(make, stations, seed: int, steps: int = 40) -> int:
    rng = random.Random(seed)
    clock = [0.0]
    engine = make(seed, clock=lambda: clock[0], holding=rng.choice(("never", "sometimes", "always")), traffic=rng.random() < 0.7)
    if rng.random() < 0.6:
        engine.set_plan(FlightPlan("EGXX", "LFXX", "ALFAX UL9 BRAVO UM605 DELTA".split(), 36000, callsign=rng.choice(("BAW123", None)), alternate=rng.choice(("EGXX", None))))  # fmt: skip
    spoken = 0
    station = rng.choice(stations)
    for step in range(steps):
        if rng.random() < 0.3:
            station = rng.choice(stations)
        where = f"seed {seed} step {step} {station.callsign}"
        raw = random_raw(rng, station, engine.traffic_enabled and rng.random() < 0.4)
        issue = rng.choice(ISSUES) if getattr(station, "airport", None) else None
        clock[0] += rng.uniform(0, 90)
        intent = random_intent(rng)
        reply = engine.handle(intent, station, issue, raw, rng.uniform(-20, 20))
        if reply is not None:
            check_text(reply.text, f"{where} {intent.intent}")
            assert isinstance(reply.expect, dict)
            spoken += bool(reply.text)
        said = engine.tick(station, issue, raw)
        if said is not None:
            check_text(said.text, f"{where} unprompted")
        for item in engine.suggestions(station, issue, raw):
            assert item["label"] and item["text"] and item["intent"]["intent"] in INTENTS, where
            check_text(item["text"].replace("{destination}", "Paris"), f"{where} button {item['label']}")
        if rng.random() < 0.25:
            # Any button the app shows can be clicked, with or without a readback outstanding.
            buttons = engine.suggestions(station, issue, raw)
            if buttons:
                clicked = engine.handle(Intent.from_dict(rng.choice(buttons)["intent"]), station, issue, raw)
                if clicked is not None:
                    check_text(clicked.text, f"{where} clicked")
        summary = engine.summary()
        assert all(value for value in summary.values()), where
        json.dumps(engine.taxi_map(raw))  # the map never raises, whatever the flight's state
        if rng.random() < 0.03:
            engine.reset()
    return spoken


@pytest.mark.parametrize("block", range(8))
def test_random_calls_in_random_states_never_break_the_engine(world, block):
    _, stations, make = world
    spoken = sum(one_flight(make, stations, seed) for seed in range(block * 50, block * 50 + 50))
    assert spoken > 200  # the flights were not all silence: the engine did answer


def test_garbage_in_the_data_files_is_skipped_not_fatal():
    rng = random.Random(7)
    good = {
        "cifp": SID + STAR,
        "apt": FIELDS.splitlines() + CROSSING,
        "openair": OPENAIR,
        "atc": ATC,
        "fix": ["  51.000000   -1.000000  ALFAX ENRT EG 4530243", "  10.0 10.0 BIBAX ENRT EG 1"],
        "hold": ["ECHOX LF LFXX 11 270.0 1.0 0.0 R 5000 14000 220"],
        "msa": ["11 LFXX LF LFXX M 000 042 25 000 000 0"],
        "mora": ["+51 -001 " + " ".join(["023"] * 30)],
        "metar": [
            "EGLL 161150Z 26012G24KT 9999 -RA BKN008 OVC015 11/09 Q0998",
            "KJFK 161151Z 00000KT 1/2SM FG VV002 05/05 A2992",
        ],
    }
    parsers = {
        "cifp": parse_cifp,
        "apt": lambda lines: list(parse_apt(lines)),
        "openair": parse_openair,
        "atc": parse_atc,
        "fix": lambda lines: parse_fixes(lines, lines),
        "hold": parse_holds,
        "msa": parse_msa,
        "mora": parse_mora,
        "metar": lambda lines: [parse_metar(line) for line in lines],
        "taxi": parse_taxi,
    }
    good["taxi"] = CROSSING
    junk = [
        "",
        " ",
        "\x00\x00",
        "1202",
        "100",
        "DP",
        "AL",
        "SID:",
        "APPCH:010",
        "1300 x y z",
        "1201 a b c d",
        "99 99 99",
        "é" * 50,
    ]

    def mangle(line: str) -> str:
        choice = rng.random()
        if choice < 0.2 and line:
            cut = rng.randrange(len(line))
            return line[:cut]  # truncated
        if choice < 0.4:
            parts = line.split(",") if "," in line else line.split()
            if parts:
                parts[rng.randrange(len(parts))] = rng.choice(("", "x", "-", "1e999", "NaN", "999999999999", "FL", "§"))
            return (", " if "," in line else " ").join(parts)
        if choice < 0.5:
            return rng.choice(junk)
        if choice < 0.6:
            return line + line
        return line

    for name, parse in parsers.items():
        parse(good[name])  # the clean data parses
        for _ in range(300):
            lines = [mangle(line) for line in good[name]]
            rng.shuffle(lines) if rng.random() < 0.2 else None
            try:
                parse(lines)
            except Exception as error:  # pragma: no cover - the assertion message is the point
                raise AssertionError(f"{name} parser raised {type(error).__name__}: {error} on {lines!r}") from error


def test_whatever_the_language_model_returns_becomes_a_valid_intent():
    rng = random.Random(11)
    pieces = ['"intent": "request_taxi"', '"runway": "27 left"', '"runway": 27', '"squawk": 4521', '"squawk": "45"', '"qnh": "abc"',
              '"altitude": "flight level 240"', '"altitude": -5', '"altitude": 1e12', '"heading": 720', '"heading": null',
              '"frequency": "118,5"', '"frequency": 118.5', '"touch_and_go": "yes"', '"compass": "up"', '"approach": ["ILS"]',
              '"service": {"a": 1}', '"intent": 5', '"intent": "launch_missiles"', '"fix": 12', '"callsign": ["G"]', '"atis": ""',
              '"destination": null', '"position": "upwind"', '"direction": "straight"']  # fmt: skip
    for _ in range(2000):
        body = ", ".join(rng.sample(pieces, rng.randint(0, 8)))
        text = rng.choice(("", "Sure! ", "```json\n")) + "{" + body + "}" + rng.choice(("", " trailing words", "}"))
        if rng.random() < 0.1:
            text = text[: rng.randrange(len(text) + 1)]
        intent = parse_json(text)
        assert intent.intent in INTENTS
        assert intent.runway is None or (2 <= len(intent.runway) <= 3 and intent.runway[:2].isdigit())
        assert intent.squawk is None or (len(intent.squawk) == 4 and intent.squawk.isdigit())
        assert intent.altitude is None or 0 <= intent.altitude <= 60000
        assert intent.heading is None or 0 <= intent.heading <= 360
        assert intent.frequency is None or intent.frequency.count(".") == 1
    for briefing in ({}, {"origin": None}, {"origin": {"icao_code": "EGLL"}, "destination": {"icao_code": {}}}, {"general": []}):
        with pytest.raises(Exception) as caught:
            from_simbrief(briefing)
        assert caught.type.__name__ in ("PlanError", "AttributeError", "TypeError")


def test_several_threads_use_the_engine_at_once_without_error_or_deadlock(world):
    _, stations, make = world
    engine = make(1)
    engine.set_plan(FlightPlan("EGXX", "LFXX", "ALFAX UL9 BRAVO UM605 DELTA".split(), 36000))
    errors: list[str] = []
    stop = time.monotonic() + 2.5
    counts = [0] * 6

    def worker(number: int) -> None:
        rng = random.Random(number)
        try:
            while time.monotonic() < stop:
                station = rng.choice(stations)
                raw = random_raw(rng, station, False)
                issue = ISSUES[0] if getattr(station, "airport", None) else None
                action = number % 3
                if action == 0:
                    engine.handle(random_intent(rng), station, issue, raw)
                elif action == 1:
                    engine.tick(station, issue, raw)
                else:
                    engine.suggestions(station, issue, raw)
                    engine.summary()
                counts[number] += 1
        except Exception as error:  # pragma: no cover
            errors.append(f"thread {number}: {type(error).__name__}: {error}")

    threads = [threading.Thread(target=worker, args=(number,), daemon=True) for number in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    assert not any(thread.is_alive() for thread in threads), "a thread never finished: deadlock"
    assert errors == [] and min(counts) > 50


def test_a_long_flight_stays_fast_and_bounded(world):
    _, stations, make = world
    clock = [0.0]
    engine = make(3, clock=lambda: clock[0])
    engine.set_plan(FlightPlan("EGXX", "LFXX", "ALFAX UL9 BRAVO UM605 DELTA".split(), 36000))
    centre = next(station for station in stations if station.kind == "center")
    engine.flight.contacted.add(centre.key)
    engine.flight.has_been_airborne, engine.flight.cleared_ft = True, 36000
    rng = random.Random(5)
    raw = random_raw(rng, centre, True)
    started = time.perf_counter()
    for step in range(6000):  # about fifty minutes of flight at two looks a second
        clock[0] += 0.5
        raw["lon"] = -2.0 + step * 0.0015
        raw["lat"] = 51.0
        raw["on_ground"], raw["elevation_m"], raw["agl_m"] = 0, 36000 * 0.3048, 10000.0
        engine.tick(centre, None, raw)
        engine.flight.pending = None
    each_ms = (time.perf_counter() - started) / 6000 * 1000
    assert each_ms < 5, f"a tick takes {each_ms:.2f} ms"
    assert len(engine._traffic_seen) < 100 and len(engine.flight.airspace_called) < 100

    log = FlightLog()
    for number in range(KEPT * 3):
        log.record({"type": "atc", "station": "Tower", "text": f"call {number}", "unprompted": True}, {"tuned": "118.100"})
    assert len(log.records) == KEPT and log.records[-1]["text"] == f"call {KEPT * 3 - 1}"
    assert len(log.text()) < KEPT * 200


def test_the_app_server_survives_malformed_commands(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr("proatc.server.PROJECT_DIR", tmp_path)
    monkeypatch.setattr("proatc.voice_loop.PROJECT_DIR", tmp_path)
    app = AppServer(Config(), 0, "token")
    seen: list[dict] = []
    app.emit = seen.append
    rng = random.Random(3)
    names = ["set_key", "simbrief", "plan", "clear_plan", "tune", "settings", "capture_ptt", "explain", "what_next", "save_log",
             "reset_cost", "ptt", "say", "click", "debrief", "new_flight", "chart", "sound_check", "sound_check_answer", "report",
             "nope", None, 5]  # fmt: skip
    values = [None, "", "x" * 5000, 0, -1, 1e308, [], {}, {"a": {"b": []}}, True, "118.100", {"remote": {"enabled": "yes"}}]
    handled = (ValueError, KeyError, TypeError, AttributeError)  # what the connection handler reports to the user
    fields = ["key", "user", "destination", "cruise", "route", "mhz", "changes", "down", "text", "intent", "ident", "heard",
              "hide"]  # fmt: skip
    for _ in range(600):
        command = {"cmd": rng.choice(names)}
        for key in rng.sample(fields, rng.randint(0, 4)):
            command[key] = rng.choice(values)
        if command["cmd"] == "set_key":
            command["key"] = ""  # never write a key file from a test
        try:
            app._command(command, remote=rng.random() < 0.5)
        except handled:
            pass
        except Exception as error:
            if type(error).__name__ not in ("PlanError", "SimUnavailable"):
                raise AssertionError(f"{command!r} raised {type(error).__name__}: {error}") from error
    assert not (tmp_path / ".env").exists()
    assert app.session.config.remote.enabled is False and app.session.config.llm.provider == "openrouter"


def test_the_real_data_sweep_runs_clean_on_the_test_airports(world):
    """The sweep tool is how real installations are checked; here it runs on the invented airports, so that it
    cannot rot unnoticed and so that these airports are held to the same standard."""
    from proatc.verify import pick_airports, sweep

    db, _, _ = world
    idents = pick_airports(db, 7)
    assert sorted(idents) == ["EGTT", "EGXX", "EGZZ", "ESXX", "KXAA", "KXBB", "LFXX"]
    report = sweep(db, idents)
    assert report.airports == 7 and sum(report.checks.values()) > 600
    assert report.failures == [], report.summary()
    assert "7 airports" in report.summary() and "0 failures" in report.summary()
