"""The airport map: what is drawn, where the aircraft is on it, and the taxi route as it is flown along."""

import json
import math

from proatc import chart
from proatc import config as config_module
from proatc.config import Config
from proatc.flightplan import FlightPlan
from proatc.intents import Intent
from proatc.server import AppServer
from tests.test_ifr import ISSUE
from tests.test_realism import APRON, ground_world, taxiing

NORTH_M = 110_574  # metres in a degree of latitude, as the map projects it


def north_of(lat: float) -> float:
    return round((lat - 51.002) * NORTH_M, 1)  # EGXX's reference point is midway between its runways


def test_the_map_holds_what_the_airport_file_has(tmp_path):
    _, engine, _ = ground_world(tmp_path)
    drawn = engine.chart("EGXX")
    assert json.loads(json.dumps(drawn)) == drawn  # it goes to the app as it is
    assert [(r["name"], r["width"]) for r in drawn["runways"]] == [("09R/27L", 45.0), ("09L/27R", 45.0)]
    assert [end[0] for end in drawn["runways"][0]["ends"]] == ["09R", "27L"]
    assert [t["name"] for t in drawn["taxiways"]] == ["B", "B1", "C"]
    assert sum(len(t["lines"]) for t in drawn["taxiways"]) == 6 * 4  # six segments, none twice, no runway among them
    assert sorted(label["name"] for label in drawn["labels"]) == ["B", "B1", "C"]
    assert [s["name"] for s in drawn["stands"]] == ["5", "7", "GA1"]
    west, south, east, north = drawn["bounds"]
    for runway in drawn["runways"]:
        for _, x, y in runway["ends"]:
            assert west <= x <= east and south <= y <= north
    assert south <= drawn["stands"][0]["at"][1] < drawn["runways"][0]["ends"][0][2]  # the apron is south of the runways
    assert engine.chart("EGXX") is drawn and engine.chart("ZZZZ") is None


def test_pavement_is_read_with_its_holes_and_a_bad_row_is_skipped():
    rows = """110 1 0.25 0.0 apron
111 51.0000 -1.0000
111 51.0000 -0.9990
112 51.0010 -0.9990 51.0011 -0.9989
113 51.0010 -1.0000
111 51.0002 -0.9998
111 51.0002 -0.9996
111 nonsense -0.9996
113 51.0004 -0.9996
120 a line, not pavement
111 51.1000 -1.1000
115 51.1000 -1.1000
110 1 0.25 0.0 too small
111 51.0000 -1.0000
113 51.0000 -0.9990
130 boundary
111 51.2 -1.2
111 51.3 -1.2
113 51.3 -1.3""".splitlines()
    polygons = chart._pavement(rows, (51.0, -1.0))
    assert len(polygons) == 1 and [len(ring) // 2 for ring in polygons[0]] == [4, 3]
    assert polygons[0][0][:2] == [0.0, 0.0] and polygons[0][0][3] == 0.0  # the first edge runs due east
    assert chart._pavement(["110 x", "111 1e400 0", "113 91 0"], (0.0, 0.0)) == []


def test_the_taxi_route_is_shown_as_far_as_the_runway_to_hold_short_of(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    parked = taxiing(*APRON)
    before = engine.taxi_map(parked)
    assert before["ident"] == "EGXX" and "route" not in before and before["position"] == [0.0, north_of(50.996)]

    engine.handle(Intent("request_taxi"), ground, ISSUE, parked)
    view = engine.taxi_map(parked)
    route = view["route"]
    assert route["goal"]["label"] == "Holding point 27R" and route["hold"]["runway"] == "09R"
    # Cleared from the aircraft up to 75 m before the runway's centreline; the rest is drawn apart.
    assert route["cleared"][0] == view["position"]
    assert route["cleared"][-1] == route["hold"]["at"] == route["beyond"][0]
    assert math.isclose(route["hold"]["at"][1], north_of(51.0) - chart.HOLD_BACK_M, abs_tol=0.2)
    assert route["beyond"][-1] == route["goal"]["at"] and route["goal"]["at"][1] == north_of(51.004)
    assert json.loads(json.dumps(view)) == view

    # Further along, less of it is left; once cleared to cross, all of it is cleared.
    nearer = engine.taxi_map(taxiing(50.998))["route"]
    assert nearer["cleared"][0][1] > route["cleared"][0][1] and nearer["cleared"][1:] == route["cleared"][1:]
    assert nearer["hold"] == route["hold"]
    engine.handle(Intent("request_crossing"), ground, ISSUE, taxiing(50.999))
    crossing = engine.taxi_map(taxiing(50.999))["route"]
    assert "hold" not in crossing and crossing["beyond"] == [] and crossing["cleared"][-1] == route["goal"]["at"]
    past = engine.taxi_map(taxiing(51.002, -0.985))["route"]
    assert len(past["cleared"]) == 4  # the aircraft, the corner, the holding point's taxiway, the runway


def test_the_map_is_for_the_ground_and_forgets_the_route_after_takeoff(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    engine.handle(Intent("request_taxi"), ground, ISSUE, taxiing(*APRON))
    assert engine.taxi_map({**taxiing(51.004), "on_ground": 0}) is None and engine.taxi_map({}) is None
    engine._observe({**taxiing(51.004), "on_ground": 0, "agl_m": 300.0})
    assert engine.flight.taxi_route is None and "route" not in engine.taxi_map(taxiing(51.004))
    far = taxiing(40.0, 10.0)
    assert engine.taxi_map(far) is None  # on the ground, but at no airport the data knows


def test_off_the_route_the_way_is_shown_from_where_the_aircraft_is(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    engine.handle(Intent("request_taxi"), ground, ISSUE, taxiing(*APRON))
    engine.handle(Intent("request_crossing"), ground, ISSUE, taxiing(50.999))
    given = engine.flight.taxi_route
    wrong_turn = taxiing(51.0025, -0.9900)  # on Bravo, well past where the first route began
    engine.flight.taxi_route = type(given)([], [], 0.0, points=[APRON, (50.9965, -1.0)])  # a route that is nowhere near
    view = engine.taxi_map(wrong_turn)
    assert engine.flight.taxi_route.points[0] != APRON and view["route"]["cleared"][0] == view["position"]
    assert view["route"]["goal"]["label"] == "Holding point 27R" and len(view["route"]["cleared"]) <= 4


def test_after_landing_the_route_ends_at_the_stand(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    engine.set_plan(FlightPlan("LFXX", "EGXX", callsign="BAW123"))
    engine.flight.has_been_airborne = True
    rolled_out = {**taxiing(51.004, -0.98), "aircraft_type": "A20N"}
    engine._observe({**rolled_out, "on_ground": 0, "agl_m": 300.0})
    engine._observe(rolled_out)
    engine.handle(Intent("runway_vacated"), ground, ISSUE, rolled_out)
    view = engine.taxi_map(rolled_out)
    stand = engine.flight.stand
    assert view["route"]["goal"]["label"] == f"Stand {stand.name}"
    assert view["route"]["goal"]["at"] == list(chart.project((51.002, -1.0), stand.lat, stand.lon))
    assert view["route"]["hold"]["runway"] == "09R"  # the other runway lies between it and the apron


def test_route_geometry_at_its_edges():
    path = [(0.0, 0.0), (0.0, 100.0), (100.0, 100.0)]
    assert chart.next_point(path, (5.0, 50.0)) == (1, 5.0) and chart.next_point(path, (50.0, 110.0)) == (2, 10.0)
    assert chart.next_point([(0.0, 0.0)], (3.0, 4.0)) == (0, 5.0) and chart.next_point([], (3.0, 4.0)) == (0, 0.0)
    assert chart.split_route(path, (0.0, 0.0), None) == ([(0.0, 0.0), (0.0, 100.0), (100.0, 100.0)], [], None)
    cleared, beyond, stop = chart.split_route(path, (0.0, 10.0), 2)
    assert stop == (25.0, 100.0) and cleared[-1] == stop and beyond == [stop, (100.0, 100.0)]
    assert chart.split_route(path, (60.0, 100.0), 1)[2] is None  # that runway is behind
    close = chart.split_route(path, (90.0, 100.0), 2)  # nearer than the mark's distance: stop here
    assert close[0] == [(90.0, 100.0)] and close[2] == (90.0, 100.0)
    assert chart.split_route([(0.0, 0.0), (0.0, 0.0)], (0.0, 0.0), 1)[2] == (0.0, 0.0)  # points on top of each other


def test_the_app_asks_for_a_map_by_name(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr("proatc.server.PROJECT_DIR", tmp_path)
    _, engine, _ = ground_world(tmp_path)
    app = AppServer(Config(), 0, "token")
    seen: list[dict] = []
    app.emit = seen.append
    app._command({"cmd": "chart", "ident": "EGXX"})
    assert seen == []  # not before the engine is ready
    app.session.ready, app.session.engine = True, engine
    app._command({"cmd": "chart", "ident": " egxx "})
    assert seen[0]["type"] == "chart" and seen[0]["ident"] == "EGXX" and len(seen[0]["runways"]) == 2
    for nothing in ("ZZZZ", "", None, ["EGXX"], "x" * 500):
        app._command({"cmd": "chart", "ident": nothing})
    assert len(seen) == 1
