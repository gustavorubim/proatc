"""Airways and the full route, the en-route track watch, directs to any waypoint ahead, and oceanic airspace."""

from proatc.airways import Airways, coordinate, expand, next_ahead, parse_airways, spoken_coordinate
from proatc.centers import CenterIndex, parse_atc
from proatc.engine import Engine
from proatc.flightplan import FlightPlan
from proatc.intents import Intent
from proatc.navdata import AirportDB, build_index
from proatc.navfix import NavFix
from proatc.procedures import parse_cifp
from proatc.voice_loop import known_names
from tests.test_ifr import APT, ATC, CALLSIGN, at

AIRWAYS = """I
1100 Version

ALFAX EG 11 MIDDA EG 11 N 2 250 460 UL9
MIDDA EG 11 MIDDB EG 11 N 2 250 460 UL9-UN57
MIDDB EG 11 BRAVO EG 11 F 2 250 460 UL9
BRAVO EG 11 FARAW EG 11 N 2 250 460 UL9
BRAVO EG 11 CHARL LF 11 N 2 250 460 UM605
CHARL LF 11 DELTA LF 11 N 2 250 460 UM605
garbage line that is not an airway
XXXXX EG zz YYYYY EG 11 N 2 250 460 UL9
""".splitlines()
FIXES = NavFix(
    fixes={
        "ALFAX": [(51.0, -2.0)], "MIDDA": [(51.3, -1.0)], "MIDDB": [(51.3, 0.5)], "BRAVO": [(51.0, 2.0), (10.0, 100.0)],
        "FARAW": [(53.0, 2.0)], "CHARL": [(51.0, 4.0)], "DELTA": [(51.0, 7.6)],
    }
)  # fmt: skip
OCEAN = """
CONTROLLER
NAME FARAWAY OCEANIC
FACILITY_ID EGGX
ROLE ctr
FREQ 12790
AIRSPACE_POLYGON_BEGIN 0 60000
POINT 53.0 -30.0
POINT 53.0 -3.0
POINT 49.0 -3.0
POINT 49.0 -30.0
AIRSPACE_POLYGON_END
CONTROLLER_END
""".splitlines()
ROUTE = "ALFAX UL9 BRAVO UM605 DELTA".split()


def test_airways_join_the_waypoints_of_a_route():
    airways = parse_airways(AIRWAYS)
    assert set(airways.segments) == {"UL9", "UN57", "UM605"} and "UL9" in airways and "DCT" not in airways
    assert airways.between("UL9", "ALFAX", "BRAVO") == ["MIDDA", "MIDDB", "BRAVO"]
    assert airways.between("UL9", "BRAVO", "ALFAX") == ["MIDDB", "MIDDA", "ALFAX"]  # flown the other way
    assert airways.between("UL9", "ALFAX", "DELTA") is None and airways.between("NOPE", "ALFAX", "BRAVO") is None

    points = expand(ROUTE, airways, FIXES, (51.0, -1.0))
    assert [name for name, _, _ in points] == ["ALFAX", "MIDDA", "MIDDB", "BRAVO", "CHARL", "DELTA"]
    assert points[3][1:] == (51.0, 2.0)  # the BRAVO near the route, not the one on the other side of the world
    # Speed and level groups, DCT and procedure names are dropped; without airway data only the filed waypoints remain.
    filed = "WEST2A ALFAX/N0450F360 DCT BRAVO UM605 DELTA EAST1B".split()
    assert [name for name, _, _ in expand(filed, None, FIXES, (51.0, -1.0))] == ["ALFAX", "BRAVO", "DELTA"]
    assert expand(ROUTE, airways, None, (51.0, -1.0)) == [] and expand([], Airways(), FIXES, (0.0, 0.0)) == []
    assert next_ahead(points, 51.2, -1.5) == "MIDDA" and next_ahead(points, 51.3, -0.2) == "MIDDB"
    assert next_ahead(points, 51.0, 7.7) == "DELTA" and next_ahead([], 0.0, 0.0) is None


def test_oceanic_waypoints_are_latitudes_and_longitudes():
    assert coordinate("53N020W") == (53.0, -20.0) and coordinate("5230N02000W") == (52.5, -20.0)
    assert coordinate("10S150E") == (-10.0, 150.0) and coordinate("ALFAX") is None and coordinate("99N200W") is None
    assert spoken_coordinate("53N020W") == "five three north zero two zero west"
    assert spoken_coordinate("5230N02000W") == "five two three zero north zero two zero west"
    crossing = expand("ALFAX 52N010W 53N020W 53N030W".split(), Airways(), FIXES, (51.0, -1.0))
    assert [name for name, _, _ in crossing] == ["ALFAX", "52N010W", "53N020W", "53N030W"]
    assert crossing[2][1:] == (53.0, -20.0)


def make(tmp_path, route=ROUTE, atc=ATC):
    apt = tmp_path / "apt.dat"
    if not (tmp_path / "a.sqlite").exists():
        apt.write_text("\n".join(APT), encoding="utf-8")
        build_index(apt, tmp_path / "a.sqlite")
    db = AirportDB(tmp_path / "a.sqlite")
    clock = [0.0]
    engine = Engine(
        db, seed=1, procedures=lambda ident: parse_cifp([]), centers=CenterIndex(parse_atc(atc)), navfix=FIXES,
        airways=parse_airways(AIRWAYS), clock=lambda: clock[0],
    )  # fmt: skip
    engine.set_plan(FlightPlan("EGXX", "LFXX", route, 36000))
    engine.flight.has_been_airborne, engine.flight.origin_ident, engine.flight.cleared_ft = True, "EGXX", 36000
    return engine, clock


def test_a_direct_is_given_to_any_waypoint_on_the_route(tmp_path):
    engine, _ = make(tmp_path)
    centre = engine.centers.on_frequency(127500, 51.0, 0.0)
    here = at(51.3, -1.2, feet=36000, heading=90)
    assert [name for name, _, _ in engine.route()] == ["ALFAX", "MIDDA", "MIDDB", "BRAVO", "CHARL", "DELTA"]
    assert known_names(engine)[:3] == ["EGXX", "LFXX", "ALFAX"] and "MIDDB" in known_names(engine)
    # MIDDB is on the airway, not in the filed text: it is a waypoint of the route all the same.
    assert engine.handle(Intent("request_direct", fix="MIDDB"), centre, None, here).text == (f"{CALLSIGN}, route direct Middb")
    assert engine.flight.direct_to == "MIDDB"
    assert engine.handle(Intent("request_direct", fix="FARAW"), centre, None, here) is None  # on UL9, but not on this route
    engine.set_plan(FlightPlan("EGXX", "LFXX", ["ALFAX", "BRAVO"], 36000))
    assert [name for name, _, _ in engine.route()] == ["ALFAX", "BRAVO"]  # a new plan, a new route


def test_a_flight_off_its_route_in_the_cruise_is_asked_once(tmp_path):
    engine, clock = make(tmp_path)
    centre = engine.centers.on_frequency(127500, 51.0, 0.0)
    engine.flight.contacted.add(centre.key)
    on_track = at(51.3, -0.2, feet=36000, heading=90)
    assert engine.tick(centre, None, on_track) is None
    adrift = at(51.55, -0.2, feet=36000, heading=90)  # fifteen miles north of the airway
    assert engine.tick(centre, None, adrift) is None  # noticed, not yet said
    clock[0] = 60.0
    assert engine.tick(centre, None, adrift) is None  # a turn can take a minute
    clock[0] = 130.0
    asked = engine.tick(centre, None, adrift)
    assert asked.text == f"{CALLSIGN}, you appear to be one five miles off your filed route, confirm your routing"
    assert asked.event == "off_route"
    clock[0] = 400.0
    assert engine.tick(centre, None, adrift) is None  # once
    # Back on track for a while resets it; a direct routing means the filed track no longer applies.
    engine, clock = make(tmp_path)
    engine.flight.contacted.add(centre.key)
    engine.flight.direct_to = "DELTA"
    engine.tick(centre, None, adrift)
    clock[0] = 300.0
    assert engine.tick(centre, None, adrift) is None


def test_oceanic_airspace_is_position_reports_not_radar(tmp_path):
    engine, _ = make(tmp_path, route="ALFAX 51N010W 51N020W".split(), atc=ATC + OCEAN)
    engine.flight.route_points = None
    centers = engine.centers
    ocean = centers.on_frequency(127900, 51.0, -8.0)
    assert ocean.center.oceanic and ocean.callsign == "Faraway Radio"
    assert not centers.on_frequency(127500, 51.0, 0.0).center.oceanic
    here = at(51.0, -8.0, feet=36000, heading=270)
    first = engine.handle(Intent("check_in"), ocean, None, here)
    assert first.text == (
        f"{CALLSIGN}, Faraway Radio, roger, maintain flight level three six zero, report five one north zero one zero west"
    )
    assert "identified" not in first.text and first.expect == {}
    report = engine.handle(Intent("position_report"), ocean, None, at(51.0, -12.0, feet=36000, heading=270))
    assert report.text == f"{CALLSIGN}, roger, maintain flight level three six zero, report five one north zero two zero west"
    climb = engine.handle(Intent("request_climb", altitude=38000), ocean, None, here)
    assert climb.text == f"{CALLSIGN}, climb flight level three eight zero, report reaching" and climb.expect == {
        "altitude": 38000
    }
    engine.flight.pending = None
    assert engine.handle(Intent("request_climb", altitude=60000), ocean, None, here).text == (
        f"{CALLSIGN}, unable, maintain flight level three eight zero"
    )
    # Nothing is watched without radar: a level bust draws no call. The next sector is still given at the boundary.
    busting = {**at(51.0, -9.0, feet=39500, heading=270), "vs_fpm": 1500.0}
    assert engine.tick(ocean, None, busting) is None
    leaving = engine.tick(ocean, None, at(51.0, -2.5, feet=38000, heading=90))
    assert leaving.text == f"{CALLSIGN}, contact Westland Control one two seven decimal five"
    engine.flight.pending = None
    labels = [item["label"] for item in engine.suggestions(ocean, None, here)]
    assert labels[:2] == ["Position report", "Request climb"]
    assert engine.suggestions(ocean, None, here)[0]["text"].endswith(
        "position, flight level three eight zero, estimating five one north zero one zero west"
    )


def test_a_pressure_below_one_thousand_is_said_with_its_unit():
    from proatc import phraseology as say
    from proatc.atis import Issue
    from proatc.weather import Weather

    assert say.qnh(1013.2) == "Q N H one zero one three" and say.qnh(1000) == "Q N H one zero zero zero"
    assert say.qnh(998.7) == "Q N H niner niner eight hectopascals"
    low = Issue("A", Weather(qnh_hpa=985.0), 0.0)
    assert Engine._pressure(low, False) == ("Q N H niner eight five hectopascals", 985.0)
    assert Engine._pressure(low, True)[0] == "altimeter two niner zero niner"  # inches are never confused: no unit
