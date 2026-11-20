"""The arrival's vertical profile: never cleared for an approach from too high, step-down descents on the way in,
turned out when too high close in, and told when well above the glide path on final.

A real flight found these: cleared for the ILS thirty miles out, 2,400 ft above the glide path, at the sector
altitude, then handed to the tower sixteen miles out 2,700 ft above the path.
"""

import math

from proatc.intents import Intent
from proatc.vectors import locate
from tests.test_ifr import CALLSIGN, ISSUE, at, world  # noqa: F401 (world is a fixture)

THRESHOLD = (51.0, 5.37)  # runway 27 at Eastfield, 300 ft
NM_LON = 1 / (60 * math.cos(math.radians(51.0)))  # one mile of longitude at this latitude


def out(miles, feet, heading=270.0):
    """On the extended centreline of runway 27, `miles` out, inbound."""
    return at(51.0, THRESHOLD[1] + miles * NM_LON, feet=feet, heading=heading)


def arrive(world, raw):  # noqa: F811
    db, engine, _ = world
    engine.flight.has_been_airborne, engine.flight.origin_ident, engine.flight.cleared_ft = True, "EGXX", 13000
    approach = db.station(124300, 51.0, 5.0)
    first = engine.handle(Intent("check_in"), approach, ISSUE, raw)
    engine.flight.pending = None  # read back
    return engine, approach, first


def test_a_straight_in_too_high_to_make_the_glide_path_is_given_a_descent_not_the_approach(world):  # noqa: F811
    # Twenty-five miles out at 12,000 ft: the path is at 8,260 ft. Cleared now, it could not get down in time.
    engine, approach, first = arrive(world, out(25, 12000))
    assert "cleared" not in first.text
    assert "descend altitude two thousand five hundred feet" in first.text
    assert first.expect["altitude"] == 2500
    assert engine.flight.arrival.stage == "descend"
    # Lower, twenty miles out: now it can, and it is cleared, with one eight zero to five miles this far out.
    cleared = engine.tick(approach, ISSUE, out(20, 6000))
    assert "two zero miles from touchdown, cleared I L S approach runway two seven" in cleared.text
    assert "speed one eight zero knots until five miles" in cleared.text
    assert cleared.expect["runway"] == "27"


def test_the_approach_is_given_where_the_aircraft_can_make_the_path(world):  # noqa: F811
    engine, approach, first = arrive(world, out(20, 6000))
    assert "cleared I L S approach runway two seven" in first.text


def test_too_high_close_in_is_turned_out_for_the_descent(world):  # noqa: F811
    engine, approach, _ = arrive(world, out(35, 13000))
    final_nm = engine.flight.arrival.final_nm
    # Just outside the joining point at 7,000 ft: the path there is near 4,000 ft, the joining altitude 2,500 ft.
    near = out(final_nm + 1.5, 7000)
    turned = engine.tick(approach, ISSUE, near)
    assert turned.text.startswith(f"{CALLSIGN}, too high to join the glide path, turn ")
    assert "cleared" not in turned.text
    assert "heading" in turned.expect and engine.flight.arrival.stage == "downwind"
    engine.flight.pending = None  # read back
    # Flying out, still too high: not turned out again and again; nothing new is said.
    heading = turned.expect["heading"]
    for miles, feet in ((final_nm + 2.5, 6800), (final_nm + 3.5, 6400)):
        assert engine.tick(approach, ISSUE, out(miles, feet, heading=heading)) is None
    # Far enough out and low enough: turned back in, and cleared for the approach.
    back = engine.tick(approach, ISSUE, out(final_nm + 8, 3500, heading=heading))
    assert "cleared I L S approach runway two seven" in back.text


def test_never_cleared_from_above_the_floor_beyond_the_sector_altitude_circle(world):  # noqa: F811
    db, engine, _ = world

    class Floors:  # a minimum sector altitude of 7,000 ft and higher terrain beyond its circle
        def airport_safe_ft(self, ident):
            return 7000

        def grid_safe_ft(self, lat, lon):
            return 6500

        def position(self, *args):
            return None

    engine.navfix = Floors()
    engine, approach, first = arrive(world, out(28, 12000))
    assert "cleared" not in first.text
    assert first.expect["altitude"] == 7000  # down to the floor out here, and no lower
    for miles in (27, 26.5, 26):
        reply = engine.tick(approach, ISSUE, out(miles, 11500))
        assert reply is None or "cleared" not in reply.text  # still too high: no approach clearance


def test_the_sector_altitude_circle_is_measured_from_the_airport_not_the_threshold(world):  # noqa: F811
    db, engine, _ = world

    class Floors:
        def airport_safe_ft(self, ident):
            return 7000

        def grid_safe_ft(self, lat, lon):
            return 6500

        def position(self, *args):
            return None

    engine.navfix = Floors()
    # 24.5 miles from the threshold of 27, which is east of the airport's reference point: over 25 from the airport.
    engine, approach, first = arrive(world, out(24.5, 9000))
    assert first.expect["altitude"] == 7000  # the floor outside the circle, not the approach's 2,500 ft
    assert "cleared" not in first.text


def test_a_leg_given_beyond_the_sector_altitude_circle_is_brought_down_inside_it(world):  # noqa: F811
    db, engine, _ = world

    class Floors:
        def airport_safe_ft(self, ident):
            return 7000

        def grid_safe_ft(self, lat, lon):
            return 6500

        def position(self, *args):
            return None

    engine.navfix = Floors()
    south = 5 / 60  # five miles south of the extended centreline
    engine, approach, first = arrive(world, at(51.0 - south, THRESHOLD[1] + 27 * NM_LON, feet=9000, heading=270))
    assert first.expect["altitude"] == 7000 and "heading" in first.expect  # a leg toward final, at the floor out here
    engine.flight.pending = None
    stage = engine.flight.arrival.stage
    inside = engine.tick(approach, ISSUE, at(51.0 - south, THRESHOLD[1] + 23 * NM_LON, feet=7000, heading=270))
    assert "descend altitude two thousand five hundred feet" in inside.text  # inside the circle: down to the approach's
    assert engine.flight.arrival.stage == stage


def test_well_above_the_glide_path_on_final_is_told_and_the_tower_waits(world):  # noqa: F811
    engine, approach, first = arrive(world, out(15, 3000))
    assert "cleared I L S approach runway two seven" in first.text
    assert "speed one six zero knots until four miles" in first.text  # close in: one six zero to four miles
    position = locate(*[out(8, 4000)[k] for k in ("lat", "lon")], engine.flight.arrival.threshold, 270)
    assert 7.5 < position.along_nm < 8.5
    high = engine.tick(approach, ISSUE, out(8, 4000))  # the path is at 2,850 ft
    assert high.text == f"{CALLSIGN}, you appear well above the glide path, say intentions"
    assert high.event == "high_on_approach"
    assert engine.tick(approach, ISSUE, out(7.8, 3900)) is None  # said once; not handed on meanwhile
    handed = engine.handle(Intent("established"), approach, ISSUE, out(7.6, 3800))
    assert handed.text == f"{CALLSIGN}, contact Eastfield Tower one one niner decimal one"


def test_back_on_the_glide_path_the_hand_off_follows(world):  # noqa: F811
    engine, approach, _ = arrive(world, out(15, 3000))
    assert engine.tick(approach, ISSUE, out(8, 4000)).event == "high_on_approach"
    handed = engine.tick(approach, ISSUE, out(7.5, 2700))  # the path is at 2,690 ft
    assert handed.text == f"{CALLSIGN}, contact Eastfield Tower one one niner decimal one"


def test_a_base_leg_beyond_the_circle_is_turned_toward_final_and_cleared_inside_it(world):  # noqa: F811
    db, engine, _ = world

    class Floors:
        def airport_safe_ft(self, ident):
            return 3000

        def grid_safe_ft(self, lat, lon):
            return 9000  # high ground in the same one-degree cell, far from the airport

        def position(self, *args):
            return None

    engine.navfix = Floors()
    north = 12 / 60
    engine, approach, first = arrive(world, at(51.0 + north, THRESHOLD[1] + 26 * NM_LON, feet=9000, heading=180))
    assert "cleared" not in first.text and engine.flight.arrival.stage == "base"
    # Three miles from the centreline, 26 miles out, at the floor: the path where final would be joined is far below.
    close = at(51.0 + 3 / 60, THRESHOLD[1] + 26 * NM_LON, feet=9000, heading=180)
    turned = engine.tick(approach, ISSUE, close)
    assert turned.text == f"{CALLSIGN}, turn right heading two four zero"  # toward final, and no approach clearance
    engine.flight.pending = None
    assert engine.tick(approach, ISSUE, close) is None  # said once
    inside = at(51.0 + 2 / 60, THRESHOLD[1] + 23.5 * NM_LON, feet=9000, heading=240)
    cleared = engine.tick(approach, ISSUE, inside)
    assert "descend altitude two thousand five hundred feet" in cleared.text
    assert "cleared I L S approach runway two seven" in cleared.text


def test_an_aircraft_is_turned_out_for_height_only_once(world):  # noqa: F811
    engine, approach, _ = arrive(world, out(35, 13000))
    final_nm = engine.flight.arrival.final_nm
    assert "too high" in engine.tick(approach, ISSUE, out(final_nm + 1.5, 7000)).text
    engine.flight.pending = None
    heading = engine.flight.arrival.heading
    back = engine.tick(approach, ISSUE, out(final_nm + 8, 3500, heading=heading))
    assert "cleared I L S" in back.text or "turn" in back.text
    engine.flight.pending = None
    # Still high the second time round: cleared as it is, and told on final, not sent round again and again.
    engine.flight.arrival.stage = "base"
    again = engine.tick(approach, ISSUE, out(final_nm + 1.5, 7000))
    assert again is None or "too high" not in again.text


class HighGround:  # a low sector altitude, and high ground beyond its circle
    def airport_safe_ft(self, ident):
        return 3000

    def grid_safe_ft(self, lat, lon):
        return 9000

    def position(self, *args):
        return None


def test_where_final_is_joined_beyond_the_circle_the_approach_is_cleared_as_it_is(world):  # noqa: F811
    db, engine, _ = world
    engine.navfix = HighGround()
    engine.navfix.grid_safe_ft = lambda lat, lon: 12000  # the path is below this where final would be joined
    engine, approach, _ = arrive(world, at(51.0 + 12 / 60, THRESHOLD[1] + 34 * NM_LON, feet=12000, heading=180))
    engine.flight.arrival.stage = "base"  # on a base leg, far out
    reply = engine.tick(approach, ISSUE, at(51.0 + 3 / 60, THRESHOLD[1] + 34 * NM_LON, feet=12000, heading=180))
    # Nothing to wait for: turned toward final without the clearance, it would be turned out again and again.
    assert "cleared I L S approach runway two seven" in reply.text


def test_cleared_far_out_the_descent_on_the_glide_path_is_not_a_level_bust(world):  # noqa: F811
    db, engine, _ = world
    engine.navfix = HighGround()
    engine, approach, _ = arrive(world, out(28, 9000))
    arrival = engine.flight.arrival  # cleared for the approach twenty-eight miles out, at 9,000 ft
    arrival.stage, arrival.cleared_nm, engine.flight.cleared_ft, engine.flight.pending = "intercept", 28.0, 9000, None
    # Twenty-six miles out the glide path is at 8,580 ft: below the level given, and on the approach it was cleared for.
    on_path = {**out(26, 8580), "vs_fpm": -800.0}
    assert engine.tick(approach, ISSUE, on_path) is None


def test_the_approach_is_not_cleared_from_the_terrains_floor_but_inside_the_circle_at_its_own_altitude(world):  # noqa: F811
    # A real flight: thirty miles out at Los Angeles, held at 7,800 ft by the terrain's floor, and cleared for the ILS
    # there because the glide path is at 7,800 ft twenty-four miles out. No glideslope is received that far out: it
    # flew level through the path, and was 1,600 ft above it by eighteen miles.
    db, engine, _ = world

    class Floors:
        def airport_safe_ft(self, ident):
            return 7800

        def grid_safe_ft(self, lat, lon):
            return 7500

        def position(self, *args):
            return None

    engine.navfix = Floors()
    engine, approach, first = arrive(world, out(29.5, 9400))  # low enough to reach the path from 7,800 ft: that is not enough
    assert "cleared" not in first.text and first.expect["altitude"] == 7800
    engine.flight.pending = None
    assert engine.tick(approach, ISSUE, out(27, 7800)) is None  # at the floor, still outside the circle: nothing yet
    inside = engine.tick(approach, ISSUE, out(23.5, 7800))
    assert "descend altitude two thousand five hundred feet" in inside.text  # the approach's own altitude
    assert "cleared I L S approach runway two seven" in inside.text
