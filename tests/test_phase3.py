"""Taxi routing, intent normalisation and the clearance rules engine."""

import pytest

from proatc import phraseology as say
from proatc.atis import Issue
from proatc.engine import Engine, Phase
from proatc.intents import Intent, parse_json
from proatc.navdata import AirportDB, build_index
from proatc.taxi import nearest_parking, parse_taxi, route_to_point, route_to_runway
from proatc.weather import Weather

# One east-west runway (09/27), a parallel taxiway A north of it with entries A1 (27 end),
# A2 (midfield intersection) and A3 (09 end), and an apron on taxiway B.
FIELD = """A
1200 test

1    100 0 0 EGXX Testfield
1302 icao_code EGXX
100 45.00 1 1 0.25 1 3 0  09 51.0000 -1.0200 0 0 3 2 1 0 27 51.0000 -0.9800 0 0 3 2 1 0
1201 51.0000 -0.9800 both 0 rwy27
1201 51.0000 -1.0000 both 1 rwymid
1201 51.0000 -1.0200 both 2 rwy09
1201 51.0010 -0.9800 both 3 holdA1
1201 51.0020 -0.9800 both 4 A_east
1201 51.0020 -1.0000 both 5 A_mid
1201 51.0020 -1.0200 both 6 A_west
1201 51.0010 -1.0200 both 7 holdA3
1201 51.0030 -1.0000 both 8 apron
1202 0 1 twoway runway 09/27
1202 1 2 twoway runway 09/27
1202 3 0 twoway taxiway_E A1
1204 departure 09,27
1202 4 3 twoway taxiway_E A1
1202 4 5 twoway taxiway_E A
1202 5 6 twoway taxiway_E A
1202 6 7 twoway taxiway_E A3
1202 7 2 twoway taxiway_E A3
1202 5 1 twoway taxiway_E A2
1202 5 8 twoway taxiway_E B
1300 51.0031 -1.0000 180.0 gate jets 12
1052 121900 Testfield Delivery
1053 121700 Testfield Ground
1054 118100 Testfield Tower
1056 125500 Testfield Departure
""".splitlines()

STAND = (51.0031, -1.0000)
ISSUE = Issue("C", Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1013.2), 12 * 3600)


@pytest.fixture
def db(tmp_path):
    apt = tmp_path / "apt.dat"
    apt.write_text("\n".join(FIELD), encoding="utf-8")
    build_index(apt, tmp_path / "a.sqlite")
    return AirportDB(tmp_path / "a.sqlite")


def state(lat=STAND[0], lon=STAND[1], on_ground=True, agl=0.0, speed=0.0):
    return {"tail_number": "G-ABCD", "lat": lat, "lon": lon, "on_ground": int(on_ground), "agl_m": agl, "groundspeed_ms": speed}


def test_taxiway_pronunciation():
    assert say.taxiway("A") == "Alfa"
    assert say.taxiway("N4W") == "November four Whiskey"
    assert say.taxiway("LINK56") == "Link five six" and say.taxiway("Link 41") == "Link four one"
    assert say.taxiway("Cargo Apron") == "Cargo Apron"
    assert say.join(["A"]) == "A" and say.join(["A", "B", "C"]) == "A, B and C"


def test_routes_use_full_length_entries_and_named_taxiways(db):
    airport = db.airport("EGXX")
    net = parse_taxi(db.record_lines(airport))
    assert len(net.nodes) == 9 and net.usable and nearest_parking(net, *STAND).name == "12"
    to_27 = route_to_runway(net, *STAND, "27", (51.0, -0.98))
    assert to_27.taxiways == ["B", "A", "A1"] and to_27.crossings == []
    to_09 = route_to_runway(net, *STAND, "09", (51.0, -1.02))
    assert to_09.taxiways == ["B", "A", "A3"]  # not the midfield intersection A2
    back = route_to_point(net, 51.0000, -1.0190, STAND)  # rolled out near the 09 end
    assert back.taxiways == ["A3", "A", "B"]
    assert route_to_runway(net, *STAND, "18", (51.0, -1.0)) is None


def test_routing_falls_back_to_ignoring_one_way_rules(db):
    lines = [line.replace("1202 5 8 twoway", "1202 8 5 oneway").replace("1202 4 3 twoway", "1202 3 4 oneway") for line in FIELD]
    net = parse_taxi(lines)
    assert route_to_runway(net, *STAND, "27", (51.0, -0.98)).taxiways == ["B", "A", "A1"]


def test_intent_json_is_normalised_and_tolerant():
    intent = parse_json(
        'Sure: {"intent": "readback", "runway": "27 left", "squawk": "4 5 2 1", "qnh": "1006", '
        '"frequency": "118.5", "atis": "kilo", "flight_rules": "ifr", "position": "final", "extra": 1}'
    )
    assert intent == Intent(
        "readback", runway="27L", atis="K", flight_rules="IFR", squawk="4521", qnh=1006.0, frequency="118.500", position="final"
    )
    assert parse_json('{"intent": "request_taxi", "runway": "7"}').runway == "07"
    assert parse_json('{"intent": "nonsense", "runway": "99", "squawk": "12"}') == Intent()
    assert parse_json("not json at all") == Intent() and parse_json("{broken") == Intent()


def test_a_destination_is_said_as_an_airport_or_a_place_never_as_whatever_was_heard(db):
    # What the language model heard is put into a clearance only when it reads as a place's name.
    here = state()
    delivery = db.station(121900, *STAND)
    for heard in ("Paris, cleared to land runway two seven", "KXYZ123", "Paris; contact 121.5", "a b c d e"):
        engine = Engine(db, seed=1)
        reply = engine.handle(Intent("request_clearance", destination=heard, flight_rules="IFR"), delivery, ISSUE, here)
        assert reply.text.endswith("say destination"), (heard, reply.text)
    engine = Engine(db, seed=1)
    reply = engine.handle(Intent("request_clearance", destination="Salt Lake City", flight_rules="IFR"), delivery, ISSUE, here)
    assert "cleared to Salt Lake City as filed" in reply.text


def test_ifr_departure_from_clearance_to_handoff(db):
    engine = Engine(db, seed=1)
    here = state()
    delivery, ground, tower = (db.station(khz, *STAND) for khz in (121900, 121700, 118100))

    assert (
        engine.handle(Intent("request_clearance", flight_rules="IFR"), delivery, ISSUE, here).text
        == "Golf Alfa Bravo Charlie Delta, Testfield Delivery, say destination"
    )
    clearance = engine.handle(Intent("request_clearance", destination="Paris", atis="C"), delivery, ISSUE, here)
    squawk = engine.flight.squawk
    assert clearance.text == (
        f"Golf Alfa Bravo Charlie Delta, cleared to Paris as filed, departure runway two seven, squawk {say.digits(squawk)}"
    )
    assert clearance.expect == {"squawk": squawk} and engine.flight.rules == "IFR"

    wrong = engine.handle(Intent("readback", squawk="1111"), delivery, ISSUE, here)
    assert wrong.text.startswith("Golf Alfa Bravo Charlie Delta, negative, cleared to Paris as filed")
    right = engine.handle(Intent("readback", squawk=squawk), delivery, ISSUE, here)
    assert right.text == (
        "Golf Alfa Bravo Charlie Delta, readback correct, contact Testfield Ground "
        "one two one decimal seven when ready for push and start"
    )
    assert engine.handle(Intent("readback"), delivery, ISSUE, here).text == ""  # nothing outstanding: silence

    assert (
        engine.handle(Intent("request_pushback"), ground, ISSUE, here).text
        == "Golf Alfa Bravo Charlie Delta, Testfield Ground, push back and start approved, Q N H one zero one three"
    )
    taxi = engine.handle(Intent("request_taxi"), ground, ISSUE, here)
    assert taxi.text == (
        "Golf Alfa Bravo Charlie Delta, taxi to holding point runway two seven "
        "via Bravo, Alfa and Alfa one, Q N H one zero one three"
    )
    assert engine.flight.phase == Phase.TAXI_OUT
    assert engine.handle(Intent("readback", runway="09"), ground, ISSUE, here).text.startswith(
        "Golf Alfa Bravo Charlie Delta, negative, taxi to holding point runway two seven"
    )
    assert engine.handle(Intent("say_again"), ground, ISSUE, here).text == taxi.text  # the instruction, not the correction
    assert engine.handle(Intent("readback", runway="09"), ground, ISSUE, here).text.startswith(
        "Golf Alfa Bravo Charlie Delta, negative, taxi"
    )  # still pending after a second wrong readback
    assert engine.handle(Intent("readback", runway="27"), ground, ISSUE, here).text == ""

    at_hold = state(51.0010, -0.9800)
    handoff = engine.handle(Intent("ready_for_departure"), ground, ISSUE, at_hold)
    assert handoff.text == "Golf Alfa Bravo Charlie Delta, contact Testfield Tower one one eight decimal one"
    assert handoff.expect == {"frequency": "118.100"}
    engine.handle(Intent("readback", frequency="118.1"), ground, ISSUE, at_hold)

    takeoff = engine.handle(Intent("ready_for_departure"), tower, ISSUE, at_hold)
    assert takeoff.text == (
        "Golf Alfa Bravo Charlie Delta, Testfield Tower, runway two seven, cleared for takeoff, "
        "wind two seven zero degrees one zero knots"
    )
    assert engine.flight.phase == Phase.TAKEOFF_CLEARED

    assert engine.tick(tower, ISSUE, at_hold) is None  # still on the ground
    climbing = state(51.0, -1.03, on_ground=False, agl=400, speed=80)
    departure = engine.tick(tower, ISSUE, climbing)
    assert departure.text == "Golf Alfa Bravo Charlie Delta, contact Testfield Departure one two five decimal five"
    assert engine.tick(tower, ISSUE, climbing) is None  # said once
    assert engine.flight.phase == Phase.AIRBORNE and engine.flight.runway is None


def test_vfr_circuit_landing_and_taxi_in(db):
    engine = Engine(db, seed=1)
    ground, tower = db.station(121700, *STAND), db.station(118100, *STAND)
    engine.handle(Intent("radio_check"), tower, ISSUE, state())  # parked: remembers the stand

    downwind = state(51.02, -1.0, on_ground=False, agl=300, speed=50)
    join = engine.handle(Intent("inbound", flight_rules="VFR"), tower, ISSUE, downwind)
    assert (
        # North of the runway, and the circuit for 27 is left-hand, to the south: across the field first.
        join.text == "Golf Alfa Bravo Charlie Delta, join overhead for left hand downwind runway two seven, "
        "Q N H one zero one three, report downwind"
    )
    assert (
        engine.handle(Intent("position_report", position="downwind"), tower, ISSUE, downwind).text
        == "Golf Alfa Bravo Charlie Delta, number one, report final runway two seven"
    )
    landing = engine.handle(Intent("position_report", position="final"), tower, ISSUE, downwind)
    assert (
        landing.text
        == "Golf Alfa Bravo Charlie Delta, runway two seven, cleared to land, wind two seven zero degrees one zero knots"
    )
    assert engine.flight.phase == Phase.LANDING_CLEARED

    rollout = state(51.0, -1.015, speed=45)
    assert engine.tick(tower, ISSUE, rollout) is None  # still fast
    slow = state(51.0, -1.019, speed=10)
    vacate = engine.tick(tower, ISSUE, slow)
    assert vacate.text == (
        "Golf Alfa Bravo Charlie Delta, vacate at the next available exit, "
        "when vacated contact Testfield Ground one two one decimal seven"
    )
    assert engine.flight.phase == Phase.LANDED and engine.tick(tower, ISSUE, slow) is None

    clear = state(51.0010, -1.0200)
    assert (
        engine.handle(Intent("runway_vacated"), tower, ISSUE, clear).text
        == "Golf Alfa Bravo Charlie Delta, contact Testfield Ground one two one decimal seven"
    )
    taxi_in = engine.handle(Intent("runway_vacated"), ground, ISSUE, clear)
    assert taxi_in.text == "Golf Alfa Bravo Charlie Delta, Testfield Ground, taxi to stand one two via Alfa three, Alfa and Bravo"
    assert engine.flight.phase == Phase.TAXI_IN


def test_touch_and_go_keeps_the_flight_in_the_circuit(db):
    engine = Engine(db, seed=1)
    tower = db.station(118100, *STAND)
    final = state(51.0, -0.95, on_ground=False, agl=100, speed=40)
    reply = engine.handle(Intent("request_touch_and_go"), tower, ISSUE, final)
    assert "cleared touch and go" in reply.text
    engine.tick(tower, ISSUE, state(51.0, -0.99, speed=35))
    assert engine.flight.phase == Phase.LANDING_CLEARED  # no "vacate" call during a touch and go
    assert engine.tick(tower, ISSUE, state(51.0, -0.99, speed=10)) is None


def test_only_unforeseen_speech_is_left_to_the_free_form_fallback(db):
    engine = Engine(db, seed=1)
    tower = db.station(118100, *STAND)
    assert engine.handle(Intent("other"), tower, ISSUE, state()) is None
    nothing_said = engine.handle(Intent("say_again"), tower, ISSUE, state())
    assert nothing_said.text == "Golf Alfa Bravo Charlie Delta, Testfield Tower, no instructions for you"
    assert (
        engine.handle(Intent("request_taxi"), tower, ISSUE, state()).text
        == "Golf Alfa Bravo Charlie Delta, contact Testfield Ground one two one decimal seven"  # named itself already
    )
    assert engine.summary() == {"phase": "parked", "next_frequency": "121.700"}  # the hand-off is offered as a one-click tune


def test_faa_phrasing():
    from proatc.atis import uses_faa_style

    assert uses_faa_style("KSEA")
    assert Engine._wind(ISSUE, 0, faa=True) == "wind two seven zero at one zero"
    assert Engine._pressure(ISSUE, faa=True) == ("altimeter two niner niner two", 29.92)
    assert Engine._pressure(ISSUE, faa=False) == ("Q N H one zero one three", 1013.0)


def test_suggested_calls_follow_the_flight_and_carry_their_intent(db):
    engine = Engine(db, seed=1)
    here = state()
    delivery, ground, tower = (db.station(khz, *STAND) for khz in (121900, 121700, 118100))

    def labels(station, raw=here):
        return [item["label"] for item in engine.suggestions(station, ISSUE, raw)]

    assert labels(delivery) == ["Request IFR clearance…", "Request VFR departure", "Radio check"]
    first = engine.suggestions(delivery, ISSUE, here)[0]
    assert first["needs"] == "destination" and "{destination}" in first["text"]
    assert first["text"].startswith(
        "Testfield Delivery, Golf Alfa Bravo Charlie Delta, information Charlie, request I F R clearance"
    )
    # Until the flight has a clearance, every ground-side frequency offers to ask for one.
    assert labels(ground) == [
        "Request IFR clearance…",
        "Request VFR departure",
        "Request push and start",
        "Request taxi",
        "Radio check",
    ]

    engine.handle(Intent("request_clearance", destination="Paris"), delivery, ISSUE, here)
    readback = engine.suggestions(delivery, ISSUE, here)[0]
    assert readback["label"] == "Read back" and readback["intent"] == {"intent": "readback", "squawk": engine.flight.squawk}
    assert readback["text"] == (
        f"Cleared to Paris as filed, departure runway two seven, squawk {say.digits(engine.flight.squawk)}, "
        "Golf Alfa Bravo Charlie Delta"
    )
    # Clicking it is a correct readback by construction.
    from proatc.server import to_intent

    assert engine.handle(to_intent(readback["intent"]), delivery, ISSUE, here).text.startswith(
        "Golf Alfa Bravo Charlie Delta, readback correct"
    )
    assert "Read back" not in labels(delivery) and "Say again" in labels(delivery)

    engine.handle(Intent("request_taxi"), ground, ISSUE, here)
    engine.handle(Intent("readback", runway="27"), ground, ISSUE, here)
    assert labels(ground) == ["Ready for departure", "Progressive taxi", "Return to stand", "Say again", "Radio check"]
    ready = [item for item in engine.suggestions(tower, ISSUE, here) if item["label"] == "Ready for departure"][0]
    assert ready["text"] == "Testfield Tower, Golf Alfa Bravo Charlie Delta, holding short runway two seven, ready for departure"

    airborne = state(51.02, -1.0, on_ground=False, agl=300, speed=50)
    engine.tick(tower, ISSUE, airborne)
    assert labels(tower, airborne)[:5] == ["Inbound for landing", "Downwind", "Final", "Touch and go", "Going around"]
    landed = state(51.0, -1.019, speed=5)
    engine.tick(tower, ISSUE, landed)
    assert "Runway vacated" in labels(tower, landed) and "Ready for departure" not in labels(tower, landed)


def test_key_is_saved_to_env_without_disturbing_other_lines(tmp_path, monkeypatch):
    from proatc import server

    monkeypatch.setattr(server, "PROJECT_DIR", tmp_path)
    (tmp_path / ".env").write_text("OTHER=1\nOPENROUTER_API_KEY=old\n", encoding="utf-8")
    server.save_key("new-key")
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "OTHER=1\nOPENROUTER_API_KEY=new-key\n"


def test_a_flight_that_starts_on_the_runway_gets_its_clearance_from_tower(db):
    engine = Engine(db, seed=1)
    tower, ground = db.station(118100, *STAND), db.station(121700, *STAND)
    airport = db.airport("EGXX")
    on_runway = state(51.0000, -0.9810)  # lined up near the threshold of 27
    assert (
        engine.on_runway(airport, on_runway)
        and not engine.on_runway(airport, state())
        and not engine.on_runway(airport, state(51.0010, -0.9800))
    )

    # At the stand, tower sends you to delivery, and the app offers that frequency.
    redirect = engine.handle(Intent("request_clearance", destination="Paris"), tower, ISSUE, state())
    assert (
        redirect.text
        == "Golf Alfa Bravo Charlie Delta, Testfield Tower, contact Testfield Delivery one two one decimal niner for clearance"
    )
    assert engine.summary()["next_frequency"] == "121.900"

    labels = [item["label"] for item in engine.suggestions(tower, ISSUE, on_runway)]
    assert labels[:2] == ["Read back", "Request IFR clearance…"] and "Ready for departure" in labels
    engine.flight.pending = None
    clearance = engine.handle(Intent("request_clearance", destination="Paris"), tower, ISSUE, on_runway)
    assert clearance.text.startswith(
        "Golf Alfa Bravo Charlie Delta, cleared to Paris as filed, departure runway two seven, squawk"
    )
    done = engine.handle(Intent("readback", squawk=engine.flight.squawk), tower, ISSUE, on_runway)
    assert done.text == "Golf Alfa Bravo Charlie Delta, readback correct, report ready for departure"
    assert "Request IFR clearance…" not in [item["label"] for item in engine.suggestions(tower, ISSUE, on_runway)]
    assert "cleared for takeoff" in engine.handle(Intent("ready_for_departure"), tower, ISSUE, on_runway).text
    del ground


def test_tuning_from_the_app_writes_the_selected_com_radio():
    import pytest

    from proatc.config import Config
    from proatc.voice_loop import Session
    from proatc.xpbridge import SimUnavailable

    class Bridge:
        def __init__(self, accepts=True, selection=6):
            self.values = {"com1_khz": 118505, "com2_khz": 121500, "com_selection": selection}
            self.accepts, self.writes = accepts, []

        def snapshot(self):
            return dict(self.values)

        def write(self, key, value):
            self.writes.append((key, value))
            if self.accepts:
                self.values[key] = value

    events = []
    session = Session(Config(), emit=events.append)
    session.bridge = Bridge()
    session.tune("121.905")
    assert session.bridge.writes == [("com1_khz", 121905)] and events[-1] == {"type": "info", "text": "COM1 tuned to 121.905"}
    session.bridge = Bridge(selection=7)
    session.tune("128.08")
    assert session.bridge.writes == [("com2_khz", 128080)]
    for bad in ("99.5", "140.0", "abc"):
        with pytest.raises((SimUnavailable, ValueError)):
            session.tune(bad)
    session.bridge = Bridge(accepts=False)
    with pytest.raises(SimUnavailable, match="did not take"):
        session.tune("121.905")
    session.config.sim.allow_tuning = False
    with pytest.raises(SimUnavailable, match="switched off"):
        session.tune("121.905")
