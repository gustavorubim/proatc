"""Faults read from the journal of a real flight, Boston to Washington Dulles:

- two hand-offs fifteen seconds apart (to the next centre, then to approach) left the pilot on the centre,
  which then had nothing to say but "unable" and "say intentions";
- the approach frequency, shared by several airports around the city, was answered by a neighbouring
  airport's controller, who gave vectors to that airport's runway;
- runways 1 to 9 of airports in the United States never found their approach, SID or STAR.
"""

import pytest

from proatc import navdata
from proatc.centers import CenterIndex, parse_atc
from proatc.config import Config
from proatc.engine import Engine
from proatc.flightplan import FlightPlan
from proatc.intents import Intent
from proatc.navdata import AirportDB, build_index, prefer_airports
from proatc.procedures import Approach, Procedure, Procedures, choose_approach, parse_cifp, use_airport_runway_names
from proatc.radio import current_station
from proatc.voice_loop import _answer
from tests.test_ifr import APT, ATC, CALLSIGN, CIFP_EAST, CIFP_WEST, PLAN, at
from tests.test_realism import APRON, ground_world, taxiing

# The same two centres as elsewhere, with their boundary 60 miles short of the destination instead of 127.
NEAR_BOUNDARY = [line.replace(" 2.0", " 3.8") for line in ATC]
WEST, EAST = 127500, 132100


@pytest.fixture(autouse=True)
def no_preference_left_over():
    prefer_airports([])
    yield
    prefer_airports([])


@pytest.fixture
def arriving(tmp_path):
    apt = tmp_path / "apt.dat"
    apt.write_text("\n".join(APT), encoding="utf-8")
    build_index(apt, tmp_path / "a.sqlite")
    db = AirportDB(tmp_path / "a.sqlite")
    db.centers = CenterIndex(parse_atc(NEAR_BOUNDARY))
    procedures = {"EGXX": parse_cifp(CIFP_WEST), "LFXX": parse_cifp(CIFP_EAST)}
    engine = Engine(db, seed=1, procedures=lambda ident: procedures.get(ident, parse_cifp([])), centers=db.centers)
    engine.set_plan(PLAN)
    engine.flight.has_been_airborne, engine.flight.origin_ident = True, "EGXX"
    destination = db.airport("LFXX")
    out = lambda miles, feet=13000: at(51.0, destination.lon - miles / (60 * 0.6293), feet=feet, heading=90, speed=230.0)  # noqa: E731
    first = out(100)
    west = current_station(db, {**first, "com1_khz": WEST})[1]
    engine.handle(Intent("check_in"), west, None, first)
    engine.flight.cleared_ft, engine.flight.descent_given, engine.flight.pending = 13000, True, None
    return db, engine, out


def tuned(db, khz, raw):
    return current_station(db, {**raw, "com1_khz": khz})[1]


def test_close_to_the_hand_off_to_approach_a_centre_does_not_send_the_flight_to_another_centre(arriving):
    db, engine, out = arriving
    # 66 miles out, a minute from the boundary. On the real flight: "contact Washington Center", and fifteen
    # seconds later "contact Dulles Approach", with the copilot already tuning the first.
    nearly = out(66)
    west = tuned(db, WEST, nearly)
    assert west.callsign == "Westland Control" and not west.center.contains(51.0, out(55)["lon"])  # the boundary is ahead
    assert engine.tick(west, None, nearly) is None
    assert engine.tick(tuned(db, WEST, out(50)), None, out(50)) is None  # across the boundary by now, still its flight
    said = engine.tick(tuned(db, WEST, out(44)), None, out(44))
    assert said.text == f"{CALLSIGN}, contact Eastfield Approach one two four decimal three"
    assert engine.flight.next_frequency == "124.300" and engine.flight.sector_ahead is None
    # Further out the boundary is handed over as usual.
    db, engine, out = arriving
    engine.flight.told_sector, engine.flight.told_approach, engine.flight.pending = None, False, None
    far = at(51.0, 3.6, feet=13000, heading=90, speed=230.0)
    assert "LFXX" and engine.flight.plan.destination == "LFXX"
    engine.flight.plan = FlightPlan("EGXX", "ZZZZ", [], 36000)  # nowhere near: no approach hand-off is coming
    handed = engine.tick(tuned(db, WEST, far), None, far)
    assert handed.text == f"{CALLSIGN}, contact Eastland Control one three two decimal one"


def test_a_centre_that_hears_from_a_flight_that_should_be_with_approach_sends_it_there(arriving):
    db, engine, out = arriving
    close = out(44)
    engine.tick(tuned(db, WEST, close), None, close)  # "contact approach", from the first centre
    engine.flight.pending = None
    # The pilot is on the other centre instead, as on the real flight. Then: "radar contact" and nothing else,
    # "unable, say again your request", "say intentions".
    east = tuned(db, EAST, out(40))
    checked_in = engine.handle(Intent("check_in"), east, None, out(40))
    assert checked_in.text == f"{CALLSIGN}, Eastland Control, identified, contact Eastfield Approach one two four decimal three"
    engine.flight.pending = None
    for asked in ("check_in", "request_descent", "request_approach", "other"):
        again = engine.handle(Intent(asked), east, None, out(36))
        assert again.text == f"{CALLSIGN}, contact Eastfield Approach one two four decimal three", asked
        engine.flight.pending = None
    assert engine.tick(east, None, out(35)) is None  # it has said so: not on every tick

    # A centre the pilot never called speaks up by itself once.
    db, engine, out = arriving
    engine.tick(tuned(db, WEST, close), None, close)
    engine.flight.pending = None
    engine.flight.contacted.add(east.key)
    assert engine.tick(east, None, out(40)).text == f"{CALLSIGN}, contact Eastfield Approach one two four decimal three"
    engine.flight.pending = None
    assert engine.tick(east, None, out(39)) is None


def test_a_check_in_with_nothing_to_add_is_answered(arriving):
    db, engine, out = arriving
    engine.flight.plan = FlightPlan("EGXX", "ZZZZ", [], 13000)
    west = tuned(db, WEST, out(100))
    assert engine.handle(Intent("check_in"), west, None, out(100)).text == f"{CALLSIGN}, roger"


# ---- a frequency that several airports share ------------------------------------

CITY = """A
1200 test

1    300 0 0 KBIG Metro Intl
1302 icao_code KBIG
100 45.00 1 1 0.25 1 3 0  1C 38.9000 -77.4500 0 0 3 2 1 0 19C 38.9300 -77.4500 0 0 3 2 1 0
100 45.00 1 1 0.25 1 3 0  12 38.9300 -77.5000 0 0 3 2 1 0 30 38.9200 -77.4700 0 0 3 2 1 0
1055 120450 POTOMAC APP (241-330)
1055 126100 POTOMAC APP (331-090)
1055 128525 POTOMAC APP (091-240)
1056 125050 POTOMAC DEP (300-010)
1054 120100 Metro Tower

1    400 0 0 KSML Smallfield
1302 icao_code KSML
100 23.00 1 1 0.25 1 3 0  17 39.0900 -77.5600 0 0 3 2 1 0 35 39.0700 -77.5600 0 0 3 2 1 0
1055 125050 Potomac Approach
1055 128525 Potomac Approach
""".splitlines()


@pytest.fixture
def city(tmp_path):
    apt = tmp_path / "city.dat"
    apt.write_text("\n".join(CITY), encoding="utf-8")
    build_index(apt, tmp_path / "city.sqlite")
    db = AirportDB(tmp_path / "city.sqlite")
    db.centers = None
    return db


def test_on_a_shared_frequency_the_station_is_the_one_at_the_flights_own_airport(city):
    near_small = {"lat": 39.05, "lon": -77.55, "com1_khz": 125050}  # two miles from Smallfield, nine from Metro
    assert current_station(city, near_small)[1].airport.ident == "KSML"  # with no plan: the nearest, as before
    engine = Engine(city, seed=1, procedures=lambda ident: parse_cifp([]))
    engine.set_plan(FlightPlan("KBOS", "KBIG", [], 38000))
    # On the real flight this was Smallfield's controller, and the vectors were to Smallfield's runway 17.
    station = current_station(city, near_small)[1]
    assert station.airport.ident == "KBIG"
    assert current_station(city, {**near_small, "com1_khz": 128525})[1].airport.ident == "KBIG"
    engine.set_plan(FlightPlan("KBOS", "KSML", [], 8000))
    assert current_station(city, near_small)[1].airport.ident == "KSML"
    engine.set_plan(None)
    assert navdata._PREFERRED == ()


def test_the_station_being_talked_to_does_not_change_as_the_aircraft_moves(city):
    far_side = {"lat": 38.80, "lon": -77.40, "com1_khz": 125050}  # nearer Metro
    assert current_station(city, far_side)[1].airport.ident == "KBIG"
    # Flying past Smallfield: on the real flight the station changed here, mid-approach.
    for lat in (38.95, 39.02, 39.08, 39.12):
        assert current_station(city, {"lat": lat, "lon": -77.55, "com1_khz": 125050})[1].airport.ident == "KBIG"
    # Out of range of everything, then back: the choice is made afresh.
    assert current_station(city, {"lat": 45.0, "lon": -77.55, "com1_khz": 125050})[1] is None
    assert current_station(city, {"lat": 39.08, "lon": -77.56, "com1_khz": 125050})[1].airport.ident == "KSML"


def test_the_approach_sector_for_the_side_of_the_airport_the_aircraft_is_on(city):
    engine = Engine(city, seed=1, procedures=lambda ident: parse_cifp([]))
    metro = city.airport("KBIG")
    north_east = {"lat": metro.lat + 0.5, "lon": metro.lon + 0.5, "heading_true": 200.0, "heading_mag": 200.0}
    assert engine._handoff(metro, "approach", north_east)[2] == "126.100"  # bearing 038 from the airport: 331-090
    south = {"lat": metro.lat - 0.6, "lon": metro.lon, "heading_true": 0.0, "heading_mag": 0.0}
    assert engine._handoff(metro, "approach", south)[2] == "128.525"  # 180: 091-240
    west = {"lat": metro.lat, "lon": metro.lon - 0.6, "heading_true": 90.0, "heading_mag": 100.0}
    assert engine._handoff(metro, "approach", west)[2] == "120.450"  # 270 true, 280 magnetic: 241-330
    assert engine._handoff(metro, "approach")[2] in ("120.450", "126.100", "128.525")  # no position: one of them, as before
    assert engine._handoff(metro, "tower", south)[2] == "120.100"  # no sectors: the only one
    assert engine._handoff(city.airport("KSML"), "approach", south)[2] in ("125.050", "128.525")  # no sectors named


# ---- runway names -----------------------------------------------------------------


def test_procedures_take_the_runway_names_of_the_airport(city):
    legs: list = []
    procedures = Procedures(
        sids={"DEP1": Procedure("DEP1", {"RW01C": legs, "RW12": legs, "ALL": legs})},
        stars={"ARR1": Procedure("ARR1", {"RW04B": legs, "ALFAX": legs})},
        approaches={"I01C": Approach("I01C", "I", "01C"), "I12": Approach("I12", "I", "12"), "R09": Approach("R09", "R", "09")},
    )
    assert choose_approach(procedures, "1C") is None  # as it was: never found
    use_airport_runway_names(procedures, ["1C", "19C", "12", "30", "4L", "4R", "9"])
    assert choose_approach(procedures, "1C").ident == "I01C" and choose_approach(procedures, "9").ident == "R09"
    assert choose_approach(procedures, "12").ident == "I12"
    assert list(procedures.sids["DEP1"].transitions) == ["RW1C", "RW12", "ALL"] and procedures.sids["DEP1"].serves("1C")
    assert list(procedures.stars["ARR1"].transitions) == ["RW4B", "ALFAX"] and procedures.stars["ARR1"].serves("4R")
    use_airport_runway_names(procedures, ["1C", "19C"])  # done twice: nothing changes
    assert procedures.approaches["I01C"].runway == "1C"
    # An airport that writes its runways with the zero keeps the names it has.
    european = Procedures(approaches={"I09L": Approach("I09L", "I", "09L")})
    use_airport_runway_names(european, ["09L", "27R"])
    assert european.approaches["I09L"].runway == "09L"

    # Through the engine: the approach to runway 1C is the one given, not a circle from another runway.
    loaded = Procedures(approaches={"I01C": Approach("I01C", "I", "01C"), "I12": Approach("I12", "I", "12")})
    engine = Engine(city, seed=1, procedures=lambda ident: loaded)
    assert choose_approach(engine.procedures("KBIG"), "1C").ident == "I01C"


# ---- a controller with no instruction to give -------------------------------------


class Model:
    def __init__(self, answer):
        self.answer = answer

    def reply(self, transcript, context, station_key="free", inform_only=False):
        return self.answer

    def remember(self, *args):
        pass


def test_when_the_controller_has_no_instruction_the_pilot_is_told_what_to_say(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    parked = taxiing(*APRON)
    events: list[dict] = []
    for answer in ("Golf Alfa Bravo Charlie Delta, roger, say intentions.", "Golf Alfa Bravo Charlie Delta, taxi to the moon"):
        context: dict = {}
        said = _answer(
            "what now", Intent("other"), context, ground.key, ground, None, parked, Model(answer), engine, None, events.append
        )
        assert said.endswith("say intentions.") or said.endswith("unable, say again your request")
        assert "request" in context["way_out"].lower() and context["way_out"].startswith("Testfield Ground")
    # A request the engine does answer needs no hint.
    context = {}
    _answer(
        "radio check", Intent("radio_check"), context, ground.key, ground, None, parked, Model("x"), engine, None, events.append
    )
    assert "way_out" not in context and Config() is not None


# ---- rules the worldwide flight sweep led to ---------------------------------------


def test_a_sector_across_the_date_line_does_not_cover_the_rest_of_the_world():
    pacific = """CONTROLLER
NAME PACIFIC OCEANIC
FACILITY_ID KZAK
ROLE ctr
FREQ 12790
AIRSPACE_POLYGON_BEGIN 0 60000
POINT 50.0 160.0
POINT 50.0 -150.0
POINT 20.0 -150.0
POINT 20.0 160.0
AIRSPACE_POLYGON_END
CONTROLLER_END
""".splitlines()
    index = CenterIndex(parse_atc(pacific))
    centre = index.centers[0]
    assert centre.polygons[0].wraps
    assert centre.contains(35.0, 175.0) and centre.contains(35.0, -175.0) and centre.contains(35.0, -155.0)
    assert not centre.contains(35.0, 10.0) and not centre.contains(35.0, -100.0) and not centre.contains(35.0, 150.0)
    assert index.at(50.0, 14.0) is None  # central Europe: on a real flight "Anchorage Radio" answered there
    assert centre.near(35.0, -149.5, 0.75) and not centre.near(35.0, 10.0, 0.75)


def test_an_airliner_in_the_cruise_is_not_talking_to_a_tower_far_below(city):
    overhead = {"lat": 38.93, "lon": -77.46, "com1_khz": 120100, "on_ground": 0}
    assert current_station(city, {**overhead, "elevation_m": 3000 * 0.3048})[1].kind == "tower"
    assert current_station(city, {**overhead, "elevation_m": 36000 * 0.3048})[1] is None
    approach = {**overhead, "com1_khz": 126100}
    assert current_station(city, {**approach, "elevation_m": 13000 * 0.3048})[1].kind == "approach"
    assert current_station(city, {**approach, "elevation_m": 30000 * 0.3048})[1] is None
    assert current_station(city, {**overhead, "on_ground": 1, "elevation_m": 95.0})[1].kind == "tower"  # on the ground: no limit


def test_an_airport_with_a_tower_and_no_approach_is_handed_over_inside_the_towers_range(arriving):
    db, engine, out = arriving
    destination = db.airport("LFXX")
    engine._handoff = lambda airport, kind, raw=None: (
        None if kind == "approach" else ("Eastfield Tower", "one one niner decimal one", "119.100") if kind == "tower" else None
    )
    east = tuned(db, EAST, out(44))
    engine.handle(Intent("check_in"), east, None, out(44))
    engine.flight.pending = None
    far = engine.tick(east, None, out(40))
    assert far is None or "contact" not in far.text  # too far out for a tower
    engine.flight.pending = None
    said = engine.tick(east, None, out(24))
    assert said.text == f"{CALLSIGN}, contact Eastfield Tower one one niner decimal one" and destination is not None
    # Nobody to hand the flight to at all: the pilot is released.
    engine.flight.told_approach, engine.flight.approach_told_by, engine.flight.pending = False, None, None
    engine._handoff = lambda airport, kind, raw=None: None
    assert engine.tick(east, None, out(20)).text == f"{CALLSIGN}, radar service terminated, frequency change approved"


def test_a_radar_controller_on_the_way_does_not_vector_the_flight_to_its_own_airport(city):
    engine = Engine(city, seed=1, procedures=lambda ident: parse_cifp([]))
    engine.set_plan(FlightPlan("KBOS", "KFAR", [], 24000))  # going somewhere else entirely
    engine.flight.has_been_airborne, engine.flight.origin_ident = True, "KBOS"
    passing = {"tail_number": "N123AB", "lat": 39.05, "lon": -77.55, "on_ground": 0, "elevation_m": 4000.0, "agl_m": 3900.0,
               "groundspeed_ms": 200.0, "heading_true": 90.0, "heading_mag": 90.0, "com1_khz": 125050}  # fmt: skip
    station = current_station(city, passing)[1]
    said = engine.handle(Intent("check_in"), station, None, passing, 0.0)
    assert said.text.endswith("radar contact") and engine.flight.arrival is None  # on a real flight: an ILS to this airport
    assert engine.tick(station, None, passing) is None
