"""The runways an airport is really using, read from its real digital ATIS, and what ATC does with them.

A real flight found this: at Las Vegas, with the wind from 200 at 8 knots, the flight plan and the wind both said
runway 26R. The airport was departing 1R and 1L, with the wind behind them. The texts below are real ATIS broadcasts.
"""

from __future__ import annotations

import threading

from proatc.atis import compose, runways_in_use, split_runways
from proatc.datis import RealAtis, RealRunways, read, runways
from proatc.engine import Engine
from proatc.intents import Intent
from proatc.procedures import parse_cifp
from proatc.weather import Weather
from tests.test_runway_choice import ATIS_KHZ, DELIVERY, PLAN, clearance, issue, parked, world

LAS = (
    "LAS ATIS INFO V 1256Z. 20008KT 10SM CLR 09/M01 A2993 (TWO NINER NINER THREE). VISUAL APPROACHES IN USE. "
    "LANDING RWYS 26L AND 1L. DEPG RWYS 1R FULL LENGTH AND 1L AT BRAVO. SIMUL APCHS TO CROSSING AND PARALLEL RUNWAYS "
    "IN USE, CONVERGING RUNWAY OPERATIONS IN EFFECT. NOTICE TO AIRMEN. HAZD WX INFO AVBL ON FSS. GC COMBINED ON 121.1, "
    "HELICOPTOR CONTROL OPEN ON 119.9. HELICOPTERS OVER THE STRIP AT 3000. ...ADVS YOU HAVE INFO V."
)
LAX = (
    "LAX ATIS INFO M 1253Z. 03005KT 10SM FEW150 11/04 A3000 (THREE ZERO ZERO ZERO) RMK AO2 SLP157 T01110044. INST APCHS "
    "AND RNAV RNP APCHS ILS RY 25L, OR VCTR FOR VISUAL APCH WILL BE PROVIDED, SIMUL VISUAL APCHS TO ALL RWYS ARE IN PROG, "
    "SIMUL INSTR DEPARTURES IN PROG 25R. CTC L A GC ON 121.75 FOR PUSH OR  TAXI ON A. RWY 2 4 LEFT 6 RIGHT CLSD, "
    "RWY 2 4 RIGHT SIX LEFT CLSD. TWY B CLSD BTN TWY, C6 AND TWY C8 , , TWY B6 CLSD, TWY B5 CLSD, TWY C7 CLSD. "
    "TWR FREQ FOR ALL RWYS IS 120.95. CD AND GC FREQ IS 121.75. USE CAUTION WHEN EXITING RUNWAY TWO FOUR RIGHT AT TWY V, W, "
    "AND Y, TO HOLD SHORT OF RUNWAY TWO FOUR LEFT. MULTIPLE RUNWAY INCURSIONS HAVE OCCURRED. ...ADVS YOU HAVE INFO M."
)
SOUTH = Weather(wind_dir_true=200, wind_kt=8, qnh_hpa=1013.2)  # eight knots behind runway 1 at Testville
REAL_WEATHER = {"wx_source": 1}


# ---- reading the text -------------------------------------------------------------------------------------------


def test_las_vegas_lands_two_six_left_and_one_left_and_departs_one_right_and_one_left():
    assert read(LAS) == (["26L", "1L"], ["1R", "1L"], [])  # "at Bravo" is where 1L is entered, not a runway


def test_los_angeles_has_two_runways_closed_and_names_no_runway_from_its_cautions():
    landing, departing, closed = read(LAX)
    assert (landing, departing) == (["25L"], ["25R"])
    assert closed == ["24L", "6R", "24R", "6L"]  # "2 4 LEFT 6 RIGHT": said digit by digit, both ends


def test_a_taxiway_closed_between_two_runways_does_not_close_them():
    text = "RNAV APPROACHES IN USE. LNDG AND DEPG RWY 17R. RWY 17L CLSD. TWY BRAVO 2 BTWN RWY 17R AND RWY 17L CLSD."
    assert read(text) == (["17R"], ["17R"], ["17L"])


def test_runways_closed_in_a_list_with_commas():
    assert read("LNDG AND DEPG RWY 13R. RUNWAY 13L, 31R, 22, 4 CLOSED. RWY 13R PAPI OTS.") == (
        ["13R"], ["13R"], ["13L", "31R", "22", "4"],
    )  # fmt: skip


def test_landing_one_way_and_departing_the_other_for_noise_is_read_as_it_is_said():
    text = "ILS AND VIS APR IN USE, RY 26L & RY 26R. JETS DEPART RUNWAYS 8L AND 8R FOR NOISE ABATEMENT. RY 26L IM OTS."
    assert read(text)[:2] == (["26L", "26R"], ["8L", "8R"])


def test_what_is_out_of_service_not_authorized_or_a_distance_is_not_a_runway_in_use():
    text = (
        "ARR EXP VECTORS ILS RWY 27C APCH, VISUAL APCH RWY 28C. RWY 27C ARR PLAN TO H/S OF TWY V V, 9 THSD 7 HND 25 FT AVBL. "
        "DEPS EXP RWYS 22L 28R FROM NOVEMBER 5 9750 FT AVL. ILS RWY 10R CAT 2 NOT AUTHORIZED. RWY 9L IM OTS, RWY 9C IM OTS. "
        "DEP FREQ 125.9. RWY 17L DEPARTURES EXPECT INTERSECTION D7."
    )
    assert read(text) == (["27C", "28C"], ["22L", "28R"], [])


def test_runway_in_use_with_no_other_word_is_for_landing_and_departing():
    assert read("RUNWAY 24 IN USE, EXPECT ILS APPROACH. RWY 15/33 CLOSED.") == (["24"], ["24"], ["15", "33"])


def test_a_runway_written_against_its_word_and_a_departure_atis():
    assert read("DEPG RWY8, RWY17L.", "dep") == ([], ["8", "17L"], [])
    assert read("ILS RWY 16R APCH IN USE. DEPG RWY 8.", "arr") == (["16R"], [], [])  # an arrival ATIS: landing only


# ---- the airport's own runways ----------------------------------------------------------------------------------


def test_what_is_read_is_kept_only_where_it_is_one_of_the_airports_runways(tmp_path):
    db, _ = world(tmp_path)
    testville = db.airport("KXXX")
    found = runways(testville, [{"type": "combined", "datis": LAS, "time": "1256"}])
    assert (found.landing, found.departing, found.closed) == (("26L", "1L"), ("1R", "1L"), frozenset())
    # Another airport's ATIS: none of its runways are here, and nothing is taken from it.
    assert runways(testville, [{"type": "combined", "datis": "ILS RWY 4R APCH IN USE. DEPG RWY 4L."}]) is None
    # Both ends of one runway at once is a misread: neither is used.
    assert runways(testville, [{"type": "combined", "datis": "LANDING RWYS 26L AND 8R."}]) is None
    # "RWYS 1/8" where the airport has 1L, 1R, 8L and 8R is all four. Only approaches given: departing the same.
    family = runways(testville, [{"type": "combined", "datis": "LNDG/DEPG RWYS 1/8 EXP VISUAL APCH."}])
    assert set(family.landing) == {"1L", "1R", "8L", "8R"} and family.departing == family.landing
    # "RWYS 1 AND 8": the second number is one of the list the word began.
    pair = runways(testville, [{"type": "combined", "datis": "ILS RY 26L. SIMUL INSTR DEPARTURES IN PROG RWYS 1 AND 26."}])
    assert pair.landing == ("26L",) and set(pair.departing) == {"1L", "1R", "26L", "26R"}
    # A bare number in a list nobody introduced is a runway only if the airport has one of exactly that name.
    assert runways(testville, [{"type": "combined", "datis": "EXPECT VISUAL APCH 1L, 1R, 26."}]).landing == ("1L", "1R")
    # A closed runway is closed from both ends, and is not in use even if the text also names it.
    shut = runways(testville, [{"type": "combined", "datis": "LANDING RWYS 26L AND 26R. RWY 26R CLSD."}])
    assert shut.landing == ("26L",) and shut.closed == {"26R", "8L"}


def test_an_atis_issued_hours_ago_is_not_the_airport_now(tmp_path):
    db, _ = world(tmp_path)
    old = [{"type": "combined", "datis": LAS, "time": "0850"}]
    assert runways(db.airport("KXXX"), old, now_minutes=13 * 60) is None  # four hours old
    assert runways(db.airport("KXXX"), old, now_minutes=10 * 60) is not None  # seventy minutes


# ---- what ATC does with it --------------------------------------------------------------------------------------


def real_world(tmp_path, entries, **options):
    """The world of test_runway_choice with a real ATIS for Testville, looked up at once."""
    db, _ = world(tmp_path)
    asked = []

    def fetch(ident):
        asked.append(ident)
        return entries.get(ident, [])

    engine = Engine(db, seed=1, procedures=lambda ident: parse_cifp([]), real_atis=RealAtis(fetch, background=False), **options)
    return db, engine, asked


def test_on_real_weather_the_real_runways_come_before_the_wind_and_the_plan(tmp_path):
    db, engine, _ = real_world(tmp_path, {"KXXX": [{"type": "combined", "datis": LAS}]})
    testville = db.airport("KXXX")
    engine.set_plan(PLAN)  # the plan says 26R, and the wind allows it
    assert runways_in_use(testville, 200, 8)[-1] == "26R"  # not known yet: the plan's runway
    delivery = db.station(DELIVERY, 36.002, -115.0)
    here = {**parked(36.002, -115.0), **REAL_WEATHER}
    said = engine.handle(Intent("request_clearance", flight_rules="IFR"), delivery, issue(SOUTH), here).text
    assert ", runway one right, climb and maintain" in said or ", runway one left, climb and maintain" in said, said
    assert engine.flight.runway in ("1R", "1L")
    active = runways_in_use(testville, 200, 8)
    assert split_runways(active) == (["26L", "1L"], ["1R", "1L"])
    atis = compose(testville, SOUTH, "A", 12 * 3600)
    assert "Landing runways two six left and one left, departing runways one right and one left." in atis, atis


def test_with_weather_the_pilot_has_set_the_real_airport_is_not_asked(tmp_path):
    db, engine, _ = real_world(tmp_path, {"KXXX": [{"type": "combined", "datis": LAS}]})
    engine.set_plan(PLAN)
    said = clearance(db, engine, SOUTH)  # no real weather in the sim's state
    assert ", runway two six right, climb and maintain" in said, said
    own = {**parked(36.002, -115.0), "wx_source": 0}
    engine.active_runways(db.airport("KXXX"), issue(SOUTH), raw=own)
    assert runways_in_use(db.airport("KXXX"), 200, 8)[-1] == "26R"


def test_only_airports_that_can_have_a_digital_atis_are_asked_about_and_only_once_in_a_while(tmp_path):
    db, engine, asked = real_world(tmp_path, {"KXXX": [{"type": "combined", "datis": LAS}]})
    for _ in range(5):
        engine.active_runways(db.airport("KXXX"), issue(SOUTH), raw=REAL_WEATHER)
    assert asked == ["KXXX"]  # asked once, not at every look
    british = type("Airport", (), {"ident": "EGZZ", "runways": []})()
    assert RealAtis(lambda ident: asked.append(ident), background=False).runways(british) is None
    assert asked == ["KXXX"]  # a British airport has no digital ATIS to ask for


def test_a_runway_the_airport_is_really_using_is_given_on_request_whatever_the_wind_and_a_closed_one_is_not(tmp_path):
    from proatc.answers import acceptable_runway

    closed = LAS.replace("NOTICE TO AIRMEN.", "RWY 8R/26L CLSD.").replace("LANDING RWYS 26L AND 1L", "LANDING RWY 1L")
    db, engine, _ = real_world(tmp_path, {"KXXX": [{"type": "combined", "datis": closed}]})
    testville = db.airport("KXXX")
    assert acceptable_runway(testville, "1R", SOUTH) == "tailwind eight knots"  # before the real ATIS is known
    engine.active_runways(testville, issue(SOUTH), raw=REAL_WEATHER)
    assert acceptable_runway(testville, "1R", SOUTH) is None
    assert acceptable_runway(testville, "26L", SOUTH) == "runway closed"
    assert acceptable_runway(testville, "8R", SOUTH) == "runway closed"  # from the other end too


def test_closures_alone_keep_the_wind_and_the_plan_but_never_give_the_closed_runway(tmp_path):
    db, engine, _ = real_world(tmp_path, {"KXXX": [{"type": "combined", "datis": "NOTAMS. RWY 8L/26R CLSD."}]})
    engine.set_plan(PLAN)  # the plan's 26R is closed today
    here = {**parked(36.002, -115.0), **REAL_WEATHER}
    delivery = db.station(DELIVERY, 36.002, -115.0)
    said = engine.handle(Intent("request_clearance", flight_rules="IFR"), delivery, issue(SOUTH), here).text
    assert "two six right" not in said and engine.flight.runway != "26R", said


def test_asking_never_waits_for_the_network_and_a_failure_keeps_what_was_known(tmp_path):
    db, _ = world(tmp_path)
    testville = db.airport("KXXX")
    gate, answers = threading.Event(), [[{"type": "combined", "datis": LAS}], None]

    def slow(ident):
        gate.wait(5)
        return answers.pop(0)

    now = [0.0]
    real = RealAtis(slow, background=True, clock=lambda: now[0])
    assert real.runways(testville) is None  # asked for in the background: nothing known yet, and no wait
    gate.set()
    for _ in range(200):
        if real.runways(testville) is not None:
            break
        threading.Event().wait(0.01)
    assert real.runways(testville).departing == ("1R", "1L")
    now[0] += 700  # ten minutes on: asked again, and the service fails
    real.runways(testville)
    for _ in range(200):
        if not real._asking:
            break
        threading.Event().wait(0.01)
    assert real.runways(testville) == RealRunways(("26L", "1L"), ("1R", "1L"), frozenset())  # still known


def test_the_atis_station_itself_brings_the_real_runways_in(tmp_path):
    db, engine, _ = real_world(tmp_path, {"KXXX": [{"type": "combined", "datis": LAS}]})
    station = db.station(ATIS_KHZ, 36.002, -115.0)
    engine.real_runways(station.airport, REAL_WEATHER)  # what the ATIS broadcast does before it is put together
    assert "departing runways one right and one left" in compose(station.airport, SOUTH, "A", 12 * 3600)


# ---- a hostile or broken service --------------------------------------------------------------------------------


def test_a_crafted_text_is_read_at_once_and_does_not_freeze_the_engine(tmp_path):
    # A list of runways the text never closes was tried again every way its spaces could be split: twenty-six
    # numbers took a minute, while every thread of the engine waited.
    import time

    db, _ = world(tmp_path)
    testville = db.airport("KXXX")
    texts = ["RWY " + "1 " * 40 + "X", "RWY 1, 2 / 3 AND RY 4 " * 3000, "LANDING RWYS " + "26L AND " * 4000]
    started = time.perf_counter()
    for text in texts:
        read(text)
        runways(testville, [{"type": "combined", "datis": text}])
    assert time.perf_counter() - started < 1.5


def test_an_answer_far_larger_than_an_atis_is_not_read(monkeypatch):
    import httpx

    from proatc import datis

    class Huge:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def raise_for_status(self):
            return None

        def iter_bytes(self):
            while True:
                yield b"[" + b" " * 65536

    monkeypatch.setattr(httpx, "stream", lambda *args, **kwargs: Huge())
    assert datis.fetch("KLAS") is None


def test_of_two_runways_landing_the_centre_expects_the_plans_as_approach_will_clear_it(tmp_path):
    # Found at Miami: the real ATIS landed 27 and 30, the plan said 30. Asked for the expected approach, the centre
    # said runway 27, the first the ATIS named, while approach would have cleared the plan's 30.
    from proatc.answers import expected_approach
    from proatc.flightplan import FlightPlan

    db, engine, _ = real_world(tmp_path, {"KXXX": [{"type": "combined", "datis": LAS}]})  # lands 26L and 1L
    testville = db.airport("KXXX")
    engine.set_plan(FlightPlan("KYYY", "KXXX", [], 24000, arrival_runway="1L", source="simbrief"))
    engine.flight.has_been_airborne = True
    engine.active_runways(testville, issue(SOUTH), raw=REAL_WEATHER)
    assert expected_approach(engine, testville, issue(SOUTH), REAL_WEATHER) == "visual approach runway one left"
    # A plan's runway the airport is not landing on is not expected: the first one landing is.
    engine.set_plan(FlightPlan("KYYY", "KXXX", [], 24000, arrival_runway="1R", source="simbrief"))
    assert expected_approach(engine, testville, issue(SOUTH), REAL_WEATHER) == "visual approach runway two six left"
