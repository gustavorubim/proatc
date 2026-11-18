"""A departure from a runway the taxiways reach only part way along: entered there, backtracked to the threshold,
turned round, and cleared for takeoff. Ground says to expect it; the tower clears it when nobody is on final."""

from __future__ import annotations

import pytest

from proatc.engine import Engine, Phase
from proatc.intents import Intent
from proatc.navdata import AirportDB, build_index
from proatc.procedures import parse_cifp
from proatc.taxi import parse_taxi, route_to_runway
from tests.test_gate import ISSUE
from tests.test_realism import APRON, CROSSING, taxiing

GROUND, TOWER = 121700, 118100
THRESHOLD_27L = (51.0, -0.98)


def airport(tmp_path, ident="EGXX"):
    """The crossing airport, with no way round to 27R and taxiway C meeting 27L half way along: a jet departing 27L
    has not enough runway from there and backtracks."""
    rows = [line for line in CROSSING if not line.startswith("1202 3 4")]
    rows = [line.replace("1201 51.0000 -1.0000 both 2 on_27L", "1201 51.0000 -0.9990 both 2 on_27L") for line in rows]
    rows = [line.replace("EGXX", ident) for line in rows]
    (tmp_path / "b.dat").write_text("\n".join(rows), encoding="utf-8")
    build_index(tmp_path / "b.dat", tmp_path / "b.sqlite")
    db = AirportDB(tmp_path / "b.sqlite")
    engine = Engine(db, seed=1, procedures=lambda ident: parse_cifp([]))
    engine.flight.runway = "27L"
    return db, engine


def jet(lat, lon, heading=0.0, speed=0.0, **more):
    return {**taxiing(lat, lon, speed), "heading_true": heading, "aircraft_type": "A320", **more}


def test_with_no_other_way_the_route_ends_where_the_runway_is_entered_and_ground_says_to_expect_a_backtrack(tmp_path):
    db, engine = airport(tmp_path)
    taxi = engine.handle(Intent("request_taxi"), db.station(GROUND, *APRON), ISSUE, jet(*APRON))
    assert "taxi to holding point runway two seven left via Charlie, expect backtrack, Q N H" in taxi.text, taxi.text
    assert engine.flight.backtrack and engine.flight.taxi_route.points[-1] == (51.0, -0.999)
    # A light aircraft has runway enough from there: an intersection departure, no backtrack.
    (tmp_path / "light").mkdir()
    db, engine = airport(tmp_path / "light")
    taxi = engine.handle(Intent("request_taxi"), db.station(GROUND, *APRON), ISSUE, {**jet(*APRON), "aircraft_type": "C172"})
    assert "intersection departure" in taxi.text and not engine.flight.backtrack


def test_the_tower_clears_the_backtrack_then_the_takeoff_once_lined_up_at_the_threshold(tmp_path):
    db, engine = airport(tmp_path)
    engine.handle(Intent("request_taxi"), db.station(GROUND, *APRON), ISSUE, jet(*APRON))
    tower = db.station(TOWER, *APRON)
    at_entry = jet(50.9993, -0.999)
    reply = engine.handle(Intent("ready_for_departure"), tower, ISSUE, at_entry)
    assert reply.text.endswith("backtrack runway two seven left, line up and wait") and reply.expect == {"runway": "27L"}
    assert engine.flight.backtracking and engine.flight.phase == Phase.TAXI_OUT  # not cleared for takeoff yet
    labels = [call["label"] for call in engine.suggestions(tower, ISSUE, at_entry)]
    assert "Lined up" in labels and "Ready for departure" not in labels

    on_the_way = jet(51.0, -0.99, heading=90.0, speed=8.0)
    assert engine.tick(tower, ISSUE, on_the_way) is None  # backtracking: nothing to say
    early = engine.handle(Intent("ready_for_departure"), tower, ISSUE, on_the_way)
    assert early.text.endswith("roger, report lined up") and engine.flight.phase == Phase.TAXI_OUT

    turned = jet(*THRESHOLD_27L, heading=270.0)
    cleared = engine.tick(tower, ISSUE, turned)
    assert "runway two seven left, cleared for takeoff" in cleared.text and engine.flight.phase == Phase.TAKEOFF_CLEARED
    assert not engine.flight.backtracking and not engine.flight.backtrack


def test_at_the_threshold_but_pointing_the_wrong_way_is_not_lined_up(tmp_path):
    db, engine = airport(tmp_path)
    engine.handle(Intent("request_taxi"), db.station(GROUND, *APRON), ISSUE, jet(*APRON))
    tower = db.station(TOWER, *APRON)
    engine.handle(Intent("ready_for_departure"), tower, ISSUE, jet(50.9993, -0.999))
    assert engine.tick(tower, ISSUE, jet(*THRESHOLD_27L, heading=90.0)) is None  # still to turn round
    assert engine.tick(tower, ISSUE, jet(*THRESHOLD_27L, heading=270.0, speed=10.0)) is None  # still rolling


def test_with_traffic_on_final_the_backtrack_waits_and_follows_by_itself(tmp_path):
    db, engine = airport(tmp_path)
    engine.traffic_enabled = True
    engine.handle(Intent("request_taxi"), db.station(GROUND, *APRON), ISSUE, jet(*APRON))
    tower = db.station(TOWER, *APRON)
    # Seven miles out on final for 27L: fine for a takeoff from the threshold, too close for a backtrack first.
    arriving = {"tcas_lat": [0.0, 51.0], "tcas_lon": [0.0, -0.821], "tcas_ele_m": [0.0, 580.0],
                "tcas_heading": [0.0, 270.0], "tcas_on_ground": [0, 0]}  # fmt: skip
    held = engine.handle(Intent("ready_for_departure"), tower, ISSUE, jet(50.9993, -0.999, **arriving))
    assert "hold short runway two seven left, traffic on a seven mile final" in held.text and not engine.flight.backtracking
    assert engine.tick(tower, ISSUE, jet(50.9993, -0.999, **arriving)) is None
    gone = engine.tick(tower, ISSUE, jet(50.9993, -0.999))
    assert gone.text.endswith("backtrack runway two seven left, line up and wait") and engine.flight.backtracking


@pytest.mark.parametrize(("ident", "words"), [("KXXX", "runway two seven left, back-taxi, line up and wait")])
def test_in_the_united_states_it_is_a_back_taxi(tmp_path, ident, words):
    db, engine = airport(tmp_path, ident)
    taxi = engine.handle(Intent("request_taxi"), db.station(GROUND, *APRON), ISSUE, jet(*APRON))
    assert "taxi via Charlie, expect back-taxi, altimeter" in taxi.text, taxi.text
    reply = engine.handle(Intent("ready_for_departure"), db.station(TOWER, *APRON), ISSUE, jet(50.9993, -0.999))
    assert reply.text.endswith(words), reply.text


def test_a_backtrack_is_the_last_choice_after_the_threshold_and_an_intersection():
    rows = """1201 52.0000 -1.0000 both 0 apron
1201 52.0010 -1.0000 both 1 west_end
1201 52.0010 -0.9600 both 2 east_end
1201 52.0010 -0.9800 both 3 middle
1202 0 3 twoway taxiway_E A
1202 1 3 twoway runway 09/27
1202 3 2 twoway runway 09/27""".splitlines()
    for_27 = route_to_runway(parse_taxi(rows), 52.0, -1.0, "27", (52.001, -0.96))
    assert for_27.backtrack and for_27.points[-1] == (52.001, -0.98)
    with_threshold = [*rows, "1202 0 2 twoway taxiway_E B"]
    assert not route_to_runway(parse_taxi(with_threshold), 52.0, -1.0, "27", (52.001, -0.96)).backtrack


def test_landed_with_no_exit_ahead_the_tower_says_to_backtrack_to_the_last_one(tmp_path):
    db, engine = airport(tmp_path)
    tower = db.station(TOWER, *APRON)
    flight = engine.flight
    flight.has_been_airborne, flight.phase, flight.runway = True, Phase.LANDED, "27L"
    flight.contacted.add(tower.key)
    # Landed on 27L and slowing past taxiway C, a little way from the far end: no taxiway ahead.
    reply = engine.tick(tower, ISSUE, jet(51.0, -1.012, heading=270.0, speed=12.0))
    assert reply.text.endswith("backtrack runway two seven left, vacate via Charlie, when vacated contact Testfield Ground "
                               "one two one decimal seven"), reply.text  # fmt: skip


def test_landed_with_an_exit_still_ahead_it_is_the_next_available_one(tmp_path):
    db, engine = airport(tmp_path)
    tower = db.station(TOWER, *APRON)
    flight = engine.flight
    flight.has_been_airborne, flight.phase, flight.runway = True, Phase.LANDED, "27L"
    flight.contacted.add(tower.key)
    reply = engine.tick(tower, ISSUE, jet(51.0, -0.99, heading=270.0, speed=12.0))  # taxiway C still ahead
    assert "vacate at the next available exit" in reply.text
