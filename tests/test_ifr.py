"""Procedures, en-route centres, vectors and the instrument flight from clearance to the approach."""

import pytest

from proatc import phraseology as say
from proatc.atis import Issue
from proatc.centers import CenterIndex, parse_atc
from proatc.engine import Engine
from proatc.flightplan import FlightPlan, PlanError, from_simbrief, manual
from proatc.intents import Intent, parse_json
from proatc.navdata import AirportDB, build_index
from proatc.procedures import Procedure, choose_approach, choose_sid, choose_star, parse_cifp, route_fixes, spoken_name
from proatc.vectors import established, locate, next_vector, turn_direction
from proatc.weather import Weather

# Two airports 240 NM apart on the same parallel, each with one east-west runway.
APT = """A
1200 test

1    100 0 0 EGXX Westfield
1302 icao_code EGXX
100 45.00 1 1 0.25 1 3 0  09 51.0000 -1.0200 0 0 3 2 1 0 27 51.0000 -0.9800 0 0 3 2 1 0
1052 121900 Westfield Delivery
1053 121700 Westfield Ground
1054 118100 Westfield Tower
1056 125500 Westfield Departure

1    300 0 0 LFXX Eastfield
1302 icao_code LFXX
100 45.00 1 1 0.25 1 3 0  09 51.0000 5.3300 0 0 3 2 1 0 27 51.0000 5.3700 0 0 3 2 1 0
1054 119100 Eastfield Tower
1055 124300 Eastfield Approach
""".splitlines()

CIFP_WEST = """SID:010,2,ALFA1A,RW27, , , , ,    , ,   ,CA, , , , , ,      ,    ,    ,2700,    ,+,00600,     ,06000, ,   ,    ,   , , , , , , , , ;
SID:020,2,ALFA1A,RW27,ALFAX,EG,E,A,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,05000,     ,     , ,   ,    ,   , , , , , , , , ;
SID:010,2,ALFA2B,RW09,ALFAX,EG,E,A,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,04000,     ,     , ,   ,    ,   , , , , , , , , ;
SID:010,2,BRV3C,RW27,BRV,EG,D, ,V   , ,   ,TF, , , , , ,      ,    ,    ,    ,    ,+,04000,     ,     , ,   ,    ,   , , , , , , , , ;""".splitlines()

CIFP_EAST = """STAR:010,2,DELT1E,ALL,DELTA,LF,E,A,E   , ,   ,IF, , , , , ,      ,    ,    ,    ,    , ,FL100,     ,     , ,   ,    ,   , , , , , , , , ;
STAR:020,2,DELT1E,ALL,ECHOX,LF,E,A,E   , ,   ,TF, , , , , ,      ,    ,    ,    ,    , ,     ,     ,     , ,   ,    ,   , , , , , , , , ;
APPCH:010,A,I27,ECHOX,ECHOX,LF,E,A,E   , ,   ,IF, , , , , ,      ,    ,    ,    ,    , ,04000,     ,     , ,   ,    ,   , , , , , ,0,D,S;
APPCH:020,A,I27,ECHOX,CF27,LF,P,C,E   , ,   ,CF, , , , , ,      ,    ,    ,    ,    , ,02500,     ,     , ,   ,    ,   , , , , , ,0,D,S;
APPCH:010,I,I27, ,CF27,LF,P,C,E   , ,   ,IF, , , , , ,      ,    ,    ,    ,    , ,02500,     ,     , ,   ,    ,   , , , , , ,0,D,S;
APPCH:020,I,I27, ,RW27,LF,P,G,G   , ,   ,CF, , , , , ,      ,    ,    ,    ,    , ,00350,     ,     , ,   ,    ,   , , , , , ,0,D,S;
APPCH:010,R,R27, ,RW27,LF,P,G,G   , ,   ,CF, , , , , ,      ,    ,    ,    ,    , ,00350,     ,     , ,   ,    ,   , , , , , ,0,D,S;
APPCH:010,D,D09, ,RW09,LF,P,G,G   , ,   ,CF, , , , , ,      ,    ,    ,    ,    , ,00350,     ,     , ,   ,    ,   , , , , , ,0,D,S;""".splitlines()

ATC = """A
1000
ATCFILE

CONTROLLER
NAME WESTLAND
FACILITY_ID EGTT
ROLE ctr
FREQ 12750
AIRSPACE_POLYGON_BEGIN 0 60000
POINT 53.0 -3.0
POINT 53.0 2.0
POINT 49.0 2.0
POINT 49.0 -3.0
AIRSPACE_POLYGON_END
CONTROLLER_END

CONTROLLER
NAME EASTLAND
FACILITY_ID LFFF
ROLE ctr
FREQ 13210
FREQ 12860
AIRSPACE_POLYGON_BEGIN 0 60000
POINT 53.0 2.0
POINT 53.0 8.0
POINT 49.0 8.0
POINT 49.0 2.0
AIRSPACE_POLYGON_END
CONTROLLER_END

CONTROLLER
NAME WESTFIELD TOWER
FACILITY_ID EGXX
ROLE twr
FREQ 11810
AIRSPACE_POLYGON_BEGIN 0 3000
POINT 51.1 -1.1
POINT 51.1 -0.9
POINT 50.9 -0.9
AIRSPACE_POLYGON_END
CONTROLLER_END
""".splitlines()

ISSUE = Issue("C", Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1013.2), 12 * 3600)
PLAN = FlightPlan("EGXX", "LFXX", "ALFAX UL9 BRAVO UM605 DELTA".split(), 36000, source="simbrief")
CALLSIGN = "Golf Alfa Bravo Charlie Delta"


@pytest.fixture
def world(tmp_path):
    apt = tmp_path / "apt.dat"
    apt.write_text("\n".join(APT), encoding="utf-8")
    build_index(apt, tmp_path / "a.sqlite")
    db = AirportDB(tmp_path / "a.sqlite")
    procedures = {"EGXX": parse_cifp(CIFP_WEST), "LFXX": parse_cifp(CIFP_EAST)}
    centers = CenterIndex(parse_atc(ATC))
    engine = Engine(db, seed=1, procedures=lambda ident: procedures.get(ident, parse_cifp([])), centers=centers)
    engine.set_plan(PLAN)
    return db, engine, centers


def at(lat, lon, feet=0.0, heading=270.0, on_ground=False, speed=120.0):
    return {"tail_number": "G-ABCD", "lat": lat, "lon": lon, "on_ground": int(on_ground), "elevation_m": feet * 0.3048,
            "agl_m": 0.0 if on_ground else feet * 0.3048, "groundspeed_ms": 0.0 if on_ground else speed,
            "heading_mag": heading, "heading_true": heading}  # fmt: skip


# ---- data -------------------------------------------------------------------


def test_level_and_heading_phraseology():
    assert say.level(24000, 6000) == "flight level two four zero"
    assert say.level(6000, 6000) == "altitude six thousand feet"  # at the transition altitude: still an altitude
    assert say.level(7000, 6000) == "flight level zero seven zero"
    assert say.level(5000, 6000) == "altitude five thousand feet"
    assert say.level(5000, 18000, faa=True) == "five thousand"
    assert say.level(17000, 18000, faa=True) == "one seven thousand"
    assert say.heading(90) == "zero niner zero" and say.heading(0) == "three six zero"


def test_cifp_parsing_and_procedure_choice():
    west, east = parse_cifp(CIFP_WEST), parse_cifp(CIFP_EAST)
    assert set(west.sids) == {"ALFA1A", "ALFA2B", "BRV3C"} and west.transition_altitude_ft == 6000
    assert west.sids["ALFA1A"].runways() == {"27"} and west.sids["ALFA1A"].top_altitude() == 5000
    assert west.sids["BRV3C"].top_altitude() is None  # only an "at or above" restriction
    assert route_fixes("ALFAX UL9 BRAVO DCT N0450F360 DELTA/N0440F340 UM605".split()) == ["ALFAX", "BRAVO", "DELTA"]

    assert choose_sid(west, "27", PLAN.route).name == "ALFA1A"
    assert choose_sid(west, "09", PLAN.route).name == "ALFA2B"
    assert choose_sid(west, "27", PLAN.route, filed="BRV3C").name == "BRV3C"
    assert choose_sid(west, "27", PLAN.route, filed="ALFA2B").name == "ALFA1A"  # filed SID does not serve the runway
    assert choose_sid(west, "27", ["ZZZZZ"]) is None

    assert choose_star(east, PLAN.route).name == "DELT1E" and choose_star(east, ["NOPE"]) is None
    ils = choose_approach(east, "27")
    assert (ils.ident, ils.spoken, ils.platform_ft) == ("I27", "I L S", 2500)  # ILS preferred over RNAV
    assert choose_approach(east, "09").spoken == "V O R D M E" and choose_approach(east, "18") is None

    assert spoken_name(west.sids["ALFA1A"]) == "Alfax one Alfa"
    assert spoken_name(east.stars["DELT1E"]) == "Delta one Echo"
    assert spoken_name(Procedure("XY")) == "X-ray Yankee"


def test_centres_by_position_and_frequency():
    centers = CenterIndex(parse_atc(ATC))
    assert [c.name for c in centers.centers] == ["WESTLAND", "EASTLAND"]  # the tower is not a centre
    assert centers.at(51.0, 0.0, 30000).callsign == "Westland Control"
    assert centers.at(51.0, 3.0).callsign == "Eastland Control" and centers.at(40.0, 0.0) is None
    station = centers.on_frequency(132100, 51.0, 3.0)
    assert (station.callsign, station.kind, station.key, station.airport) == (
        "Eastland Control",
        "center",
        "CTR:LFFF:EASTLAND",
        None,
    )
    assert centers.on_frequency(132100, 51.0, 0.0) is None  # right frequency, wrong airspace
    assert centers.on_frequency(128605, 51.0, 3.0).khz == 128605


def test_flight_plan_sources():
    plan = from_simbrief({
        "general": {"route": "ALFAX UL9 BRAVO", "initial_altitude": "36000", "sid_ident": {}, "star_ident": "DELT1E"},
        "origin": {"icao_code": "egxx", "plan_rwy": "27"},
        "destination": {"icao_code": "LFXX", "plan_rwy": {}},
    })  # fmt: skip
    assert (plan.origin, plan.destination, plan.cruise_ft, plan.sid, plan.star) == ("EGXX", "LFXX", 36000, None, "DELT1E")
    assert plan.departure_runway == "27" and plan.arrival_runway is None and plan.route == ["ALFAX", "UL9", "BRAVO"]
    assert plan.summary()["route"] == "ALFAX UL9 BRAVO"
    with pytest.raises(PlanError):
        from_simbrief({"general": {}})
    assert manual("lfxx", "360").cruise_ft == 36000 and manual("LFXX", "FL90", "alfax").route == ["ALFAX"]
    assert manual("LFXX", 36000).cruise_ft == 36000
    for bad in ("", "TOOLONG"):
        with pytest.raises(PlanError):
            manual(bad)


def test_intent_carries_cleared_altitude_and_heading():
    intent = parse_json('{"intent": "readback", "altitude": 24000, "heading": "240"}')
    assert (intent.altitude, intent.heading) == (24000.0, 240.0)
    assert parse_json('{"intent": "check_in", "altitude": "junk", "heading": 999}') == Intent("check_in")


# ---- vectors ----------------------------------------------------------------


def test_position_in_the_runway_frame():
    threshold, course = (51.0, 5.37), 270.0  # landing westbound, so the approach comes from the east
    east = locate(51.0, 5.80, threshold, course)
    assert 16 < east.along_nm < 17 and east.cross_nm < 0.1
    south_east = locate(50.9, 5.80, threshold, course)
    assert south_east.side == "left" and 5.5 < south_east.cross_nm < 6.5  # south is left when landing westbound
    north_west = locate(51.1, 5.0, threshold, course)
    assert north_west.side == "right" and north_west.along_nm < 0
    assert established(locate(51.0, 5.6, threshold, course)) and not established(south_east)


def test_vector_pattern_from_each_direction():
    threshold, course, platform = (51.0, 5.37), 270.0, 2500
    far = next_vector(locate(51.0, 4.0, threshold, course), course, platform, 12000)
    assert far.stage == "descend" and far.heading_true is None and platform < far.altitude_ft <= 12000
    assert next_vector(locate(51.0, 4.0, threshold, course), course, platform, 4000).altitude_ft == 4000  # never a climb

    # From the west, north of the field: downwind eastbound, then base south, then intercept.
    downwind = next_vector(locate(51.1, 5.0, threshold, course), course, platform, 8000)
    assert (downwind.stage, downwind.heading_true, downwind.altitude_ft) == ("downwind", 90.0, 3500)
    close = next_vector(locate(51.02, 5.0, threshold, course), course, platform, 8000)
    assert (close.stage, close.heading_true) == ("downwind", 70.0)  # near the centreline: drift north for room
    base = next_vector(locate(51.1, 5.75, threshold, course), course, platform, 4000)
    assert (base.stage, base.heading_true, base.altitude_ft) == ("base", 180.0, 2500)
    intercept = next_vector(locate(51.04, 5.75, threshold, course), course, platform, 3000)
    assert (intercept.stage, intercept.heading_true) == ("intercept", 240.0)
    assert next_vector(locate(50.96, 5.75, threshold, course), course, platform, 3000).heading_true == 300.0
    assert next_vector(locate(51.0, 5.75, threshold, course), course, platform, 3000).heading_true == 270.0

    assert turn_direction(90, 180) == "right" and turn_direction(90, 20) == "left" and turn_direction(350, 20) == "right"


# ---- the flight -------------------------------------------------------------


def test_clearance_uses_the_filed_plan_and_the_sid(world):
    db, engine, _ = world
    delivery = db.station(121900, 51.0, -1.0)
    here = at(51.003, -1.0, on_ground=True)
    reply = engine.handle(Intent("request_clearance", atis="C", destination="Eastfield"), delivery, ISSUE, here)
    squawk = engine.flight.squawk
    assert reply.text == (
        f"{CALLSIGN}, Westfield Delivery, cleared to Eastfield via the Alfax one Alfa departure, runway two seven, "
        f"climb altitude five thousand feet, squawk {say.digits(squawk)}"
    )
    assert reply.expect == {"squawk": squawk, "altitude": 5000}
    assert engine.summary() == {"phase": "parked", "flight_rules": "IFR", "destination": "Eastfield", "assigned_squawk": squawk,
                                "assigned_runway": "27", "sid": "ALFA1A", "cleared_altitude_ft": 5000, "cleared_level": "5,000 ft"}  # fmt: skip
    wrong = engine.handle(Intent("readback", squawk=squawk, altitude=6000), delivery, ISSUE, here)
    assert wrong.text.startswith(f"{CALLSIGN}, negative, cleared to Eastfield")
    right = engine.handle(Intent("readback", squawk=squawk, altitude=5000), delivery, ISSUE, here)
    assert right.text.startswith(f"{CALLSIGN}, readback correct, contact Westfield Ground")


def test_step_levels():
    from proatc.ifr import _next_level

    assert _next_level(5000, 1500, 24000) == 15000  # from the SID's altitude, one step up
    assert _next_level(15000, 13500, 24000) == 24000  # the rest is less than a step and a bit
    assert _next_level(24000, 18000, 36000) == 36000
    assert _next_level(24000, 23000, 41000) == 34000
    assert _next_level(36000, 36000, 13000) == 26000
    assert _next_level(26000, 27500, 13000) == 16000
    assert _next_level(16000, 17000, 13000) == 13000
    assert _next_level(8000, 8000, 6000) == 6000


def test_departure_centres_descent_and_handoff_to_approach(world):
    db, engine, centers = world
    ground, tower, departure = (db.station(khz, 51.0, -1.0) for khz in (121700, 118100, 125500))
    parked = at(51.003, -1.0, on_ground=True)
    engine.handle(Intent("request_clearance"), ground, ISSUE, parked)  # no delivery contact: ground hands over
    engine.handle(Intent("request_clearance"), db.station(121900, 51.0, -1.0), ISSUE, parked)
    engine.handle(Intent("ready_for_departure"), tower, ISSUE, parked)

    climbing = at(51.0, -1.1, feet=1500)
    assert engine.tick(tower, ISSUE, climbing).text == f"{CALLSIGN}, contact Westfield Departure one two five decimal five"
    assert engine.tick(departure, ISSUE, climbing) is None  # not checked in yet: the controller does not call first

    check_in = engine.handle(Intent("check_in"), departure, ISSUE, climbing)
    assert check_in.text == f"{CALLSIGN}, Westfield Departure, identified, climb flight level one five zero"
    assert check_in.expect == {"altitude": 15000} and engine.flight.cleared_ft == 15000
    assert engine.handle(Intent("readback", altitude=16000), departure, ISSUE, climbing).text.startswith(
        f"{CALLSIGN}, negative, climb"
    )
    assert engine.handle(Intent("readback", altitude=15000), departure, ISSUE, climbing).text == ""
    assert engine.tick(departure, ISSUE, at(51.0, -1.3, feet=9000)) is None  # well below the cleared level
    step = engine.tick(departure, ISSUE, at(51.0, -1.2, feet=13500))
    assert step.text == f"{CALLSIGN}, climb flight level two four zero" and step.expect == {"altitude": 24000}
    engine.flight.pending = None  # read back: the next instruction waits for it

    passing = at(51.0, -0.2, feet=18000, heading=90)
    handoff = engine.tick(departure, ISSUE, passing)
    assert handoff.text == f"{CALLSIGN}, contact Westland Control one two seven decimal five"
    assert engine.tick(departure, ISSUE, passing) is None  # said once

    west = centers.on_frequency(127500, passing["lat"], passing["lon"])
    assert engine.handle(Intent("check_in"), west, None, passing).text == (
        f"{CALLSIGN}, Westland Control, identified, climb flight level three six zero"
    )
    engine.flight.pending = None  # read back: the next instruction waits for it
    cruising = at(51.0, 1.0, feet=36000, heading=90)
    assert engine.tick(west, None, cruising) is None  # inside the sector, far from the descent point
    assert engine.handle(Intent("check_in"), west, None, cruising).text == f"{CALLSIGN}, roger"  # nothing new to say

    crossed = at(51.0, 2.3, feet=36000, heading=90)
    assert engine.tick(west, None, crossed).text == f"{CALLSIGN}, contact Eastland Control one three two decimal one"
    east = centers.on_frequency(132100, crossed["lat"], crossed["lon"])
    assert engine.handle(Intent("check_in"), east, None, crossed).text == f"{CALLSIGN}, Eastland Control, identified"

    # (36000 - 13000) / 300 + 55 = about 132 NM from the destination: already inside it here.
    descent = engine.tick(east, None, at(51.0, 2.2, feet=36000, heading=90))
    assert descent.text == f"{CALLSIGN}, descend flight level two six zero, Delta one Echo arrival"
    assert descent.expect == {"altitude": 26000} and engine.flight.star == "DELT1E"
    engine.flight.pending = None  # read back: the next instruction waits for it
    assert engine.tick(east, None, at(51.0, 3.0, feet=30000, heading=90)) is None
    assert engine.tick(east, None, at(51.0, 3.2, feet=27500, heading=90)).text == f"{CALLSIGN}, descend flight level one six zero"
    engine.flight.pending = None  # read back: the next instruction waits for it
    assert (
        engine.tick(east, None, at(51.0, 3.6, feet=17000, heading=90)).text == f"{CALLSIGN}, descend flight level one three zero"
    )
    engine.flight.pending = None  # read back: the next instruction waits for it
    assert engine.tick(east, None, at(51.0, 3.9, feet=13200, heading=90)) is None  # at the hand-off level

    near = at(51.0, 4.3, feet=13000, heading=90)
    assert engine.tick(east, None, near).text == f"{CALLSIGN}, contact Eastfield Approach one two four decimal three"
    assert engine.tick(east, None, near) is None


def test_vectors_to_the_ils_and_handoff_to_tower(world):
    db, engine, _ = world
    engine.flight.has_been_airborne, engine.flight.origin_ident, engine.flight.cleared_ft = True, "EGXX", 13000
    approach = db.station(124300, 51.0, 5.0)
    tower = db.station(119100, 51.0, 5.3)

    inbound = at(51.1, 4.3, feet=13000, heading=90)
    first = engine.handle(Intent("check_in"), approach, ISSUE, inbound)
    assert first.text == (
        f"{CALLSIGN}, Eastfield Approach, identified, descend flight level one one zero, expect I L S approach runway two seven"
    )
    engine.flight.pending = None  # read back: the next instruction waits for it
    # Outside vectoring range, down at the level given: the next descent on the way in, then nothing until lower.
    step = engine.tick(approach, ISSUE, at(51.1, 4.5, feet=11000, heading=90))
    assert step.text == f"{CALLSIGN}, descend flight level zero niner zero"
    engine.flight.pending = None
    assert engine.tick(approach, ISSUE, at(51.1, 4.55, feet=10800, heading=90)) is None

    abeam = at(51.1, 5.0, feet=7000, heading=90)
    downwind = engine.tick(approach, ISSUE, abeam)
    assert downwind.text == (
        f"{CALLSIGN}, fly heading zero niner zero, descend altitude three thousand five hundred feet, Q N H one zero one three, "
        "reduce speed two two zero knots"
    )
    assert downwind.expect == {"heading": 90, "altitude": 3500}
    assert engine.tick(approach, ISSUE, abeam) is None  # no repeat while the stage is unchanged
    assert engine.summary()["heading"] == 90 and engine.summary()["speed_kt"] == 220

    wrong = engine.handle(Intent("readback", heading=100, altitude=3500), approach, ISSUE, abeam)
    assert wrong.text.startswith(f"{CALLSIGN}, negative, fly heading zero niner zero")
    assert engine.handle(Intent("readback", heading=90, altitude=3500), approach, ISSUE, abeam).text == ""

    base = engine.tick(approach, ISSUE, at(51.1, 5.75, feet=3500, heading=90))
    assert base.text == (
        f"{CALLSIGN}, turn right heading one eight zero, descend altitude two thousand five hundred feet, reduce speed one eight zero knots"
    )
    engine.flight.pending = None  # read back: the next instruction waits for it
    final = engine.tick(approach, ISSUE, at(51.05, 5.75, feet=2500, heading=180))
    assert final.text == (
        f"{CALLSIGN}, turn right heading two four zero, one five miles from touchdown, cleared I L S approach runway two seven, "
        "speed one six zero knots until four miles, report established"
    )
    assert final.expect == {"heading": 240, "runway": "27"}
    assert engine.summary()["approach"] == "I L S runway 27"
    # "Report established" is asked: its answer comes next to the readback, which stays the call to make first.
    offered = engine.suggestions(approach, ISSUE, at(51.03, 5.72, feet=2500, heading=240))
    assert [item["label"] for item in offered][:3] == ["Read back", "Established", "Request vectors"]
    assert [item["label"] for item in offered if item.get("primary")] == ["Read back"]
    engine.flight.pending = None  # read back: the next instruction waits for it

    assert engine.tick(approach, ISSUE, at(51.03, 5.72, feet=2500, heading=240)) is None  # not on the localiser yet
    on_ils = at(51.0, 5.62, feet=2500, heading=270)
    assert engine.tick(approach, ISSUE, on_ils).text == f"{CALLSIGN}, contact Eastfield Tower one one niner decimal one"
    assert engine.tick(approach, ISSUE, on_ils) is None

    # Nine miles out: continue. Tower clears to land, unasked, inside four and a half miles.
    far = engine.handle(Intent("established"), tower, ISSUE, at(51.0, 5.6, feet=2500, heading=270))
    assert (
        far.text == f"{CALLSIGN}, Eastfield Tower, continue approach runway two seven, wind two seven zero degrees one zero knots"
    )
    assert engine.tick(tower, ISSUE, at(51.0, 5.55, feet=2000, heading=270)) is None
    landing = engine.tick(tower, ISSUE, at(51.0, 5.47, feet=1200, heading=270))
    assert landing.text == f"{CALLSIGN}, runway two seven, cleared to land, wind two seven zero degrees one zero knots"
    assert engine.tick(tower, ISSUE, at(51.0, 5.45, feet=1000, heading=270)) is None
    assert engine.flight.phase.value == "cleared to land"


def test_speed_is_not_given_to_an_aircraft_already_slow(world):
    db, engine, _ = world
    engine.flight.has_been_airborne, engine.flight.origin_ident = True, "EGXX"
    approach = db.station(124300, 51.0, 5.0)
    slow = {**at(51.1, 5.0, feet=7000, heading=90), "ias_kt": 200.0}
    assert "reduce speed" not in engine.handle(Intent("check_in"), approach, ISSUE, slow).text


def test_reporting_established_gets_the_tower_handoff(world):
    db, engine, _ = world
    engine.flight.has_been_airborne, engine.flight.origin_ident = True, "EGXX"
    approach = db.station(124300, 51.0, 5.0)
    east_of_field = at(51.03, 5.75, feet=3000, heading=270)
    first = engine.handle(Intent("check_in"), approach, ISSUE, east_of_field)
    assert first.text == (
        f"{CALLSIGN}, Eastfield Approach, identified, turn left heading two four zero, descend altitude two thousand five hundred feet, "
        "Q N H one zero one three, one four miles from touchdown, cleared I L S approach runway two seven, "
        "speed one six zero knots until four miles, report established"
    )
    assert engine.handle(Intent("established"), approach, ISSUE, east_of_field).text == (
        f"{CALLSIGN}, contact Eastfield Tower one one niner decimal one"
    )


def test_going_around_returns_to_approach_for_new_vectors(world):
    db, engine, _ = world
    engine.flight.has_been_airborne, engine.flight.origin_ident, engine.flight.rules = True, "EGXX", "IFR"
    approach, tower = db.station(124300, 51.0, 5.0), db.station(119100, 51.0, 5.3)
    engine.handle(Intent("check_in"), approach, ISSUE, at(51.03, 5.75, feet=3000, heading=270))
    engine.handle(Intent("established"), approach, ISSUE, at(51.0, 5.6, feet=2500, heading=270))
    short_final = at(51.0, 5.40, feet=600, heading=270)
    engine.handle(Intent("established"), tower, ISSUE, short_final)
    missed = engine.handle(Intent("going_around"), tower, ISSUE, short_final)
    assert missed.text == (
        f"{CALLSIGN}, roger, fly runway heading, climb altitude two thousand five hundred feet, "
        "contact Eastfield Approach one two four decimal three"
    )
    assert missed.expect == {"altitude": 2500, "frequency": "124.300"} and engine.flight.cleared_ft == 2500
    west_of_field = at(51.0, 5.2, feet=2500, heading=270)
    again = engine.handle(Intent("going_around"), approach, ISSUE, west_of_field)
    assert again.text.startswith(f"{CALLSIGN}, turn right heading zero seven zero")  # downwind again, drifting off the centreline
    assert "expect I L S approach runway two seven" in again.text


def test_the_controller_notices_a_level_bust_a_wrong_heading_and_a_wrong_squawk(world):
    db, engine, centers = world
    clock = [1000.0]
    engine.now = lambda: clock[0]
    flight = engine.flight
    flight.has_been_airborne, flight.origin_ident, flight.squawk = True, "EGXX", "4521"

    # Wrong code on first contact with a radar controller.
    departure = db.station(125500, 51.0, -1.0)
    first = engine.handle(Intent("check_in"), departure, ISSUE, {**at(51.0, -1.1, feet=3000), "squawk": 2000})
    assert first.text.endswith("climb flight level one three zero, squawk four five two one")
    assert first.expect == {"altitude": 13000, "squawk": "4521"}
    engine.handle(Intent("readback", altitude=13000, squawk="4521"), departure, ISSUE, at(51.0, -1.1, feet=3000))

    # Climbing through the cleared level: called once, and only while still climbing.
    west = centers.on_frequency(127500, 51.0, 0.0)
    engine.handle(Intent("check_in"), west, None, at(51.0, 0.0, feet=30000, heading=90))
    engine.handle(Intent("readback", altitude=36000), west, None, at(51.0, 0.0, feet=30000, heading=90))
    through = {**at(51.0, 0.1, feet=36500, heading=90), "vs_fpm": 1500.0}
    assert engine.tick(west, None, {**at(51.0, 0.1, feet=36300, heading=90), "vs_fpm": 1500.0}) is None  # within tolerance
    bust = engine.tick(west, None, through)
    assert bust.text == f"{CALLSIGN}, check your level, cleared flight level three six zero"
    assert bust.expect == {}
    assert engine.tick(west, None, through) is None
    flight.bust_called_for = None
    assert engine.tick(west, None, {**through, "vs_fpm": 0.0}) is None  # above the level but not going through it

    # A heading that is not being flown is repeated after a while, once.
    flight.origin_ident, flight.cleared_ft = "ELSE", 7000
    approach = db.station(124300, 51.0, 5.0)
    engine.handle(Intent("check_in"), approach, ISSUE, at(51.1, 5.0, feet=7000, heading=270))
    engine.handle(Intent("readback", heading=90, altitude=3500), approach, ISSUE, at(51.1, 5.0, feet=7000, heading=270))
    still_west = {**at(51.1, 5.0, feet=6000, heading=270), "vs_fpm": -800.0}
    assert engine.tick(approach, ISSUE, still_west) is None  # give the turn time
    clock[0] += 60
    assert engine.tick(approach, ISSUE, still_west).text == f"{CALLSIGN}, fly heading zero niner zero"
    assert engine.tick(approach, ISSUE, still_west) is None


def test_an_unanswered_instruction_is_said_again_once(world):
    db, engine, _ = world
    clock = [0.0]
    engine.now = lambda: clock[0]
    ground = db.station(121700, 51.0, -1.0)
    parked = at(51.003, -1.0, on_ground=True)
    engine.set_plan(None)
    taxi = engine.handle(Intent("request_taxi"), ground, ISSUE, parked)
    assert engine.tick(ground, ISSUE, parked) is None
    clock[0] = 31.0
    again = engine.tick(ground, ISSUE, parked)
    assert again.text == f"{CALLSIGN}, Westfield Ground, I say again, {taxi.text.split('Westfield Ground, ', 1)[1]}"
    clock[0] = 90.0
    assert engine.tick(ground, ISSUE, parked) is None  # only once
    assert engine.handle(Intent("readback", runway="27"), ground, ISSUE, parked).text == ""  # still accepted
    clock[0] = 200.0
    assert engine.tick(ground, ISSUE, parked) is None  # nothing pending any more


def test_direct_to_a_filed_waypoint(world):
    db, engine, centers = world
    engine.flight.has_been_airborne = True
    cruising = at(51.0, 0.0, feet=36000, heading=90)
    west = centers.on_frequency(127500, 51.0, 0.0)
    engine.handle(Intent("check_in"), west, None, cruising)
    labels = [item["label"] for item in engine.suggestions(west, None, cruising)]
    assert "Request direct DELTA" in labels
    assert engine.handle(Intent("request_direct", fix="delta"), west, None, cruising).text == f"{CALLSIGN}, route direct Delta"
    assert engine.summary()["direct_to"] == "DELTA"
    assert "Request direct DELTA" not in [item["label"] for item in engine.suggestions(west, None, cruising)]
    assert engine.handle(Intent("request_direct", fix="NOWHR"), west, None, cruising).text == f"{CALLSIGN}, unable direct"


def test_airline_callsign_from_the_filed_plan(world):
    db, engine, _ = world
    assert say.airline_callsign("BAW123") == "Speedbird one two three"
    assert say.airline_callsign("ezy45ab") == "Easy four five Alfa Bravo"
    assert say.airline_callsign("CGTLT") is None and say.airline_callsign("BAW") is None and say.airline_callsign("XXX12") is None
    plan = from_simbrief(
        {"general": {}, "origin": {"icao_code": "EGXX"}, "destination": {"icao_code": "LFXX"}, "atc": {"callsign": "BAW123"}}
    )
    assert plan.callsign == "BAW123"
    engine.set_plan(plan)
    tower = db.station(118100, 51.0, -1.0)
    assert engine.handle(Intent("radio_check"), tower, ISSUE, at(51.003, -1.0, on_ground=True)).text == (
        "Speedbird one two three, Westfield Tower, reading you five"
    )


def test_without_a_plan_the_centre_still_answers_and_grants_requests(world):
    db, engine, centers = world
    engine.set_plan(None)
    engine.flight.has_been_airborne = True
    cruising = at(51.0, 0.0, feet=20000, heading=90)
    west = centers.on_frequency(127500, 51.0, 0.0)
    assert engine.handle(Intent("check_in"), west, None, cruising).text == f"{CALLSIGN}, Westland Control, identified"
    assert (
        engine.handle(Intent("request_climb", altitude=24000), west, None, cruising).text
        == f"{CALLSIGN}, climb flight level two four zero"
    )
    assert (
        engine.handle(Intent("request_descent", altitude=10000), west, None, cruising).text
        == f"{CALLSIGN}, descend flight level one zero zero"
    )
    assert engine.handle(Intent("other"), west, None, cruising) is None
    # Centres do not work the ground: a call from there is acknowledged and nothing more.
    assert engine.handle(Intent("check_in"), west, None, at(51.0, 0.0, on_ground=True)).text == f"{CALLSIGN}, roger"


def test_simbrief_user_defaults_to_the_env_file(tmp_path, monkeypatch):
    from proatc import config, server

    monkeypatch.delenv("SIMBRIEF_USER", raising=False)
    monkeypatch.setattr(config, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(server, "PROJECT_DIR", tmp_path)
    assert server.load_settings() == {}
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=k\nSIMBRIEF_USER=123456\n", encoding="utf-8")
    assert server.load_settings() == {"simbrief_user": "123456"} and config.openrouter_key() == "k"
    server.save_settings({"simbrief_user": "someone"})
    assert server.load_settings()["simbrief_user"] == "someone"  # an account used in the app wins
