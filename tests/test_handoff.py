"""Faults found on a real flight into Edinburgh: no hand-off from one centre to the next, so the pilot was left
on a frequency nobody answered with nothing listed to tune; and the descent stepped down far too early."""

from dataclasses import replace
from types import SimpleNamespace

from proatc import config as config_module
from proatc.atis import AtisBook
from proatc.config import Config
from proatc.intents import Intent
from proatc.navdata import distance_nm
from proatc.radio import current_station
from proatc.server import airport_to_list, build_state
from proatc.voice_loop import Session
from tests.test_ifr import CALLSIGN, at, world  # noqa: F401  (a fixture)
from tests.test_radio_loop import FakeBridge

WEST, EAST = 127500, 132100  # Westland Control, west of 2 degrees east; Eastland Control beyond it


def tuned(db, khz, raw):
    """The station the radio loop finds: what the engine is really given, flight after flight."""
    return current_station(db, {**raw, "com1_khz": khz})[1]


def in_the_cruise(world):  # noqa: F811
    db, engine, centers = world
    db.centers = centers
    engine.flight.has_been_airborne, engine.flight.origin_ident = True, "EGXX"
    cruising = at(51.0, 1.0, feet=36000, heading=90, speed=230.0)
    west = tuned(db, WEST, cruising)
    engine.handle(Intent("check_in"), west, None, cruising)
    engine.flight.cleared_ft, engine.flight.pending = 36000, None
    return db, engine


def test_the_next_centre_is_given_before_the_boundary_is_crossed(world):  # noqa: F811
    db, engine = in_the_cruise(world)
    far = at(51.0, 1.5, feet=36000, heading=90, speed=230.0)
    assert engine.tick(tuned(db, WEST, far), None, far) is None  # twenty miles to run: not yet
    # 230 m/s for a minute and a half is eleven miles: the boundary is six ahead.
    nearly = at(51.0, 1.85, feet=36000, heading=90, speed=230.0)
    handed = engine.tick(tuned(db, WEST, nearly), None, nearly)
    assert handed.text == f"{CALLSIGN}, contact Eastland Control one three two decimal one"
    assert handed.expect == {"frequency": "132.100"}
    engine.flight.pending = None
    assert engine.tick(tuned(db, WEST, nearly), None, nearly) is None  # said once
    # Checked in with the next centre before the boundary: it keeps the flight, and does not send it back.
    east = tuned(db, EAST, nearly)
    assert engine.handle(Intent("check_in"), east, None, nearly).text == f"{CALLSIGN}, Eastland Control, identified"
    engine.flight.pending = None
    worked = engine.tick(east, None, at(51.0, 1.95, feet=36000, heading=90, speed=230.0))
    assert worked.text.startswith(f"{CALLSIGN}, descend flight level two six zero")  # its to descend, not to send back
    engine.flight.pending = None
    assert engine.flight.sector_ahead == "EASTLAND"
    inside = at(51.0, 2.4, feet=36000, heading=90, speed=230.0)
    assert engine.tick(tuned(db, EAST, inside), None, inside) is None and engine.flight.sector_ahead is None
    # Flying along the boundary, not toward it, nobody is handed anywhere.
    db, engine = in_the_cruise(world)
    along = at(51.0, 1.85, feet=36000, heading=0, speed=230.0)
    said = engine.tick(tuned(db, WEST, along), None, along)
    assert "contact" not in said.text and engine.flight.sector_ahead is None  # worked as usual, by the same centre


def test_just_across_the_boundary_the_last_centre_still_answers_and_hands_over(world):  # noqa: F811
    db, engine = in_the_cruise(world)
    # The aircraft was moved, or the pilot was slow: it is across without having been told.
    across = at(51.0, 2.3, feet=36000, heading=90, speed=230.0)
    west = tuned(db, WEST, across)
    assert west is not None and west.callsign == "Westland Control"  # before: nobody in range, and silence for good
    assert engine.tick(west, None, across).text == f"{CALLSIGN}, contact Eastland Control one three two decimal one"
    # The right centre for a position still wins over one that is merely near.
    assert tuned(db, EAST, across).callsign == "Eastland Control"
    assert tuned(db, EAST, at(51.0, 1.8, feet=36000)).callsign == "Eastland Control"  # near enough to answer
    # Far outside, a centre's frequency is silent.
    assert tuned(db, WEST, at(51.0, 4.0, feet=36000)) is None and tuned(db, EAST, at(51.0, 0.0, feet=36000)) is None


def test_close_in_a_centre_that_is_still_tuned_sends_the_flight_to_approach(world):  # noqa: F811
    db, engine = in_the_cruise(world)
    engine.flight.descent_given = True
    destination = db.airport("LFXX")
    # As it was on the real flight: the last centre's frequency still tuned, well inside approach's airspace.
    close = at(51.0, destination.lon - 0.7, feet=10000, heading=90, speed=120.0)
    assert distance_nm(close["lat"], close["lon"], destination.lat, destination.lon) < 30
    east = tuned(db, EAST, close)
    engine.handle(Intent("check_in"), east, None, close)
    engine.flight.pending = None
    said = engine.tick(east, None, close) or engine.handle(Intent("check_in"), east, None, close)
    assert engine.flight.told_approach and "contact Eastfield Approach one two four decimal three" in (
        said.text if said else engine.flight.last_said[east.key]
    )


def low_cruise(world):  # noqa: F811
    """Cruising at FL300, so that the whole descent is inside the last centre's airspace."""
    db, engine = in_the_cruise(world)
    engine.flight.plan = replace(engine.flight.plan, cruise_ft=30000)
    engine.flight.cleared_ft = 30000
    destination = db.airport("LFXX")
    out = lambda miles, feet: at(51.0, destination.lon - miles / (60 * 0.6293), feet=feet, heading=90, speed=230.0)  # noqa: E731
    start = out(111, 30000)  # (30000 - 13000) / 300 + 45 + 10 = 112 miles: the descent begins
    station = tuned(db, EAST, start)
    engine.handle(Intent("check_in"), station, None, start)
    engine.flight.pending = None
    first = engine.tick(station, None, start)
    assert first.text.startswith(f"{CALLSIGN}, descend flight level two zero zero")
    engine.flight.pending = None
    return engine, station, out


def test_the_next_step_down_waits_for_the_distance(world):  # noqa: F811
    engine, station, out = low_cruise(world)
    # Down to FL210 in six miles: far steeper than three degrees. On the real flight the next level came at once.
    assert engine.tick(station, None, out(105, 21000)) is None
    # A three degree path leaves FL200 at (20000 - 13000) / 300 + 45 + 10 = 78 miles; the step comes 5 before.
    assert engine.tick(station, None, out(90, 20200)) is None
    assert engine.tick(station, None, out(83, 20000)).text == f"{CALLSIGN}, descend flight level one three zero"


def test_a_three_degree_descent_is_never_held_up(world):  # noqa: F811
    engine, station, out = low_cruise(world)
    # At 300 ft a mile the aircraft is 1600 ft above its cleared level, still descending, when the next one comes.
    assert engine.tick(station, None, out(85, 22000)) is None
    assert engine.tick(station, None, out(83, 21600)).text == f"{CALLSIGN}, descend flight level one three zero"


def test_the_frequencies_listed_are_the_ones_the_pilot_needs(world):  # noqa: F811
    db, engine = in_the_cruise(world)
    session = SimpleNamespace(engine=engine)
    destination = db.airport("LFXX")
    assert airport_to_list(session, db, at(51.0, destination.lon - 0.1, feet=2000)).ident == "LFXX"  # at the airport
    inbound = at(51.0, destination.lon - 0.7, feet=10000)  # 27 miles out, as on the real flight: nothing was listed
    assert airport_to_list(session, db, inbound).ident == "LFXX"
    assert airport_to_list(session, db, at(51.0, 1.0, feet=36000)) is None  # in the cruise, far from both
    engine.set_plan(None)
    assert airport_to_list(session, db, inbound).ident == "LFXX"  # no plan: the nearest airport with a controller
    assert airport_to_list(session, db, at(51.0, destination.lon - 1.5, feet=10000)) is None


def test_the_app_is_told_the_centre_for_where_the_aircraft_is(world, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    db, engine = in_the_cruise(world)
    destination = db.airport("LFXX")
    # Still on the last centre's frequency, far past where it answers.
    lost = {**at(51.0, destination.lon - 0.7, feet=10000, heading=90), "com1_khz": WEST, "zulu_seconds": 43200.0}
    session = Session(Config(), emit=lambda event: None)
    session.bridge, session.db, session.book, session.engine = FakeBridge(lost), db, AtisBook(), engine
    session.stt, session.ready = SimpleNamespace(device="cpu"), True
    state = build_state(session, db)
    assert state["radio"]["station"] is None
    assert state["centre"] == {"mhz": "132.100", "callsign": "Eastland Control", "tuned": False}
    assert state["airport"]["ident"] == "LFXX"
    assert [f["mhz"] for f in state["airport"]["frequencies"] if f["kind"] == "approach"] == ["124.300"]

    session.bridge = FakeBridge({**lost, "com1_khz": 128600})  # Eastland's other frequency
    assert build_state(session, db)["centre"] == {"mhz": "128.600", "callsign": "Eastland Control", "tuned": True}
    session.bridge = FakeBridge({**lost, "on_ground": 1})
    assert build_state(session, db)["centre"] is None
