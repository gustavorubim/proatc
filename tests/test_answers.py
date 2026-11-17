"""Fewer dead ends: questions answered from what was assigned, "unable" from the pilot, levels, runways and
frequency changes asked for anywhere, and an answer that says who can help when no rule covers a call."""

import pytest

from proatc.atis import Issue
from proatc.engine import Phase
from proatc.flightplan import FlightPlan
from proatc.intents import ITEMS, Intent, parse_json
from proatc.weather import Weather
from tests.deadends import ISSUE, PLAN, at, build_world, measure, plausible, read_back

CALLSIGN = "Golf Alfa Bravo Charlie Delta"
NOVEMBER = "November one two three Alfa Bravo"
LIGHT = Issue("D", Weather(wind_dir_true=270, wind_kt=3, qnh_hpa=1013.2), 12 * 3600)
SIM_WEATHER = {"wx_qnh_pas": 101320.0, "wx_wind_alt_m": [0.0], "wx_wind_kt": [10.0], "wx_wind_dir": [270.0]}


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path)


def flight(world, plan=PLAN):
    engine = world.engine()
    if plan:
        engine.set_plan(plan)
    return engine


def us(raw):
    return {**raw, "tail_number": "N123AB"}


def said(engine, intent, station, raw, issue=ISSUE):
    return engine.handle(intent, station, issue, raw).text


def climbing_with_departure(world, ident="EGXX", lon=-1.12):
    """Airborne after takeoff, checked in with departure and cleared to the first step."""
    engine = flight(world, PLAN if ident == "EGXX" else FlightPlan(ident, "KEST", [], 24000))
    departure = world.station(ident, "departure")
    raw = at(51.0 if ident == "EGXX" else 41.0, lon, 3000, heading=270)
    if ident != "EGXX":
        raw = us(raw)
    engine.flight.origin_ident, engine.flight.rules, engine.flight.cleared_ft = ident, "IFR", 5000
    engine.handle(Intent("check_in"), departure, ISSUE, raw)
    read_back(engine, departure, raw)
    return engine, departure, raw


def vectored(world, issue=ISSUE):
    """With Eastfield Approach, checked in and given the first vector."""
    engine = flight(world)
    engine.flight.has_been_airborne, engine.flight.origin_ident, engine.flight.rules = True, "EGXX", "IFR"
    engine.flight.cleared_ft, engine.flight.descent_given = 10000, True
    approach = world.station("LFXX", "approach")
    raw = at(51.0, 4.7, 10000, heading=90)
    engine.handle(Intent("check_in"), approach, issue, raw)
    read_back(engine, approach, raw)
    return engine, approach, raw


# ---- the matrix ---------------------------------------------------------------------


def test_every_call_a_pilot_plausibly_makes_gets_an_answer_from_the_rules(tmp_path):
    matrix = measure(tmp_path)
    assert matrix.unanswered(only_plausible=True) == []
    # Left to the free-form reply on purpose: speech nobody foresaw, and a blind call to other traffic.
    assert {key[0] for key in matrix.unanswered()} <= {"other", "traffic_call"}
    assert sum(1 for key in matrix.cells if plausible(key[0], key[1])) > 3000


def test_the_language_model_is_told_the_new_calls_and_their_items():
    from proatc.intents import SYSTEM

    for name in ("request_information", "acknowledge", "unable", "request_delay", "request_return", "request_runway"):
        assert f'"{name}"' in SYSTEM
    assert '"item"' in SYSTEM and all(f'"{item}"' in SYSTEM for item in ITEMS)
    assert parse_json('{"intent": "request_information", "item": "cleared level"}').item == "altitude"
    assert parse_json('{"intent": "say_again", "item": "altimeter"}').item == "qnh"
    assert parse_json('{"intent": "request_information", "item": "the moon"}').item is None
    from dataclasses import fields

    from proatc.bench import UNDERSTOOD
    from proatc.intents import INTENTS

    known = {field.name for field in fields(Intent)}
    assert all(meant["intent"] in INTENTS and set(meant) <= known for _, _, meant in UNDERSTOOD)
    assert {meant["intent"] for _, _, meant in UNDERSTOOD} >= {"request_information", "unable", "request_runway"}


# ---- confirm and say again ---------------------------------------------------------------


def test_the_cleared_level_is_confirmed_from_what_was_assigned(world):
    engine, departure, raw = climbing_with_departure(world)
    assert engine.flight.cleared_ft == 15000
    asked = Intent("request_information", item="altitude")
    assert said(engine, asked, departure, raw) == f"{CALLSIGN}, climb flight level one five zero"
    right = Intent("request_information", item="altitude", altitude=15000)
    assert said(engine, right, departure, raw) == f"{CALLSIGN}, affirm, flight level one five zero"
    wrong = Intent("request_information", item="altitude", altitude=16000)
    assert said(engine, wrong, departure, raw) == f"{CALLSIGN}, negative, climb flight level one five zero"
    level = {**raw, "elevation_m": 15000 * 0.3048}
    assert said(engine, asked, departure, level) == f"{CALLSIGN}, maintain flight level one five zero"


def test_an_american_controller_verifies_the_assigned_altitude(world):
    engine, departure, raw = climbing_with_departure(world, "KWST", lon=-90.12)
    asked = Intent("request_information", item="altitude", altitude=15000)
    assert said(engine, asked, departure, raw) == f"{NOVEMBER}, affirmative, one five thousand"
    wrong = Intent("request_information", item="altitude", altitude=10000)
    assert said(engine, wrong, departure, raw) == f"{NOVEMBER}, negative, climb and maintain one five thousand"


def test_what_was_never_assigned_is_said_to_be_unassigned(world):
    engine = flight(world)
    ground, parked = world.station("EGXX", "ground"), at(51.003, -1.0, 100, on_ground=True)
    answers = [said(engine, Intent("request_information", item=item), ground, parked) for item in
               ("altitude", "heading", "speed", "squawk", "clearance", "route")]  # fmt: skip
    assert answers == [
        f"{CALLSIGN}, Westfield Ground, no level assigned",
        f"{CALLSIGN}, no heading assigned",
        f"{CALLSIGN}, no speed restrictions",
        f"{CALLSIGN}, no squawk assigned",
        f"{CALLSIGN}, no clearance issued",
        f"{CALLSIGN}, no taxi clearance issued",
    ]


def test_say_again_the_heading_repeats_only_the_heading_while_vectored(world):
    engine, approach, raw = vectored(world)
    heading = engine.flight.arrival.heading
    assert heading is not None
    from proatc import phraseology as say

    again = said(engine, Intent("say_again", item="heading"), approach, raw)
    assert again == f"{CALLSIGN}, fly heading {say.heading(heading)}"
    assert said(engine, Intent("request_information", item="speed"), approach, raw) == f"{CALLSIGN}, speed two two zero knots"
    distance = said(engine, Intent("request_information", item="distance"), approach, raw)
    assert distance == f"{CALLSIGN}, two five miles from touchdown"
    assert said(engine, Intent("request_information", item="approach"), approach, raw) == (
        f"{CALLSIGN}, expect I L S approach runway two seven"
    )


def test_say_again_with_nothing_said_tells_the_pilot_there_was_nothing(world):
    engine = flight(world)
    tower, parked = world.station("EGXX", "tower"), at(51.003, -1.0, 100, on_ground=True)
    assert said(engine, Intent("say_again"), tower, parked) == f"{CALLSIGN}, Westfield Tower, no instructions for you"


def test_the_frequency_of_a_hand_off_not_yet_made_is_given_again(world):
    engine = flight(world)
    engine.flight.origin_ident, engine.flight.rules = "EGXX", "IFR"
    engine.flight.phase, engine.flight.has_been_airborne = Phase.AIRBORNE, True
    tower, departure = world.station("EGXX", "tower"), world.station("EGXX", "departure")
    climbing = at(51.0, -1.12, 3000)
    engine.flight.contacted.add(tower.key)
    assert engine.tick(tower, ISSUE, climbing).text == f"{CALLSIGN}, contact Westfield Departure one two five decimal five"
    asked = Intent("request_information", item="frequency")
    assert said(engine, asked, tower, climbing) == f"{CALLSIGN}, contact Westfield Departure one two five decimal five"
    assert said(engine, asked, departure, climbing) == f"{CALLSIGN}, Westfield Departure, remain this frequency"


def test_the_runway_and_approach_at_the_destination_come_from_the_weather_there(world):
    """From a real flight: a centre asked for the runway and the approach in use answered 'say intentions'."""
    engine = flight(world)
    engine.flight.has_been_airborne, engine.flight.rules, engine.flight.descent_given = True, "IFR", True
    east = world.centers.on_frequency(132100, 51.0, 3.0)
    cruising = {**at(51.0, 3.5, 20000, heading=90), **SIM_WEATHER}
    runway = said(engine, Intent("request_information", item="runway"), east, cruising, None)
    assert runway == f"{CALLSIGN}, Eastland Control, Eastfield runway two seven in use"
    approach = said(engine, Intent("request_information", item="approach"), east, cruising, None)
    assert approach == f"{CALLSIGN}, Eastfield, expect I L S approach runway two seven"
    unknown = said(engine, Intent("request_information", item="runway"), east, at(51.0, 3.5, 20000, heading=90), None)
    assert unknown == f"{CALLSIGN}, Eastfield runway in use not known, Eastfield Approach one two four decimal three will advise"


def test_the_taxi_route_is_said_again_from_where_the_aircraft_is(tmp_path):
    from tests.test_realism import APRON, ground_world, taxiing

    db, engine, ground = ground_world(tmp_path)
    issue = Issue("C", Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1013.2), 12 * 3600)
    engine.handle(Intent("request_taxi"), ground, issue, taxiing(APRON[0]))
    again = said(engine, Intent("request_information", item="route"), ground, taxiing(APRON[0]), issue)
    assert again.startswith(f"{CALLSIGN}, taxi to holding point runway two seven right via") and "hold short runway" in again


def test_a_wind_check_and_the_pressure_on_any_frequency(world):
    engine = flight(world)
    tower = world.station("LFXX", "tower")
    final = at(51.0, 5.43, 1200)
    wind = said(engine, Intent("request_information", item="wind"), tower, final)
    assert wind == f"{CALLSIGN}, Eastfield Tower, wind two seven zero degrees one zero knots"
    assert said(engine, Intent("request_information", item="qnh"), tower, final) == f"{CALLSIGN}, Q N H one zero one three"
    us_tower = world.station("KEST", "tower")
    us_final = us(at(41.0, -87.93, 1900))
    assert said(engine, Intent("request_information", item="wind"), us_tower, us_final) == (
        f"{NOVEMBER}, Easttown Tower, wind two seven zero at one zero"
    )
    assert (
        said(engine, Intent("request_information", item="qnh"), us_tower, us_final)
        == f"{NOVEMBER}, altimeter two niner niner two"
    )


# ---- acknowledgements and check-ins ----------------------------------------------------------


def test_thanks_and_good_day_get_silence_and_a_wilco_still_owes_the_readback(world):
    engine, departure, raw = climbing_with_departure(world)
    assert engine.handle(Intent("acknowledge"), departure, ISSUE, raw).text == ""
    engine.flight.cleared_ft = 15000
    climb = engine.handle(Intent("request_climb", altitude=20000), departure, ISSUE, raw)
    assert climb.expect == {"altitude": 20000}
    wilco = engine.handle(Intent("acknowledge"), departure, ISSUE, raw)
    assert wilco.text == f"{CALLSIGN}, read back your cleared level"


def test_a_check_in_after_first_contact_is_answered_on_every_station(world):
    engine, departure, raw = climbing_with_departure(world)
    engine.flight.cleared_ft = 24000
    assert said(engine, Intent("check_in"), departure, raw) == f"{CALLSIGN}, roger"
    engine, approach, raw = vectored(world)
    assert said(engine, Intent("check_in"), approach, raw) == f"{CALLSIGN}, roger"
    ground = world.station("EGXX", "ground")
    parked = at(51.003, -1.0, 100, on_ground=True)
    assert said(engine, Intent("check_in"), ground, parked) == f"{CALLSIGN}, Westfield Ground, pass your message"
    us_ground = world.station("KWST", "ground")
    assert said(engine, Intent("check_in"), us_ground, us(at(41.003, -90.0, 700, on_ground=True))) == (
        f"{NOVEMBER}, Westtown Ground, go ahead"
    )


# ---- unable, not ready, return, frequency change ---------------------------------------------


def test_unable_to_climb_keeps_the_aircraft_where_it_is(world):
    engine, departure, raw = climbing_with_departure(world)
    engine.handle(Intent("request_climb", altitude=20000), departure, ISSUE, raw)
    unable = engine.handle(Intent("unable"), departure, ISSUE, raw)
    assert unable.text == f"{CALLSIGN}, roger, maintain altitude three thousand feet, advise when able"
    assert engine.flight.cleared_ft == 3000 and unable.expect == {"altitude": 3000}
    engine, departure, raw = climbing_with_departure(world, "KWST", lon=-90.12)
    engine.handle(Intent("request_climb", altitude=20000), departure, ISSUE, raw)
    assert said(engine, Intent("unable"), departure, raw) == f"{NOVEMBER}, roger, maintain three thousand, advise when able"


def test_unable_to_turn_stops_the_heading_watch(world):
    from proatc.engine import Reply

    engine, approach, raw = vectored(world)
    engine.flight.pending = Reply(f"{CALLSIGN}, turn left heading zero seven zero", {"heading": 70})
    assert (
        said(engine, Intent("unable"), approach, raw) == f"{CALLSIGN}, roger, continue present heading, advise when able to turn"
    )
    assert engine.flight.arrival.heading is None and engine.flight.pending is None


def test_not_ready_at_the_runway_cancels_the_takeoff_clearance(world):
    for ident, name, cancel in (("EGXX", CALLSIGN, "cancel takeoff"), ("KWST", NOVEMBER, "cancel takeoff clearance")):
        engine = flight(world, None)
        tower = world.station(ident, "tower")
        short = at(51.0006 if ident == "EGXX" else 41.0006, -0.979 if ident == "EGXX" else -89.979, 100, on_ground=True)
        short = short if ident == "EGXX" else us(short)
        engine.handle(Intent("ready_for_departure"), tower, ISSUE, short)
        assert engine.flight.phase == Phase.TAKEOFF_CLEARED
        assert (
            said(engine, Intent("request_delay"), tower, short)
            == f"{name}, roger, hold position, {cancel}, report ready for departure"
        )
        assert engine.flight.phase == Phase.TAXI_OUT


def test_not_ready_elsewhere_on_the_ground_is_report_ready(world):
    engine = flight(world)
    parked = at(51.003, -1.0, 100, on_ground=True)
    assert said(engine, Intent("request_delay"), world.station("EGXX", "delivery"), parked) == (
        f"{CALLSIGN}, Westfield Delivery, roger, report ready"
    )
    us_parked = us(at(41.003, -90.0, 700, on_ground=True))
    assert said(engine, Intent("request_delay"), world.station("KWST", "delivery"), us_parked) == (
        f"{NOVEMBER}, Westtown Delivery, roger, advise ready"
    )


def test_a_return_to_the_stand_and_then_a_new_taxi(world):
    engine = flight(world)
    ground = world.station("EGXX", "ground")
    moving = {**at(51.003, -1.0, 100, on_ground=True), "groundspeed_ms": 5.0}
    engine.handle(Intent("request_taxi"), ground, ISSUE, moving)
    read_back(engine, ground, moving)
    labels = [item["label"] for item in engine.suggestions(ground, ISSUE, moving)]
    assert "Return to stand" in labels
    back = said(engine, Intent("request_return"), ground, moving)
    assert back == f"{CALLSIGN}, roger, taxi to parking" and engine.flight.phase == Phase.TAXI_IN
    assert "Request taxi" in [item["label"] for item in engine.suggestions(ground, ISSUE, moving)]
    again = said(engine, Intent("request_taxi"), ground, moving)
    assert again == f"{CALLSIGN}, taxi to holding point runway two seven, Q N H one zero one three"


def test_a_return_once_airborne_is_a_diversion_back_to_the_departure_airport(world):
    engine, departure, raw = climbing_with_departure(world)
    back = said(engine, Intent("request_return"), departure, raw)
    assert back.startswith(f"{CALLSIGN}, roger, cleared to Westfield, turn")
    assert engine.flight.plan.destination == "EGXX"


def test_a_frequency_change_is_approved_and_an_instrument_flight_comes_back(world):
    engine, departure, raw = climbing_with_departure(world)
    assert said(engine, Intent("report_leaving"), departure, raw) == f"{CALLSIGN}, approved, report back on frequency"
    engine = flight(world)
    engine.flight.origin_ident, engine.flight.rules = "EGXX", "IFR"
    engine.flight.phase, engine.flight.has_been_airborne = Phase.AIRBORNE, True
    tower = world.station("EGXX", "tower")
    assert said(engine, Intent("report_leaving"), tower, at(51.0, -1.12, 3000)) == (
        f"{CALLSIGN}, Westfield Tower, contact Westfield Departure one two five decimal five"
    )


# ---- levels anywhere in the air ---------------------------------------------------------------


def test_higher_and_lower_are_answered_with_departure_approach_and_the_centre(world):
    engine, departure, raw = climbing_with_departure(world)
    # Still climbing (at three thousand feet): "lower" is a lower level to climb to, not a descent.
    assert said(engine, Intent("request_descent"), departure, raw) == f"{CALLSIGN}, climb flight level one three zero"
    engine, approach, raw = vectored(world)
    assert said(engine, Intent("request_climb"), approach, raw).startswith(f"{CALLSIGN}, unable higher, maintain")
    lower = said(engine, Intent("request_descent", altitude=1000), approach, raw)
    assert lower == f"{CALLSIGN}, descend altitude two thousand five hundred feet"  # where the approach is joined, no lower
    engine = flight(world)
    engine.flight.has_been_airborne, engine.flight.cleared_ft, engine.flight.rules = True, 36000, "IFR"
    west = world.centers.on_frequency(127500, 51.0, 0.0)
    cruising = at(51.0, 0.5, 36000, heading=90)
    engine.flight.contacted.add(west.key)
    assert said(engine, Intent("request_climb"), west, cruising) == f"{CALLSIGN}, climb flight level three eight zero"


def test_request_higher_from_an_oceanic_controller_needs_no_level(world):
    engine = flight(world)
    engine.flight.has_been_airborne, engine.flight.cleared_ft = True, 36000
    ocean = world.centers.on_frequency(127900, 51.0, -8.0)
    engine.flight.contacted.add(ocean.key)
    assert said(engine, Intent("request_climb"), ocean, at(51.0, -8.0, 36000)) == (
        f"{CALLSIGN}, climb flight level three eight zero, report reaching"
    )


# ---- runways and approaches asked for ------------------------------------------------------


def test_a_runway_is_declined_with_the_reason_when_the_wind_or_the_airport_rules_it_out(world):
    engine = flight(world)
    ground, parked = world.station("EGXX", "ground"), at(51.003, -1.0, 100, on_ground=True)
    assert said(engine, Intent("request_runway", runway="09"), ground, parked) == (
        f"{CALLSIGN}, Westfield Ground, unable runway zero niner, tailwind one zero knots, runway two seven in use"
    )
    assert said(engine, Intent("request_runway", runway="18"), ground, parked) == (
        f"{CALLSIGN}, unable runway one eight, no such runway, runway two seven in use"
    )


def test_a_runway_the_wind_allows_is_given_with_a_new_taxi_route(world):
    engine = flight(world)
    ground = world.station("EGXX", "ground")
    moving = {**at(51.003, -1.0, 100, on_ground=True), "groundspeed_ms": 5.0}
    engine.handle(Intent("request_taxi"), ground, LIGHT, moving)
    read_back(engine, ground, moving)
    assert said(engine, Intent("request_runway", runway="09"), ground, moving, LIGHT) == (
        f"{CALLSIGN}, taxi to holding point runway zero niner"
    )
    tower = world.station("EGXX", "tower")
    short = at(51.0006, -1.021, 100, on_ground=True)
    cleared = said(engine, Intent("ready_for_departure"), tower, short, LIGHT)
    assert (
        cleared == f"{CALLSIGN}, Westfield Tower, runway zero niner, cleared for takeoff, wind two seven zero degrees three knots"
    )


def test_an_arrival_is_planned_again_for_the_runway_asked_for_and_kept_on_it(world):
    engine, approach, raw = vectored(world, LIGHT)
    changed = said(engine, Intent("request_runway", runway="09"), approach, raw, LIGHT)
    assert changed.startswith(f"{CALLSIGN}, roger, expect") and changed.endswith("runway zero niner")
    assert engine.flight.arrival.runway == "09"
    read_back(engine, approach, raw)
    following = engine.tick(approach, LIGHT, raw)
    assert following is None or "runway change" not in following.text  # not taken back to the runway in use


def test_an_approach_asked_of_the_centre_is_kept_for_approach(world):
    engine = flight(world)
    engine.flight.has_been_airborne, engine.flight.rules, engine.flight.descent_given = True, "IFR", True
    east = world.centers.on_frequency(132100, 51.0, 3.0)
    cruising = {**at(51.0, 3.5, 20000, heading=90), **SIM_WEATHER}
    asked = said(engine, Intent("request_approach", approach="RNAV"), east, cruising, None)
    assert asked == f"{CALLSIGN}, Eastland Control, identified, roger, expect R NAV approach runway two seven"
    assert said(engine, Intent("request_approach", approach="NDB"), east, cruising, None) == (
        f"{CALLSIGN}, unable, no N D B approach runway two seven, expect R NAV approach runway two seven"
    )
    approach = world.station("LFXX", "approach")
    first = said(engine, Intent("check_in"), approach, at(51.0, 4.7, 10000, heading=90))
    assert "expect R NAV approach runway two seven" in first


# ---- no rule: who can help ----------------------------------------------------------------


def test_a_call_no_rule_covers_is_answered_with_who_can_help_or_unable(world):
    engine, departure, raw = climbing_with_departure(world)
    tower = world.station("EGXX", "tower")
    assert said(engine, Intent("request_climb"), tower, raw) == (
        f"{CALLSIGN}, Westfield Tower, contact Westfield Departure one two five decimal five for higher"
    )
    assert engine.flight.pending.expect == {"frequency": "125.500"}
    ground = world.station("EGXX", "ground")
    parked = at(51.003, -1.0, 100, on_ground=True)
    assert said(engine, Intent("request_crossing"), ground, parked) == f"{CALLSIGN}, Westfield Ground, unable crossing"
    assert engine.handle(Intent("other"), ground, ISSUE, parked) is None  # unforeseen speech: the free-form reply


def test_the_new_buttons_appear_where_a_pilot_wants_them(world):
    engine, departure, raw = climbing_with_departure(world)
    engine.handle(Intent("request_climb", altitude=20000), departure, ISSUE, raw)
    labels = [item["label"] for item in engine.suggestions(departure, ISSUE, raw)]
    assert labels[0] == "Read back" and "Unable" in labels and "Confirm level" in labels
    engine, us_departure, us_raw = climbing_with_departure(world, "KWST", lon=-90.12)
    assert "Verify altitude" in [item["label"] for item in engine.suggestions(us_departure, ISSUE, us_raw)]
    engine = flight(world)
    engine.flight.has_been_airborne, engine.flight.rules, engine.flight.descent_given = True, "IFR", True
    engine.flight.cleared_ft = 20000
    east = world.centers.on_frequency(132100, 51.0, 3.0)
    buttons = {item["label"]: item for item in engine.suggestions(east, None, at(51.0, 3.5, 20000, heading=90))}
    assert buttons["Expected approach"]["intent"] == {"intent": "request_information", "item": "approach"}
    tower = world.station("LFXX", "tower")
    assert "Wind check" in [item["label"] for item in engine.suggestions(tower, ISSUE, at(51.0, 5.43, 1200))]
