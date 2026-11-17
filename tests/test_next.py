"""Settings, flight log, cost, copilot and instructor, emergencies, runway split, SID checks,
flight following and traffic."""

import json

import pytest

from proatc import config as config_module
from proatc import traffic
from proatc.atis import AtisBook, Issue, compose, split_runways
from proatc.clock import Clock
from proatc.config import Config, Llm, editable_values, load_config, save_overrides, validate
from proatc.engine import Engine
from proatc.flightlog import FlightLog
from proatc.flightplan import FlightPlan
from proatc.intents import Intent, parse_json
from proatc.llm import Controller
from proatc.navdata import AirportDB, build_index
from proatc.navfix import NavFix
from proatc.voice_loop import Session
from proatc.weather import Weather
from tests.test_flow import make
from tests.test_ifr import CALLSIGN, ISSUE, at

# An airport with two parallel runways and the usual stations.
PARALLEL = """A
1200 test

1    100 0 0 EGPP Parfield
1302 icao_code EGPP
100 45.00 1 1 0.25 1 3 0  09L 51.0100 -1.0200 0 0 3 2 1 0 27R 51.0100 -0.9800 0 0 3 2 1 0
100 45.00 1 1 0.25 1 3 0  09R 51.0000 -1.0200 0 0 3 2 1 0 27L 51.0000 -0.9800 0 0 3 2 1 0
1053 121700 Parfield Ground
1054 118300 Parfield Tower
1055 119500 Parfield Approach
""".splitlines()


def parallel_db(tmp_path):
    apt = tmp_path / "parallel.dat"
    apt.write_text("\n".join(PARALLEL), encoding="utf-8")
    build_index(apt, tmp_path / "parallel.sqlite")
    return AirportDB(tmp_path / "parallel.sqlite")


def with_traffic(raw, *others):
    """Add other aircraft to a sim snapshot: (lat, lon, feet, heading, on_ground)."""
    slots = [(raw["lat"], raw["lon"], raw["elevation_m"] / 0.3048, raw["heading_true"], raw["on_ground"])] + list(others)
    return {
        **raw,
        "tcas_lat": [s[0] for s in slots] + [0.0, 0.0],
        "tcas_lon": [s[1] for s in slots] + [0.0, 0.0],
        "tcas_ele_m": [s[2] * 0.3048 for s in slots] + [0.0, 0.0],
        "tcas_heading": [s[3] for s in slots] + [0.0, 0.0],
        "tcas_vs_fpm": [0.0] * (len(slots) + 2),
        "tcas_on_ground": [int(s[4]) for s in slots] + [0, 0],
    }


# ---- settings ---------------------------------------------------------------


def test_settings_are_checked_before_they_are_applied():
    good = validate({"atc": {"holding": "always", "copilot": "full", "traffic": False}, "audio": {"input_device": 3},
                     "ptt": {"key": "f13", "joystick": None, "joystick_button": None}, "tts": {"speed": 1.2}})  # fmt: skip
    assert good["atc"]["copilot"] == "full" and good["audio"] == {"input_device": 3} and good["ptt"]["joystick"] is None
    for bad in (
        {"atc": {"holding": "often"}},
        {"atc": {"traffic": "yes"}},
        {"audio": {"input_device": "mic"}},
        {"audio": {"input_device": True}},
        {"tts": {"speed": 5}},
        {"tts": {"voice": ""}},
        {"llm": {"max_tokens": 9}},  # not something the app may change
        {"stt": {"device": "cpu"}},
        {"nonsense": {}},
        {"atc": "always"},
    ):
        with pytest.raises(ValueError):
            validate(bad)


def test_settings_changed_in_the_app_survive_a_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    (tmp_path / "config.toml").write_text('[atc]\nholding = "sometimes"\n[ptt]\nkey = "f9"\n', encoding="utf-8")
    assert load_config().atc.holding == "sometimes" and load_config().ptt.key == "f9"
    save_overrides({"atc": {"copilot": "readbacks"}, "ptt": {"joystick": "Yoke", "joystick_button": 2}})
    save_overrides({"atc": {"holding": "never"}})
    loaded = load_config()
    assert (loaded.atc.copilot, loaded.atc.holding, loaded.ptt.key) == ("readbacks", "never", "f9")
    assert (loaded.ptt.joystick, loaded.ptt.joystick_button) == ("Yoke", 2)
    assert editable_values(loaded)["atc"] == {
        "holding": "never", "copilot": "readbacks", "traffic": True, "chatter": "some", "second_radio": True,
    }  # fmt: skip
    config_module.settings_path().write_text('{"config": {"atc": {"holding": "bogus"}}}', encoding="utf-8")
    assert load_config().atc.holding == "sometimes"  # a bad saved value is ignored, not fatal


def test_session_applies_setting_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    session = Session(Config(), emit=lambda event: None)
    session.apply_settings({"atc": {"copilot": "full"}, "sim": {"auto_tune": True}})
    assert session.config.atc.copilot == "full" and session.config.sim.auto_tune
    assert json.loads((tmp_path / "settings.json").read_text())["config"]["atc"] == {"copilot": "full"}
    with pytest.raises(ValueError):
        session.apply_settings({"atc": {"copilot": "sometimes"}})


# ---- flight log and cost ----------------------------------------------------


def test_flight_log_is_a_readable_transcript_and_a_full_record(tmp_path):
    log = FlightLog()
    where = {"tuned": "118.505", "station": "Heathrow Tower", "aircraft": {"altitude_ft": 80}}
    log.record({"type": "pilot", "text": "Tower, ready for departure", "source": "voice"}, where)
    log.record({"type": "understood", "intent": "ready_for_departure"}, where)
    log.record({"type": "atc", "station": "Heathrow Tower", "text": "Cleared for takeoff", "unprompted": False}, where)
    log.record({"type": "tx", "on": True})
    log.record({"type": "timings", "listen": 0.3, "think": 1.0, "voice": 1.0, "total": 2.3})
    log.record({"type": "coach", "text": "Line up and go."})
    log.record({"type": "atis", "airport": "EGLL", "mhz": "128.080", "text": "Heathrow information Kilo"})
    path = log.save(tmp_path / "logs", {"model": "test-model", "openrouter_usage": {"cost": 0.01}})
    text = path.read_text(encoding="utf-8")
    assert "[118.505] PILOT (voice): Tower, ready for departure" in text and "understood as: ready_for_departure" in text
    assert "[118.505] HEATHROW TOWER: Cleared for takeoff" in text and "INSTRUCTOR: Line up and go." in text
    data = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert [e["type"] for e in data["events"]] == ["pilot", "understood", "atc", "coach", "atis"]  # no noise
    assert data["events"][0]["context"]["station"] == "Heathrow Tower" and data["model"] == "test-model"


def test_openrouter_usage_adds_up():
    usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}
    controller = Controller(Llm(), "key", usage)
    controller._count({"prompt_tokens": 449, "completion_tokens": 24, "cost": 0.0001449})
    controller._count({"prompt_tokens": 500, "completion_tokens": 30, "cost": 0.0002})
    controller._count({})  # a provider that reports nothing
    assert usage["calls"] == 3 and usage["prompt_tokens"] == 949 and usage["completion_tokens"] == 54
    assert usage["cost"] == pytest.approx(0.0003449)
    assert Controller(Llm(), "key").usage["calls"] == 0  # its own counter when none is shared


# ---- copilot and instructor -------------------------------------------------


class Sim:
    """Stands in for the bridge: a snapshot that a write changes."""

    connected = True

    def __init__(self, raw):
        self.raw, self.writes = dict(raw), []

    def snapshot(self):
        return dict(self.raw)

    def write(self, key, value):
        self.writes.append((key, value))
        self.raw[key] = value


class NoWaiting(Clock):
    def sleep(self, seconds):
        pass


def copilot_session(tmp_path, monkeypatch, mode, raw, auto_tune=False):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    db, engine, _, _ = make(tmp_path)
    engine.set_plan(None)
    engine.flight.has_been_airborne = False
    events = []
    session = Session(Config(), emit=events.append, clock=NoWaiting())
    session.config.atc.copilot, session.config.sim.auto_tune = mode, auto_tune
    session.db, session.engine, session.book, session.bridge, session.ready = db, engine, AtisBook(), Sim(raw), True
    return session, engine, events


def parked(com1):
    return {**at(51.003, -1.0, on_ground=True), "com1_khz": com1, "wx_qnh_pas": 101320.0,
            "wx_wind_alt_m": [30.0], "wx_wind_kt": [10.0], "wx_wind_dir": [270.0]}  # fmt: skip


def test_copilot_reads_back_once_and_tunes_the_handoff(tmp_path, monkeypatch):
    session, engine, events = copilot_session(tmp_path, monkeypatch, "full", parked(121700))
    ground = session.db.station(121700, 51.0, -1.0)
    engine.handle(Intent("request_taxi"), ground, ISSUE, session.bridge.snapshot())
    session._after_transmission()
    session._after_transmission()
    assert session.jobs.qsize() == 1  # the same instruction is not queued twice
    kind, text, intent = session.jobs.get_nowait()
    assert kind == "copilot" and intent == Intent("readback", runway="27")
    assert text.startswith("Taxi to holding point runway two seven") and text.endswith(CALLSIGN)
    assert engine.handle(intent, ground, ISSUE, session.bridge.snapshot()).text == ""

    engine.handle(Intent("ready_for_departure"), ground, ISSUE, session.bridge.snapshot())  # "contact tower"
    session._after_transmission()
    _, _, readback = session.jobs.get_nowait()
    assert readback == Intent("readback", frequency="118.100")
    engine.handle(readback, ground, ISSUE, session.bridge.snapshot())
    session._after_transmission()
    assert session.bridge.writes == [("com1_khz", 118100)]  # tuned without being asked
    assert {"type": "info", "text": "COM1 tuned to 118.100"} in events
    assert session.jobs.get_nowait()[0] == "after"  # then the copilot looks again, to check in
    session._after_transmission()
    assert session.bridge.writes == [("com1_khz", 118100)]  # tuned once
    # The hand-off is done when the tower is called on its frequency, not when the radio is turned.
    assert engine.flight.next_frequency == "118.100"
    tower = session.db.station(118100, 51.0, -1.0)
    engine.handle(Intent("ready_for_departure"), tower, ISSUE, session.bridge.snapshot())
    assert engine.flight.next_frequency is None


def test_copilot_off_leaves_the_radio_to_the_pilot_unless_auto_tune_is_on(tmp_path, monkeypatch):
    session, engine, _ = copilot_session(tmp_path, monkeypatch, "off", parked(121700))
    ground = session.db.station(121700, 51.0, -1.0)
    engine.handle(Intent("ready_for_departure"), ground, ISSUE, session.bridge.snapshot())
    session._after_transmission()
    assert session.jobs.empty() and session.bridge.writes == []  # nothing read back, nothing tuned

    session.config.sim.auto_tune = True
    session._after_transmission()
    assert session.bridge.writes == []  # not before the pilot has read the frequency back
    engine.handle(Intent("readback", frequency="118.1"), ground, ISSUE, session.bridge.snapshot())
    session._after_transmission()
    assert session.bridge.writes == [("com1_khz", 118100)]


def test_instructor_says_what_to_say_next(tmp_path, monkeypatch):
    session, engine, events = copilot_session(tmp_path, monkeypatch, "off", parked(121700))
    session._instructor("what_next")
    assert events[-1]["type"] == "coach" and "request I F R clearance to your destination" in events[-1]["text"]
    session._instructor("explain")
    assert events[-1] == {"type": "coach", "text": "No controller has said anything yet."}
    session.emit(
        {"type": "atc", "station": "Westfield Ground", "text": "taxi to holding point runway two seven", "unprompted": False}
    )
    session._instructor("explain")
    assert events[-1]["text"] == "Add an OpenRouter key to have instructions explained."

    class Model:
        def complete(self, system, user, max_tokens=None, temperature=0.3):
            assert "Westfield Ground said: taxi to holding point runway two seven" in user
            return "Taxi to the runway and stop before it."

    session.controller = Model()
    session._instructor("explain")
    assert events[-1] == {"type": "coach", "text": "Taxi to the runway and stop before it."}
    assert [r["type"] for r in session.log.records][-1] == "coach"  # and it is in the flight log


def test_copilot_checks_in_with_tower_on_the_approach(tmp_path):
    db, engine, _, _ = make(tmp_path)
    approach, tower = db.station(124300, 51.0, 5.0), db.station(119100, 51.0, 5.3)
    engine.handle(Intent("check_in"), approach, ISSUE, at(51.03, 5.75, feet=3000, heading=270))
    engine.handle(Intent("established"), approach, ISSUE, at(51.0, 5.6, feet=2500, heading=270))
    calls = engine.suggestions(tower, ISSUE, at(51.0, 5.6, feet=2500, heading=270))
    # "Contact tower" is read back to approach, who said it, not to the tower.
    assert calls[0]["label"] == "Check in"
    assert engine.suggestions(approach, ISSUE, at(51.0, 5.6, feet=2500, heading=270))[0]["label"] == "Read back"
    first = next(call for call in calls if call["label"] == "Check in")
    assert (first["label"], first["intent"]) == ("Check in", {"intent": "established"})
    assert first["text"] == f"Eastfield Tower, {CALLSIGN}, established I L S runway two seven"
    assert Intent.from_dict({**first["intent"], "junk": 1}) == Intent("established")


# ---- emergencies ------------------------------------------------------------


def test_mayday_is_acknowledged_and_the_landing_is_cleared_at_once(tmp_path):
    db, engine, centers, _ = make(tmp_path)
    west = centers.on_frequency(127500, 51.0, 0.0)
    cruising = at(51.0, 0.0, feet=20000, heading=90)
    assert parse_json('{"intent": "mayday"}').intent == "mayday"
    assert engine.handle(Intent("mayday"), west, None, cruising).text == (
        f"{CALLSIGN}, Westland Control, roger mayday, squawk seven seven zero zero, say intentions"
    )
    assert engine.summary()["emergency"] == "mayday" and engine.flight.squawk == "7700"

    tower = db.station(119100, 51.0, 5.3)
    far_out = at(51.0, 5.6, feet=2500, heading=270)  # nine miles: normally "continue approach"
    landing = engine.handle(Intent("inbound"), tower, ISSUE, far_out)
    assert landing.text == (
        f"{CALLSIGN}, Eastfield Tower, runway two seven, cleared to land, wind two seven zero degrees one zero knots, "
        "emergency services standing by"
    )
    assert engine.handle(Intent("cancel_emergency"), tower, ISSUE, far_out).text == f"{CALLSIGN}, roger, emergency cancelled"
    assert engine.handle(Intent("cancel_emergency"), tower, ISSUE, far_out) is None and "emergency" not in engine.summary()
    assert (
        engine.handle(Intent("pan_pan"), tower, ISSUE, far_out).text
        == f"{CALLSIGN}, roger pan pan, squawk seven seven zero zero, say intentions"
    )


# ---- runways ----------------------------------------------------------------


def test_parallel_runways_are_split_between_landing_and_departure(tmp_path):
    assert split_runways(["27"]) == (["27"], ["27"])
    assert split_runways(["27L", "27R"]) == (["27L"], ["27R"])
    assert split_runways(["16L", "16C", "16R"]) == (["16L", "16C"], ["16C", "16R"])
    assert split_runways(["26L", "26R", "27L", "27R"]) == (["26L", "26R"], ["27L", "27R"])

    db = parallel_db(tmp_path)
    airport = db.airport("EGPP")
    weather = Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1013)
    assert "Runway two seven left in use for landing, runway two seven right for departure." in compose(airport, weather, "A", 0)
    single = compose(make(tmp_path)[0].airport("EGXX"), weather, "A", 0)
    assert "Runway two seven in use." in single

    engine, issue = Engine(db, seed=1), Issue("A", weather, 0)
    tower = db.station(118300, 51.005, -1.0)
    on_ground = at(51.005, -1.0, on_ground=True)
    assert (
        "runway two seven right, cleared for takeoff"
        in engine.handle(Intent("ready_for_departure"), tower, issue, on_ground).text
    )
    arriving = Engine(db, seed=1)
    arriving.flight.has_been_airborne = True
    final = at(51.005, -0.9, feet=1200, heading=270)
    assert (
        "runway two seven left, cleared to land"
        in arriving.handle(Intent("position_report", position="final"), tower, issue, final).text
    )


def test_a_sid_crossing_flown_low_is_called(tmp_path):
    navfix = NavFix(fixes={"BRV": [(51.0, -1.2)]})
    db, engine, _, _ = make(tmp_path, navfix=navfix)
    engine.flight.has_been_airborne = False
    engine.set_plan(FlightPlan("EGXX", "LFXX", ["BRV", "UL9", "DELTA"], 36000, sid="BRV3C"))
    delivery, departure = db.station(121900, 51.0, -1.0), db.station(125500, 51.0, -1.0)
    clearance = engine.handle(Intent("request_clearance"), delivery, ISSUE, at(51.003, -1.0, on_ground=True))
    assert "Bravo Romeo Victor three Charlie departure" in clearance.text
    assert [(c.fix, c.altitude_ft) for c in engine.flight.sid_constraints] == [("BRV", 4000)]
    engine.handle(Intent("check_in"), departure, ISSUE, at(51.0, -1.1, feet=2000))
    engine.flight.pending = None  # read back: the next instruction waits for it
    assert engine.tick(departure, ISSUE, at(51.0, -1.12, feet=2500)) is None  # three miles short of the fix
    low = engine.tick(departure, ISSUE, at(51.0, -1.19, feet=3000))
    assert low.text == f"{CALLSIGN}, you were to cross Bravo Romeo Victor at or above altitude four thousand feet, expedite climb"
    assert engine.tick(departure, ISSUE, at(51.0, -1.2, feet=3000)) is None  # said once

    on_time = make(tmp_path, navfix=navfix)[1]
    on_time.set_plan(FlightPlan("EGXX", "LFXX", ["BRV"], 36000, sid="BRV3C"))
    on_time.flight.has_been_airborne = False
    on_time.handle(Intent("request_clearance"), delivery, ISSUE, at(51.003, -1.0, on_ground=True))
    on_time.handle(Intent("check_in"), departure, ISSUE, at(51.0, -1.1, feet=3000))
    assert on_time.tick(departure, ISSUE, at(51.0, -1.19, feet=4500)) is None


def test_vfr_flight_following(tmp_path):
    db, engine, centers, _ = make(tmp_path)
    engine.set_plan(None)
    west = centers.on_frequency(127500, 51.0, 0.0)
    cruising = at(51.0, 0.0, feet=5500, heading=90)
    # A British centre: the service is called a Basic Service there, and the aircraft is not identified for it.
    assert "Request basic service" in [s["label"] for s in engine.suggestions(west, None, cruising)]
    service = engine.handle(Intent("request_flight_following"), west, None, cruising)
    code = engine.flight.squawk
    assert (
        service.text
        == f"{CALLSIGN}, Westland Control, squawk {' '.join(['zero', 'one', 'two', 'three', 'four', 'five', 'six', 'seven'][int(d)] for d in code)}, basic service"
    )
    assert service.expect == {"squawk": code} and engine.flight.rules == "VFR" and engine.summary()["service"] == "basic"
    assert "Cancel radar service" in [s["label"] for s in engine.suggestions(west, None, cruising)]
    assert engine.handle(Intent("cancel_flight_following"), west, None, cruising).text == (
        f"{CALLSIGN}, radar service terminated, squawk seven zero zero zero, frequency change approved"
    )
    assert engine.handle(Intent("cancel_flight_following"), west, None, cruising) is None


# ---- traffic ----------------------------------------------------------------


def test_traffic_is_read_from_the_sim_and_called_once(tmp_path):
    own = at(51.0, 0.0, feet=10000, heading=90)
    busy = with_traffic(own, (51.0, 0.1, 11000, 270, False), (51.5, 3.0, 10000, 90, False), (51.0, 0.02, 100, 0, True))
    seen = traffic.targets(busy)
    assert [t.index for t in seen] == [1, 2, 3] and round(seen[0].altitude_ft) == 11000 and seen[2].on_ground
    assert traffic.targets(own) == []  # the sim reports no traffic at all

    called: dict = {}
    assert (
        traffic.advisory(busy, called, 0.0) == "traffic, twelve o'clock, four miles, opposite direction, one thousand feet above"
    )
    assert traffic.advisory(busy, called, 60.0) is None  # the same aircraft is not called again straight away
    assert traffic.advisory(busy, called, 400.0) is not None
    crossing = with_traffic(own, (50.95, 0.0, 10100, 0, False))
    assert traffic.advisory(crossing, {}, 0.0) == "traffic, three o'clock, three miles, crossing, same altitude"
    assert traffic.advisory(with_traffic(own, (51.0, 0.1, 14000, 270, False)), {}, 0.0) is None  # well above
    assert traffic.advisory(with_traffic(at(51.0, 0.0, on_ground=True), (51.0, 0.01, 0, 0, False)), {}, 0.0) is None

    db, engine, centers, _ = make(tmp_path)
    west = centers.on_frequency(127500, 51.0, 0.0)
    engine.flight.cleared_ft = 10000
    engine.set_plan(None)
    engine.handle(Intent("check_in"), west, None, busy)
    assert (
        engine.tick(west, None, busy).text
        == f"{CALLSIGN}, traffic, twelve o'clock, four miles, opposite direction, one thousand feet above"
    )
    assert engine.tick(west, None, busy) is None
    engine.traffic_enabled, engine._traffic_seen = False, {}
    assert engine.tick(west, None, busy) is None  # switched off in the settings


def test_tower_holds_a_departure_for_traffic_on_final(tmp_path):
    db, engine, _, _ = make(tmp_path)
    engine.flight.has_been_airborne = False
    tower = db.station(118100, 51.0, -1.0)
    holding = at(51.001, -0.98, on_ground=True)
    with_arrival = with_traffic(holding, (51.0, -0.93, 800, 270, False))  # two miles out for runway 27
    held = engine.handle(Intent("ready_for_departure"), tower, ISSUE, with_arrival)
    assert held.text == f"{CALLSIGN}, Westfield Tower, hold short runway two seven, traffic on a two mile final"
    assert engine.tick(tower, ISSUE, with_arrival) is None  # still on final
    rolling = with_traffic(holding, (51.0, -1.0, 100, 270, True))  # landed, still on the runway
    assert engine.tick(tower, ISSUE, rolling) is None
    clear = with_traffic(holding, (51.003, -1.0, 100, 0, True))  # vacated to the apron
    released = engine.tick(tower, ISSUE, clear)
    assert released.text == f"{CALLSIGN}, runway two seven, cleared for takeoff, wind two seven zero degrees one zero knots"
    assert engine.flight.phase.value == "cleared for takeoff" and engine.tick(tower, ISSUE, clear) is None


def test_tower_sequences_an_arrival_and_sends_it_around_if_the_runway_stays_occupied(tmp_path):
    db, engine, _, _ = make(tmp_path)
    tower = db.station(119100, 51.0, 5.3)
    three_miles = at(51.0, 5.45, feet=1200, heading=270)
    on_the_runway = (51.0, 5.35, 300, 270, True)
    told = engine.handle(Intent("position_report", position="final"), tower, ISSUE, with_traffic(three_miles, on_the_runway))
    assert told.text == f"{CALLSIGN}, Eastfield Tower, continue approach runway two seven, traffic on the runway"
    assert engine.tick(tower, ISSUE, with_traffic(at(51.0, 5.42, feet=900, heading=270), on_the_runway)) is None
    short_final = at(51.0, 5.39, feet=500, heading=270)
    around = engine.tick(tower, ISSUE, with_traffic(short_final, on_the_runway))
    assert around.text == f"{CALLSIGN}, go around, I say again, go around, traffic on the runway"
    assert engine.tick(tower, ISSUE, with_traffic(short_final, on_the_runway)) is None

    second = make(tmp_path)[1]
    four_miles = at(51.0, 5.47, feet=1500, heading=270)
    ahead = (51.0, 5.41, 700, 270, False)  # another aircraft a mile and a half from the threshold
    number_two = second.handle(Intent("position_report", position="final"), tower, ISSUE, with_traffic(four_miles, ahead))
    assert number_two.text == (
        f"{CALLSIGN}, Eastfield Tower, number two, continue approach runway two seven, wind two seven zero degrees one zero knots"
    )
    landed_and_gone = with_traffic(at(51.0, 5.43, feet=900, heading=270), (51.003, 5.35, 300, 0, True))
    assert second.tick(tower, ISSUE, landed_and_gone).text == (
        f"{CALLSIGN}, runway two seven, cleared to land, wind two seven zero degrees one zero knots"
    )
