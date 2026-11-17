"""Faults found by flying whole flights gate to gate (`proatc verify-flights --full`) on real X-Plane data.

Each test names the kind of airport or airspace that showed the fault; the flight that showed it is in the
pull request and in docs/testing.md.
"""

import math
import random
from types import SimpleNamespace

import pytest

from proatc import ifr
from proatc import phraseology as say
from proatc.atis import Issue, runways_in_use
from proatc.centers import CenterIndex, parse_atc
from proatc.engine import Engine, Phase, _end
from proatc.flightplan import FlightPlan
from proatc.intents import Intent
from proatc.navdata import AirportDB, Station, build_index, expect_station, prefer_airports
from proatc.procedures import parse_cifp
from proatc.radio import station_on
from proatc.taxi import assign_stand, parse_taxi, route_to_point, route_to_runway
from proatc.vectors import Position, joins_at, locate, next_vector
from proatc.weather import Weather
from tests.test_ifr import CALLSIGN, at, world  # noqa: F401  (a fixture)
from tests.test_realism import APRON, CROSSING, taxiing

ISSUE = Issue("C", Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1013.2), 12 * 3600)

APT = """A
1200 test

1    100 0 0 EGAA Sharefield
1302 icao_code EGAA
100 45.00 1 1 0.25 1 3 0  09 52.0000 -1.0200 0 0 3 2 1 0 27 52.0000 -0.9800 0 0 3 2 1 0
1054 118100 Sharefield Tower
1056 118100 Sharefield Departure
1055 132100 Sharefield Approach

1    5500 0 0 KHHH Highfield
1302 icao_code KHHH
100 45.00 1 1 0.25 1 3 0  09 40.0000 -105.0200 0 0 3 2 1 0 27 40.0000 -104.9800 0 0 3 2 1 0
1053 121900 Highfield Ground
1054 118900 Highfield Tower

1    100 0 0 EDHH Infofield
1302 icao_code EDHH
100 45.00 1 1 0.25 1 3 0  09 53.0000 9.9800 0 0 3 2 1 0 27 53.0000 10.0200 0 0 3 2 1 0
1054 125100 BREMEN INFO
1054 123250 Infofield Tower

1    100 0 0 EGTT Lonefield
1302 icao_code EGTT
100 45.00 1 1 0.25 1 3 0  09 56.0000 -0.0200 0 0 3 2 1 0 27 56.0000 0.0200 0 0 3 2 1 0
1054 119900 Lonefield Tower

1    100 0 0 EGDD Deptfield
1302 icao_code EGDD
100 45.00 1 1 0.25 1 3 0  09 56.0000 0.9800 0 0 3 2 1 0 27 56.0000 1.0200 0 0 3 2 1 0
1054 118300 Deptfield Tower
1056 124500 Deptfield Departure

1    100 0 0 EGNO Northfield
1302 icao_code EGNO
100 45.00 1 1 0.25 1 3 0  09 59.0000 -0.0200 0 0 3 2 1 0 27 59.0000 0.0200 0 0 3 2 1 0
1054 118700 Northfield Tower
""".splitlines()


def sector(name: str, facility: str, freqs: list[int], south: float, north: float, west: float, east: float) -> list[str]:
    corners = [(north, west), (north, east), (south, east), (south, west)]
    return [
        "CONTROLLER", f"NAME {name}", f"FACILITY_ID {facility}", "ROLE ctr", *(f"FREQ {freq}" for freq in freqs),
        "AIRSPACE_POLYGON_BEGIN 0 60000", *(f"POINT {lat} {lon}" for lat, lon in corners), "AIRSPACE_POLYGON_END",
        "CONTROLLER_END", "",
    ]  # fmt: skip


# Two centres on one frequency, side by side; a third north of a field that no centre covers.
ATC = (
    sector("WESTSIDE", "EGWW", [13210], 50.0, 54.0, -3.0, 2.0)
    + sector("EASTSIDE", "EDEE", [13210], 50.0, 54.0, 2.0, 8.0)
    + sector("NORTHSIDE", "EGNN", [12345], 56.15, 58.0, -3.0, 2.0)
    + sector("NORTH OCEANIC", "EGGX", [12790], 58.0, 62.0, -3.0, 2.0)
)


@pytest.fixture
def fields(tmp_path):
    (tmp_path / "apt.dat").write_text("\n".join(APT), encoding="utf-8")
    build_index(tmp_path / "apt.dat", tmp_path / "a.sqlite")
    db = AirportDB(tmp_path / "a.sqlite")
    db.centers = CenterIndex(parse_atc(ATC))
    yield db
    prefer_airports([])


def engine_for(db: AirportDB) -> Engine:
    return Engine(db, seed=1, procedures=lambda ident: parse_cifp([]), centers=db.centers)


def parked(lat: float, lon: float) -> dict:
    return {"tail_number": "N123AB", "lat": lat, "lon": lon, "on_ground": 1, "agl_m": 0.0, "groundspeed_ms": 0.0,
            "elevation_m": 30.0, "heading_true": 0.0, "heading_mag": 0.0}  # fmt: skip


# ---- who answers ---------------------------------------------------------------------------------------------


def test_on_the_ground_the_tower_answers_a_frequency_it_shares_with_departure(fields):
    db = fields
    assert db.station(118100, 52.0, -1.0).kind == "tower"  # was: Sharefield Departure, to a parked aircraft
    assert db.station(118100, 52.0, -1.0, feet=3000).kind == "tower"  # the station being talked to stays
    expect_station(118100, "EGAA:departure")  # until the flight is sent to the other one
    assert db.station(118100, 52.0, -1.0, feet=3000).kind == "departure"


def test_after_takeoff_the_departure_controller_answers_the_frequency_it_shares_with_the_tower(fields):
    db = fields
    engine = engine_for(db)
    tower = db.station(118100, 52.0, -1.0)
    flight = engine.flight
    flight.rules, flight.phase, flight.has_been_airborne, flight.origin_ident = "IFR", Phase.AIRBORNE, True, "EGAA"
    flight.contacted.add(tower.key)
    climbing = at(52.0, -0.95, feet=2000, heading=90, speed=120.0)
    assert engine.tick(tower, ISSUE, climbing).text == f"{CALLSIGN}, contact Sharefield Departure one one eight decimal one"
    assert station_on(db, 118100, climbing).kind == "departure"  # was: the tower again, which had nothing to say


def test_a_flight_sent_to_a_centre_is_answered_by_it_and_not_by_an_approach_on_the_same_frequency(fields):
    db = fields
    engine = engine_for(db)
    raw = at(52.1, -1.0, feet=15000, heading=90, speed=200.0)
    raw["com1_khz"] = 132100
    assert station_on(db, 132100, raw).kind == "approach"  # nobody sent there: the airport's own controller
    handed = ifr._centre_handoff(engine, raw, CALLSIGN)
    assert handed.text == f"{CALLSIGN}, contact Westside Control one three two decimal one"
    assert station_on(db, 132100, raw).callsign == "Westside Control"


def test_of_two_centres_on_one_frequency_the_one_the_flight_was_sent_to_answers_before_the_boundary(fields):
    centres = fields.centers
    east = next(centre for centre in centres.centers if centre.name == "EASTSIDE")
    assert centres.on_frequency(132100, 52.0, 1.8).center.name == "WESTSIDE"
    centres.expect(132100, east)  # handed to the next centre a minute and a half before its boundary
    assert centres.on_frequency(132100, 52.0, 1.8).center.name == "EASTSIDE"


def test_an_information_service_listed_as_a_tower_is_called_information_and_the_real_tower_is_handed_to(fields):
    airport = fields.airport("EDHH")
    assert Station(airport, "tower", 125100, "Landshut Info", 0.0).callsign == "Landshut Information"
    assert Station(airport, "tower", 125100, "BREMEN INFO", 0.0).callsign == "Bremen Information"  # not the airport's
    assert fields.station_of(airport, "tower").callsign == "Infofield Tower"  # not "BREMEN INFO", listed first


# ---- on the ground -------------------------------------------------------------------------------------------


def test_a_tower_without_a_ground_controller_approves_push_and_start(fields):
    db = fields
    engine = engine_for(db)
    tower = db.station(118100, 52.0, -1.0)
    labels = [call["label"] for call in engine.suggestions(tower, ISSUE, parked(52.0, -1.0))]
    assert "Request push and start" in labels  # offered, so it must be answered
    reply = engine.handle(Intent("request_pushback"), tower, ISSUE, parked(52.0, -1.0))
    assert reply.text.endswith("push back and start approved, Q N H one zero one three")


def test_the_initial_climb_is_above_an_airport_high_above_the_sea(fields):
    db = fields
    engine = engine_for(db)
    engine.set_plan(FlightPlan("KHHH", "EGAA", [], 24000))
    ground = db.station(121900, 40.0, -105.0)
    reply = engine.handle(Intent("request_clearance", flight_rules="IFR"), ground, ISSUE, parked(40.0, -105.0))
    assert reply.expect["altitude"] == 9000 and "climb and maintain niner thousand" in reply.text  # was five thousand


def test_departures_avoid_a_runway_no_taxiway_joins_but_not_one_the_scenery_leaves_untagged(tmp_path):
    untagged = [line for line in CROSSING if "runway 09L/27R" not in line]  # the network does not mention 09L/27R
    no_link = [line for line in CROSSING if "taxiway_E B1" not in line]  # it does, and no taxiway joins it
    for name, rows, expected in (("both", CROSSING, "27R"), ("untagged", untagged, "27R"), ("link", no_link, "27L")):
        (tmp_path / f"{name}.dat").write_text("\n".join(rows), encoding="utf-8")
        build_index(tmp_path / f"{name}.dat", tmp_path / f"{name}.sqlite")
        db = AirportDB(tmp_path / f"{name}.sqlite")
        engine = Engine(db, seed=1, procedures=lambda ident: parse_cifp([]))
        assert engine._departure_runway(db.airport("EGXX"), ISSUE, taxiing(*APRON)) == expected
    assert _end("07L") == _end("7L") and _end("07") == "7"  # scenery that writes "7" for "07"


def test_a_departure_runway_is_only_ever_one_in_use(tmp_path):
    no_link = [line for line in CROSSING if "taxiway_E B1" not in line]
    (tmp_path / "x.dat").write_text("\n".join(no_link), encoding="utf-8")
    build_index(tmp_path / "x.dat", tmp_path / "x.sqlite")
    db = AirportDB(tmp_path / "x.sqlite")
    engine = Engine(db, seed=1, procedures=lambda ident: parse_cifp([]))
    for wind in (90, 180, 270):
        weather = Issue("C", Weather(wind_dir_true=wind, wind_kt=30, qnh_hpa=1013.2), 12 * 3600)
        in_use = runways_in_use(db.airport("EGXX"), wind, 30)
        assert engine._departure_runway(db.airport("EGXX"), weather, taxiing(*APRON)) in in_use  # never out of wind


# Taxiways on either side of a runway that meet only on the runway, as at many small airports.
SPLIT = """1201 52.0000 -1.0000 both 0 apron
1201 52.0010 -1.0000 both 1 entry_west
1201 52.0010 -0.9900 both 2 entry_east
1201 52.0000 -0.9900 both 3 stand_side
1202 0 1 twoway taxiway_E A
1202 1 2 twoway runway 09/27
1202 2 3 twoway taxiway_E B
1300 52.0000 -0.9899 0.0 gate jets S1
1300 52.0300 -0.9500 0.0 gate jets FAR""".splitlines()


def test_a_taxi_route_is_never_made_along_a_runway():
    # Backtracking needs its own clearance from the tower, read back and checked against traffic: not modelled, so
    # where the taxiways meet only on the runway there is no route (the airport is a known limit), as before.
    network = parse_taxi(SPLIT)
    assert route_to_point(network, 52.0, -1.0, (52.0, -0.9899)) is None
    assert route_to_runway(network, 52.0, -1.0, "27", (52.001, -0.99)) is None  # not along 09/27 to its far end


def test_a_taxiway_the_scenery_names_none_is_not_spoken_as_none():
    rows = [row.replace("taxiway_E A", "taxiway_E None") for row in SPLIT]
    assert [edge.name for edge in parse_taxi(rows).edges[0]] == [""]


def test_an_arriving_aircraft_is_given_a_stand_the_taxiways_come_near():
    network = parse_taxi(SPLIT)
    stands = {assign_stand(network, "jets", None, (52.0, -0.97), random.Random(seed)).name for seed in range(12)}
    assert stands == {"S1"}  # never the one on an apron the network does not reach


# ---- in the air ----------------------------------------------------------------------------------------------


def test_the_tower_hands_a_departure_to_the_centre_it_climbs_into_when_none_covers_the_airport(fields):
    db = fields
    engine = engine_for(db)
    tower = db.station(119900, 56.0, 0.0)
    flight = engine.flight
    flight.rules, flight.phase, flight.has_been_airborne, flight.origin_ident = "IFR", Phase.AIRBORNE, True, "EGTT"
    flight.contacted.add(tower.key)
    southbound = at(55.95, 0.0, feet=2000, heading=180, speed=120.0)
    assert engine.tick(tower, ISSUE, southbound) is None and not flight.told_departure  # no centre here, nor ahead
    turned = at(55.95, 0.05, feet=4000, heading=0, speed=120.0)  # the SID turns north, toward Northside's airspace
    handed = engine.tick(tower, ISSUE, turned)
    assert handed.text == f"{CALLSIGN}, contact Northside Control one two three decimal four five"
    assert station_on(db, 123450, {**turned, "com1_khz": 123450}).callsign == "Northside Control"  # it answers


def test_an_arrival_at_another_tower_is_not_handed_off_as_a_departure(fields):
    db = fields
    engine = engine_for(db)
    tower = db.station(118100, 52.0, -1.0)
    flight = engine.flight
    # From a field where no centre was ever found, so the departure hand-off was never made.
    flight.rules, flight.phase, flight.has_been_airborne, flight.origin_ident = "IFR", Phase.AIRBORNE, True, "EGTT"
    flight.contacted.add(tower.key)
    assert engine.tick(tower, ISSUE, at(52.0, -0.9, feet=2000, heading=270, speed=80.0)) is None


def test_a_flight_cleared_for_the_approach_is_not_told_it_bust_its_level_on_the_glide_path(world):  # noqa: F811
    db, engine, _ = world
    approach = db.station(124300, 51.0, 5.5, feet=3000)
    flight = engine.flight
    flight.contacted.add(approach.key)
    flight.cleared_ft = 2500
    flight.arrival = ifr.Arrival("LFXX", "27", "I L S", 2500, 270.0, (51.0, 5.37), stage="intercept")
    descending = {**at(51.0, 5.45, feet=1500, heading=270), "vs_fpm": -700.0}
    assert ifr._watch(engine, approach, descending, CALLSIGN) is None  # was: "check your level, cleared ..."
    flight.arrival.stage = "base"
    assert "check your level" in ifr._watch(engine, approach, descending, CALLSIGN).text


def test_a_procedure_flown_from_far_to_the_side_is_handed_to_tower_only_near_the_centreline(world):  # noqa: F811
    db, engine, _ = world
    flight = engine.flight
    flight.has_been_airborne, flight.origin_ident, flight.cleared_ft = True, "EGXX", 2500
    approach = db.station(124300, 51.0, 5.5, feet=3000)
    flight.contacted.add(approach.key)
    flight.arrival = ifr.Arrival("LFXX", "27", "R NAV", 2500, 270.0, (51.0, 5.37), stage="procedure", final_nm=10)
    abeam = at(50.4, 5.45, feet=4000, heading=0)  # three miles out along the approach, but 36 miles to the side
    assert ifr.tick(engine, approach, ISSUE, abeam, 0.0, CALLSIGN) is None  # was: "contact Eastfield Tower", out of its range
    on_final = at(51.0, 5.50, feet=1800, heading=270)
    assert ifr.tick(engine, approach, ISSUE, on_final, 0.0, CALLSIGN).text.startswith(f"{CALLSIGN}, contact Eastfield Tower")


def test_an_intercept_is_given_only_where_it_joins_final_in_time():
    # Final at nine or ten miles: a 30 degree intercept from 3.5 miles off meets the course six miles further in.
    for along, cross, final in ((6.2, 3.4, 9), (7.0, 3.5, 9), (8.1, 3.5, 10)):
        late = Position(math.hypot(along, cross), along, cross, "left")
        assert next_vector(late, 90.0, 3000, 3000, final_nm=final, on_base=True).stage != "intercept"
    on_time = Position(14.5, 14.1, 3.4, "left")
    vector = next_vector(on_time, 90.0, 3000, 3000, final_nm=10, on_base=True)
    assert vector.stage == "intercept" and joins_at(on_time) >= 7.0
    drifted = Position(7.0, 6.5, 2.0, "left")  # a base leg that drifted far in: out again, for a new base
    out = next_vector(drifted, 90.0, 3000, 3000, final_nm=10, on_base=True)
    assert (out.stage, out.again) == ("downwind", True)


def test_an_aircraft_far_to_the_side_of_final_gets_a_base_leg_not_a_dogleg_through_the_centreline():
    wide = Position(distance_nm=29.0, along_nm=14.2, cross_nm=25.6, side="right")
    vector = next_vector(wide, 321.0, 3000, 12000, inbound=True)
    assert (vector.stage, vector.heading_true) == ("base", 231.0)  # 90 degrees; 45 would cross final past the runway
    close = Position(distance_nm=19.6, along_nm=19.0, cross_nm=5.0, side="right")
    assert next_vector(close, 321.0, 3000, 12000, inbound=True).heading_true == 276.0  # still a dogleg when it fits


def test_a_wide_downwind_leg_is_turned_onto_base_even_beyond_vectoring_range():
    wide = Position(distance_nm=33.6, along_nm=20.0, cross_nm=27.0, side="right")
    assert next_vector(wide, 270.0, 3000, 11000, final_nm=15, vectored=True).stage == "base"  # was: "descend", silence
    assert next_vector(wide, 270.0, 3000, 11000, final_nm=15, inbound=True).stage == "descend"  # not yet vectored


def test_no_vector_beyond_the_sector_altitude_circle_goes_below_the_terrain_there(world):  # noqa: F811
    db, engine, _ = world
    approach = db.station(124300, 51.0, 5.5, feet=9000)
    engine.navfix = SimpleNamespace(airport_safe_ft=lambda ident: None, grid_safe_ft=lambda lat, lon: 9000)
    flight = engine.flight
    flight.has_been_airborne, flight.origin_ident, flight.cleared_ft = True, "EGXX", 12000
    flight.arrival = ifr.Arrival("LFXX", "27", "I L S", 2500, 270.0, (51.0, 5.37), stage="downwind", final_nm=10)
    wide = at(51.45, 5.37 + 25 / 37.7, feet=12000, heading=90)  # 25 miles out along the approach, 27 to the side
    reply = ifr._vector(engine, approach, ISSUE, wide, 0.0, CALLSIGN, "", first=False)
    assert reply.expect["altitude"] == 9000  # was: 2,500 ft, the joining altitude, 37 miles from the runway


def test_an_initial_approach_fix_beyond_the_runway_is_not_flown_to(world):  # noqa: F811
    db, engine, _ = world
    airport = db.airport("LFXX")
    fixes = {"FARSD": (51.0, 4.9), "NEARS": (51.0, 5.75)}  # west of the field, and out on the approach to 27
    engine.navfix = SimpleNamespace(position=lambda fix, lat, lon, within=400: fixes.get(fix))
    approach = SimpleNamespace(kind="R", initial_fixes=lambda: [("FARSD", 4000), ("NEARS", 4000)])
    arrival = ifr.Arrival("LFXX", "27", "R NAV", 2500, 270.0, (51.0, 5.37), procedure=approach)
    from_the_west = at(51.0, 4.5, feet=9000, heading=90)
    position = locate(from_the_west["lat"], from_the_west["lon"], arrival.threshold, arrival.course_true)
    assert ifr._initial_fix(engine, airport, arrival, from_the_west, position)[0] == "NEARS"  # was: FARSD, nearer


def test_a_waypoint_with_digits_in_its_name_is_spelled():
    assert ifr._fix_word(SimpleNamespace(navaids=None), "AA690", 12.7, 44.5) == "Alfa Alfa six niner zero"
    assert ifr._fix_word(SimpleNamespace(navaids=None), "CAYAL", 35.6, -106.0) == "Cayal"


def test_an_approach_altitude_under_the_airport_is_raised_above_it():
    high = SimpleNamespace(elev_ft=3287.0)
    assert ifr._platform(SimpleNamespace(platform_ft=2500), high) == 4800  # procedure data for another elevation
    assert ifr._platform(SimpleNamespace(platform_ft=6000), high) == 6000
    assert ifr._platform(None, high) == 6500


def test_an_r_nav_approach_is_never_cleared_below_the_minimum_sector_altitude(world):  # noqa: F811
    db, engine, _ = world
    approach = db.station(124300, 51.0, 5.5, feet=9000)
    fixes = {"NEARS": (51.0, 5.75)}
    engine.navfix = SimpleNamespace(position=lambda fix, lat, lon, within=400: fixes.get(fix), grid_safe_ft=lambda *_: 0)
    procedure = SimpleNamespace(kind="R", initial_fixes=lambda: [("NEARS", 4000)])
    engine.flight.arrival = ifr.Arrival("LFXX", "27", "R NAV", 2500, 270.0, (51.0, 5.37), procedure=procedure)
    raw = at(51.0, 6.2, feet=22000, heading=270)
    position = locate(raw["lat"], raw["lon"], (51.0, 5.37), 270.0)
    # Direct to the fix from wherever the aircraft is: the sector altitude, 14,400 ft, not the fix's own 4,000.
    reply = ifr._via_initial_fix(engine, approach, ISSUE, raw, CALLSIGN, "", position, 14400, False)
    assert reply.expect["altitude"] == 14400


def test_a_departure_controller_hands_on_to_the_centre_ahead_when_none_covers_the_airport(fields):
    db = fields
    engine = engine_for(db)
    engine.set_plan(FlightPlan("EGDD", "EGNO", [], 24000))  # north, into Northside's airspace
    departure = db.station(124500, 56.0, 1.0, feet=20000)
    flight = engine.flight
    flight.rules, flight.has_been_airborne, flight.origin_ident, flight.cleared_ft = "IFR", True, "EGDD", 24000
    flight.contacted.add(departure.key)
    topping_out = at(56.05, 1.0, feet=23500, heading=0, speed=200.0)
    assert ifr.tick(engine, departure, ISSUE, topping_out, 0.0, CALLSIGN).text == (
        f"{CALLSIGN}, contact Northside Control one two three decimal four five"
    )  # was: nothing, until the departure frequency went silent sixty miles out


def test_a_departure_is_not_handed_to_a_centre_it_only_points_at_in_the_climb_out(fields):
    db = fields
    engine = engine_for(db)
    engine.set_plan(FlightPlan("EGTT", "EGAA", [], 12000))  # south, away from Northside
    tower = db.station(119900, 56.0, 0.0)
    flight = engine.flight
    flight.rules, flight.phase, flight.has_been_airborne, flight.origin_ident = "IFR", Phase.AIRBORNE, True, "EGTT"
    flight.contacted.add(tower.key)
    climbing_out = at(56.02, -0.01, feet=2000, heading=330, speed=120.0)  # before the turn south
    assert engine.tick(tower, ISSUE, climbing_out) is None  # was: "contact Northside Control", a centre it never enters


def test_a_centre_gives_no_level_to_an_aircraft_outside_its_airspace(fields):
    db = fields
    engine = engine_for(db)
    engine.set_plan(FlightPlan("EGTT", "EGAA", [], 24000))
    north = next(centre for centre in db.centers.centers if centre.name == "NORTHSIDE")
    radio = db.centers.station_for(north)
    flight = engine.flight
    flight.rules, flight.has_been_airborne, flight.origin_ident, flight.cleared_ft = "IFR", True, "EGTT", 6000
    outside = at(55.6, 0.0, feet=6000, heading=180, speed=200.0)  # thirty miles south of its boundary
    reply = engine.handle(Intent("check_in"), radio, None, outside)
    assert "climb" not in reply.text  # was: "identified, climb flight level one two zero"


def test_an_oceanic_sector_gives_the_descent_to_a_destination_inside_it(fields):
    db = fields
    engine = engine_for(db)
    engine.set_plan(FlightPlan("EGAA", "EGTT", [], 36000))
    ocean = next(centre for centre in db.centers.centers if centre.oceanic)
    radio = db.centers.station_for(ocean)
    flight = engine.flight
    flight.rules, flight.has_been_airborne, flight.origin_ident, flight.cleared_ft = "IFR", True, "EGAA", 36000
    flight.contacted.add(radio.key)
    southbound = at(58.2, 0.0, feet=36000, heading=180, speed=230.0)  # 132 miles out
    assert ifr.tick(engine, radio, None, southbound, 0.0, CALLSIGN) is None
    nearer = at(57.9, 0.0, feet=36000, heading=180, speed=230.0)  # 114 miles: a three degree path starts here
    reply = ifr.tick(engine, radio, None, nearer, 0.0, CALLSIGN)
    assert reply.text == f"{CALLSIGN}, descend flight level two six zero"  # was: the cruise level, all the way in


def test_runways_crossed_where_two_runways_meet_are_both_cleared():
    rows = """1201 52.0000 -1.0000 both 0 apron
1201 52.0010 -1.0000 both 1 intersection
1201 52.0020 -1.0000 both 2 beyond
1201 52.0010 -1.0010 both 3 west
1201 52.0015 -1.0005 both 4 north_west
1202 0 1 twoway taxiway_E A
1202 1 2 twoway taxiway_E A
1202 3 1 twoway runway 08/26
1202 1 4 twoway runway 01/19""".splitlines()
    route = route_to_point(parse_taxi(rows), 52.0, -1.0, (52.002, -1.0))
    assert sorted(route.crossings) == ["01/19", "08/26"]  # was: only the first one listed for that node


def test_a_stand_name_with_two_spaces_is_said_with_one():
    assert say.stand("Ga Ramp  3") == "Ga Ramp 3"


def test_the_tower_clears_the_flight_to_land_on_the_runway_approach_brought_it_to(world):  # noqa: F811
    db, engine, _ = world
    airport = db.airport("LFXX")
    # Light wind from the west: 27 is in use. Approach brought the flight straight in to 09 (no approach to 27).
    light = Issue("C", Weather(wind_dir_true=270, wind_kt=5, qnh_hpa=1013.2), 12 * 3600)
    engine.flight.arrival = ifr.Arrival("LFXX", "09", "V O R", 2500, 90.0, (51.0, 5.33))
    assert engine._landing_runway(airport, light, at(51.0, 5.25, feet=1500), Intent("established")) == "09"
    # Once the wind is too strong behind it, not that runway.
    assert engine._landing_runway(airport, ISSUE, at(51.0, 5.25, feet=1500), Intent("established")) == "27"


def test_far_out_and_low_an_aircraft_cleared_for_the_approach_still_gets_the_alerts(world):  # noqa: F811
    db, engine, _ = world
    approach = db.station(124300, 51.0, 5.5, feet=3000)
    flight = engine.flight
    flight.cleared_ft = 3500
    for stage in ("procedure", "intercept"):
        flight.bust_called_for, flight.low_altitude_called = None, False
        flight.arrival = ifr.Arrival("LFXX", "27", "R NAV", 2500, 270.0, (51.0, 5.37), stage=stage, final_nm=10)
        far_and_low = {**at(51.0, 5.37 + 33 / 37.7, feet=500, heading=270), "vs_fpm": -1500.0}
        assert "check your level" in ifr._watch(engine, approach, far_and_low, CALLSIGN).text  # was: silent


def test_a_new_flight_forgets_who_the_last_one_was_sent_to(fields):
    db = fields
    engine = engine_for(db)
    on_stand = {**parked(52.0, -1.0), "com1_khz": 118100}
    tower = station_on(db, 118100, on_stand)
    flight = engine.flight
    flight.rules, flight.phase, flight.has_been_airborne, flight.origin_ident = "IFR", Phase.AIRBORNE, True, "EGAA"
    flight.contacted.add(tower.key)
    climbing = at(52.0, -0.95, feet=2000, heading=90, speed=120.0)
    engine.tick(tower, ISSUE, climbing)  # "contact Sharefield Departure", on the frequency it shares with the tower
    engine._observe(climbing)
    engine.reset()  # the aircraft is put back on its stand in the sim
    again = station_on(db, 118100, on_stand)
    assert again.kind == "tower"  # was: the departure controller, which did not answer "request taxi"
    assert engine.handle(Intent("request_taxi"), again, ISSUE, on_stand) is not None
