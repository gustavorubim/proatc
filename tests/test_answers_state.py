"""Answers from the flight as it is now, and only about what the station controls or knows.

Each test here comes from an independent review of the first version of `answers.py`: a requested runway kept
against the wind, an "unable" that cancelled another controller's clearance, a level reported from before the
approach clearance, a frequency change approved on the takeoff roll."""

from proatc.atis import Issue
from proatc.engine import Phase
from proatc.intents import Intent
from proatc.weather import Weather
from tests.deadends import ISSUE, at, measure, read_back
from tests.test_answers import (  # noqa: F401  (world is a fixture)
    CALLSIGN,
    LIGHT,
    SIM_WEATHER,
    climbing_with_departure,
    flight,
    said,
    vectored,
    world,
)

STRONG = Issue("E", Weather(wind_dir_true=270, wind_kt=18, qnh_hpa=1013.2), 12 * 3600)
CALM_SIM = {**SIM_WEATHER, "wx_wind_kt": [0.0]}


def east_centre(world, engine):  # noqa: F811
    engine.flight.has_been_airborne, engine.flight.rules, engine.flight.descent_given = True, "IFR", True
    return world.centers.on_frequency(132100, 51.0, 3.0)


# ---- runways asked for ------------------------------------------------------------------------


def test_a_runway_granted_en_route_is_dropped_when_the_wind_at_arrival_no_longer_allows_it(world):  # noqa: F811
    engine = flight(world)
    east = east_centre(world, engine)
    calm = {**at(51.0, 3.5, 20000, heading=90), **CALM_SIM}
    assert said(engine, Intent("request_runway", runway="09"), east, calm, None) == (
        f"{CALLSIGN}, Eastland Control, roger, expect runway zero niner"
    )
    approach = world.station("LFXX", "approach")
    first = said(engine, Intent("check_in"), approach, at(51.0, 4.7, 10000, heading=90), STRONG)
    assert "runway two seven" in first and engine.flight.arrival.runway == "27"
    tower = world.station("LFXX", "tower")
    landing = said(engine, Intent("position_report", position="final"), tower, at(51.0, 5.43, 1200), STRONG)
    assert "runway zero niner" not in landing


def test_no_runway_is_granted_when_nobody_knows_the_wind(world):  # noqa: F811
    engine = flight(world)
    east = east_centre(world, engine)
    assert said(engine, Intent("request_runway", runway="09"), east, at(51.0, 3.5, 20000, heading=90), None) == (
        f"{CALLSIGN}, Eastland Control, unable runway zero niner, wind not known, "
        "Eastfield Approach one two four decimal three will advise"
    )
    assert engine.flight.requested_runway is None


def test_a_runway_granted_for_departure_is_not_used_to_land_back_in_another_wind(world):  # noqa: F811
    engine = flight(world, None)
    ground, tower = world.station("EGXX", "ground"), world.station("EGXX", "tower")
    moving = {**at(51.003, -1.0, 100, on_ground=True), "groundspeed_ms": 5.0}
    engine.handle(Intent("request_circuits", flight_rules="VFR"), tower, LIGHT, at(51.003, -1.0, 100, on_ground=True))
    engine.handle(Intent("request_taxi"), ground, LIGHT, moving)
    engine.handle(Intent("request_runway", runway="09"), ground, LIGHT, moving)
    assert engine.flight.requested_runway == ("EGXX", "09", "departure")
    engine.tick(tower, LIGHT, at(51.0, -1.05, 1000))  # airborne: the departure runway has been used
    assert engine.flight.requested_runway is None
    downwind = said(engine, Intent("position_report", position="downwind"), tower, at(51.02, -1.0, 1000), STRONG)
    assert "runway two seven" in downwind


def test_changing_runway_while_held_for_traffic_does_not_clear_a_takeoff_from_the_far_end(world):  # noqa: F811
    engine = flight(world)
    ground, tower = world.station("EGXX", "ground"), world.station("EGXX", "tower")
    moving = {**at(51.003, -1.0, 100, on_ground=True), "groundspeed_ms": 5.0}
    engine.handle(Intent("request_taxi"), ground, LIGHT, moving)
    read_back(engine, ground, moving)
    short27 = at(51.0006, -0.979, 100, on_ground=True)
    engine.flight.contacted.add(tower.key)
    engine.flight.runway, engine.flight.waiting_takeoff = "27", True  # held short for traffic on final
    assert said(engine, Intent("request_runway", runway="09"), tower, short27, LIGHT) == (
        f"{CALLSIGN}, taxi to holding point runway zero niner"
    )
    read_back(engine, tower, short27)
    assert not engine.flight.waiting_takeoff and engine.tick(tower, LIGHT, short27) is None


def test_a_short_runway_is_not_given_to_an_airliner():
    from proatc.answers import acceptable_runway
    from proatc.navdata import Airport, Runway

    runways = [Runway("09", 51.0, -0.02, "27", 51.0, 0.02, 2800, 1), Runway("18", 51.01, 0.0, "36", 50.99, 0.0, 800, 1)]
    airport = Airport("EGQQ", "Q", 51.0, 0.0, 0.0, runways)
    calm = Weather(wind_kt=0)
    assert acceptable_runway(airport, "18", calm, {"aircraft_type": "A320"}) == "runway too short"
    assert acceptable_runway(airport, "18", calm, {"aircraft_type": "C172"}) is None
    assert acceptable_runway(airport, "18", None) == "wind not known"


# ---- unable ----------------------------------------------------------------------------------


def test_only_the_tower_that_cleared_the_landing_cancels_it_when_the_pilot_is_unable(world):  # noqa: F811
    engine, approach, raw = vectored(world)
    tower = world.station("LFXX", "tower")
    final = at(51.0, 5.43, 1200)
    engine.handle(Intent("position_report", position="final"), tower, ISSUE, final)
    assert engine.flight.phase == Phase.LANDING_CLEARED
    read_back(engine, tower, final)
    assert said(engine, Intent("unable"), approach, final) == f"{CALLSIGN}, say again"  # not approach's clearance
    assert engine.flight.phase == Phase.LANDING_CLEARED
    assert said(engine, Intent("unable"), tower, final) == f"{CALLSIGN}, roger, landing clearance cancelled, say intentions"


def test_an_information_service_gives_no_instructions_and_cancels_nothing(world):  # noqa: F811
    engine = flight(world, None)
    afis = world.db.station_of(world.db.airport("ESXX"), "tower")
    runway = said(engine, Intent("request_runway", runway="09"), afis, at(59.87, 15.0, 2000, heading=0), LIGHT)
    assert runway == (
        f"{CALLSIGN}, Fjordby Information, runway two seven in use, wind two seven zero degrees three knots, "
        "runway at your discretion"
    )
    engine.flight.phase = Phase.LANDING_CLEARED
    assert said(engine, Intent("unable"), afis, at(60.0, 15.06, 800), LIGHT) == f"{CALLSIGN}, roger"
    assert engine.flight.phase == Phase.LANDING_CLEARED


def test_unable_to_cross_a_runway_puts_the_crossing_back_and_waits_for_the_pilot_to_ask(tmp_path):
    from tests.test_realism import APRON, ground_world, taxiing

    db, engine, ground = ground_world(tmp_path)
    issue = Issue("C", Weather(wind_dir_true=270, wind_kt=10, qnh_hpa=1013.2), 12 * 3600)
    engine.handle(Intent("request_taxi"), ground, issue, taxiing(APRON[0]))
    read_back(engine, ground, taxiing(APRON[0]))
    holding = taxiing(50.9990)
    crossing = engine.handle(Intent("request_crossing"), ground, issue, holding)
    assert crossing.text.startswith(f"{CALLSIGN}, cross runway") and engine.flight.crossings == []
    assert said(engine, Intent("unable"), ground, holding, issue) == f"{CALLSIGN}, roger, hold position"
    assert engine.flight.crossings == ["09R/27L"]
    assert engine.tick(ground, issue, holding) is None  # not cleared again by itself
    assert said(engine, Intent("request_crossing"), ground, holding, issue).startswith(f"{CALLSIGN}, cross runway")


def test_unable_in_the_middle_of_a_climb_keeps_a_whole_thousand_feet(world):  # noqa: F811
    engine, departure, raw = climbing_with_departure(world)
    engine.handle(Intent("request_climb", altitude=24000), departure, ISSUE, raw)
    climbing = {**raw, "elevation_m": 23660 * 0.3048}
    assert said(engine, Intent("unable"), departure, climbing) == (
        f"{CALLSIGN}, roger, maintain flight level two four zero, advise when able"
    )


def test_unable_during_the_takeoff_roll_does_not_cancel_the_takeoff(world):  # noqa: F811
    engine = flight(world)
    tower = world.station("EGXX", "tower")
    engine.handle(Intent("ready_for_departure"), tower, ISSUE, at(51.0006, -0.979, 100, on_ground=True))
    rolling = {**at(51.0, -0.99, 100, on_ground=True), "groundspeed_ms": 20.0}
    assert said(engine, Intent("unable"), tower, rolling) == f"{CALLSIGN}, roger"
    assert said(engine, Intent("request_delay"), tower, rolling) == f"{CALLSIGN}, roger"
    assert engine.flight.phase == Phase.TAKEOFF_CLEARED


def test_unable_to_the_hand_off_keeps_the_flight_with_approach_and_it_is_given_again_later(world):  # noqa: F811
    clock = [0.0]
    engine, approach, raw = vectored(world)
    engine.now = lambda: clock[0]
    engine.flight.arrival.stage = "intercept"
    final = at(51.0, 5.43, 1200)
    engine.handle(Intent("established"), approach, ISSUE, final)
    assert said(engine, Intent("unable"), approach, final) == f"{CALLSIGN}, roger, remain this frequency"
    assert engine.flight.arrival.stage == "intercept"
    assert engine.tick(approach, ISSUE, final) is None
    clock[0] += 61
    assert engine.tick(approach, ISSUE, final).text == f"{CALLSIGN}, contact Eastfield Tower one one niner decimal one"


# ---- assigned items, as they stand now ------------------------------------------------------------


def test_on_final_the_level_asked_for_is_the_approach_not_an_old_altitude(world):  # noqa: F811
    engine, approach, raw = vectored(world)
    engine.flight.arrival.stage = "intercept"
    final = at(51.0, 5.43, 1200)
    engine.handle(Intent("established"), approach, ISSUE, final)
    tower = world.station("LFXX", "tower")
    asked = Intent("request_information", item="altitude")
    assert said(engine, asked, tower, final) == f"{CALLSIGN}, Eastfield Tower, cleared I L S approach runway two seven"
    assert said(engine, Intent("request_information", item="speed"), tower, final) == f"{CALLSIGN}, no speed restrictions"
    # Handed on: the tower has the flight, and approach says so to anything else.
    assert said(engine, Intent("request_descent"), approach, final) == (
        f"{CALLSIGN}, contact Eastfield Tower one one niner decimal one"
    )


def test_after_a_diversion_the_heading_it_gave_is_the_one_reported(world):  # noqa: F811
    engine, approach, raw = vectored(world)
    divert = said(engine, Intent("request_diversion", destination="EGXX"), approach, raw)
    assert "turn right heading two seven zero" in divert
    assert said(engine, Intent("say_again", item="heading"), approach, raw) == f"{CALLSIGN}, fly heading two seven zero"
    approach_now = said(engine, Intent("request_information", item="approach"), approach, raw)
    assert "expect I L S approach runway two seven" not in approach_now


def test_after_landing_no_approach_is_reported(world):  # noqa: F811
    engine, approach, raw = vectored(world)
    engine.flight.arrival.stage = "tower"
    ground = world.station("LFXX", "ground")
    taxiing_in = {**at(51.002, 5.35, 300, on_ground=True), "groundspeed_ms": 5.0}
    engine.flight.phase = Phase.TAXI_IN
    assert said(engine, Intent("request_information", item="approach"), ground, taxiing_in) == (
        f"{CALLSIGN}, Eastfield Ground, no approach assigned"
    )


def test_a_level_asked_for_while_descending_is_given_with_the_verb_from_where_the_aircraft_is(world):  # noqa: F811
    engine = flight(world)
    engine.flight.has_been_airborne, engine.flight.rules, engine.flight.cleared_ft = True, "IFR", 26000
    engine.flight.descent_given = True
    west = world.centers.on_frequency(127500, 51.0, 0.0)
    engine.flight.contacted.add(west.key)
    assert said(engine, Intent("request_climb"), west, at(51.0, 0.5, 36000, heading=90)) == (
        f"{CALLSIGN}, descend flight level two eight zero"
    )


def test_a_visual_arrival_is_given_the_pressure_by_approach(world):  # noqa: F811
    engine = flight(world, None)
    engine.flight.has_been_airborne, engine.flight.cleared_ft = True, 4500
    approach = world.station("LFXX", "approach")
    assert "Q N H one zero one three" in said(engine, Intent("check_in"), approach, at(51.0, 4.7, 4500, heading=90))


def test_far_from_an_airport_on_real_weather_its_weather_is_not_made_up_from_the_aircraft_surroundings(world):  # noqa: F811
    from proatc.answers import weather_at

    engine = flight(world)
    eastfield = world.db.airport("LFXX")
    far = {**at(51.0, 0.0, 30000), **SIM_WEATHER, "wx_source": 1}
    assert weather_at(engine, eastfield, far) is None
    assert weather_at(engine, eastfield, {**far, "wx_source": 0}) is not None


# ---- returns and frequency changes ------------------------------------------------------------


def test_request_return_at_the_destination_tower_is_not_an_approach_there(world):  # noqa: F811
    engine, approach, raw = vectored(world)
    tower = world.station("LFXX", "tower")
    back = said(engine, Intent("request_return"), tower, at(51.0, 5.6, 3000, heading=270))
    assert "continue approach" not in back and back.endswith("for diversion")


def test_a_frequency_change_is_refused_while_the_controller_is_working_the_flight(world):  # noqa: F811
    engine, approach, raw = vectored(world)
    assert said(engine, Intent("report_leaving"), approach, raw) == f"{CALLSIGN}, unable, remain this frequency"
    engine = flight(world)
    tower = world.station("EGXX", "tower")
    engine.handle(Intent("ready_for_departure"), tower, ISSUE, at(51.0006, -0.979, 100, on_ground=True))
    rolling = {**at(51.0, -0.99, 100, on_ground=True), "groundspeed_ms": 20.0}
    assert said(engine, Intent("report_leaving"), tower, rolling) == f"{CALLSIGN}, unable, remain this frequency"


def test_a_controller_left_for_a_while_stays_quiet_until_the_pilot_calls_back(world):  # noqa: F811
    engine = flight(world)
    engine.flight.has_been_airborne, engine.flight.rules, engine.flight.cleared_ft = True, "IFR", 36000
    west = world.centers.on_frequency(127500, 51.0, 0.0)
    cruising = at(51.0, 0.5, 36000, heading=90)
    engine.flight.contacted.add(west.key)
    assert said(engine, Intent("report_leaving"), west, cruising) == f"{CALLSIGN}, approved, report back on frequency"
    busting = {**at(51.0, 0.5, 37000, heading=90), "vs_fpm": 1500}
    assert engine.tick(west, None, busting) is None
    engine.handle(Intent("check_in"), west, None, cruising)
    assert engine.flight.away_from is None


def test_after_the_hand_off_approach_sends_calls_to_the_tower_and_takes_a_go_around_itself(world):  # noqa: F811
    engine, approach, raw = vectored(world)
    engine.flight.arrival.stage = "intercept"
    final = at(51.0, 5.43, 1200)
    engine.handle(Intent("established"), approach, ISSUE, final)
    read_back(engine, approach, final)
    assert said(engine, Intent("position_report", position="final"), approach, final) == (
        f"{CALLSIGN}, contact Eastfield Tower one one niner decimal one"
    )
    around = said(engine, Intent("going_around"), approach, final)
    assert around.startswith(f"{CALLSIGN}, roger, ") and "climb" in around and "contact" not in around
    assert engine.flight.arrival.stage is None and engine.flight.phase == Phase.AIRBORNE


# ---- the whole matrix, read for truth ----------------------------------------------------------


def test_the_matrix_has_no_false_answer_and_the_reviewed_cells_read_right(tmp_path):
    matrix = measure(tmp_path)
    assert matrix.false == {}, matrix.false
    text = matrix.texts
    for kind in ("approach", "tower"):
        assert "climb" not in text[("request_information:altitude", "final", kind, "IFR")]
    assert "landing clearance cancelled" not in text[("unable", "final", "approach", "IFR")]
    assert "climb" in text[("going_around", "final", "approach", "IFR")]
    assert text[("request_information:approach", "landed", "tower", "IFR")].endswith("no approach assigned")
    assert text[("request_information:approach", "taxiing in", "ground", "IFR")].endswith("no approach assigned")
    assert text[("request_information:heading", "diverting", "approach", "IFR")].endswith("fly heading two seven zero")
    for rules in ("IFR", "VFR"):
        assert text[("report_leaving", "departing", "tower", rules)].endswith("unable, remain this frequency")
    assert "cancel" not in text[("unable", "departing", "tower", "IFR")]
    assert "continue approach" not in text[("request_return", "approach, vectors", "tower", "IFR")]
    assert text[("request_descent", "climbing", "departure", "IFR")].endswith("climb flight level one three zero")
    assert text[("request_information:route", "taxiing in", "ground", "IFR")].endswith("taxi to parking")
    afis = [said for key, said in text.items() if key[2] == "afis" and said]
    instructions = ("hold position", "cancel", "cleared", "join ", "holding point")
    assert not [said for said in afis if any(word in said for word in instructions)]
    assert matrix.kinds()["false"] == 0 and matrix.kinds()["no rule"] == 0


def test_the_truth_check_catches_a_false_answer():
    """The check itself: a climb to an aircraft above the level, and a level that is not the cleared one, fail."""
    from types import SimpleNamespace

    from proatc.engine import Flight, Reply
    from tests.deadends import spoken_level, truth

    assert spoken_level("flight level two four zero".split()) == 24000
    assert spoken_level("altitude three thousand five hundred feet".split()) == 3500
    assert spoken_level("one five thousand".split()) == 15000
    engine = SimpleNamespace(flight=Flight(cleared_ft=3500))
    station = SimpleNamespace(airport=None)
    high = at(51.0, 5.43, 5000)
    assert truth(engine, station, high, None, Reply(f"{CALLSIGN}, climb altitude three thousand five hundred feet"))
    assert truth(engine, station, high, None, Reply(f"{CALLSIGN}, descend altitude four thousand feet"))
    assert not truth(engine, station, high, None, Reply(f"{CALLSIGN}, descend altitude three thousand five hundred feet"))


def test_an_aircraft_in_the_air_is_never_sent_to_a_ground_frequency(tmp_path):
    # Found replaying a real flight: climbing out at 6,000 ft, "confirm departure runway and initial clearance" was
    # taken for a clearance request, and departure answered "contact Boston Delivery ... for clearance".
    from proatc import answers
    from proatc.intents import Intent
    from tests.test_answers import build_world, climbing_with_departure

    engine, departure, raw = climbing_with_departure(build_world(tmp_path))
    said = answers.way_out(engine, Intent("request_clearance"), departure, raw, "Golf Alfa Bravo Charlie Delta", "")
    assert "Delivery" not in said.text and "Ground" not in said.text and "frequency" not in said.expect
    assert said.text.startswith("Golf Alfa Bravo Charlie Delta, unable")
