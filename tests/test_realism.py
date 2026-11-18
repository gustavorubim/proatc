"""Approach variety, the missed approach and diversions, runway crossings and stands, voices,
chatter, the debrief, name hints, the local model and the view for other devices."""

import json
import random
import threading
import time

import httpx
import pytest

from proatc import chatter, voices
from proatc import config as config_module
from proatc import phraseology as say
from proatc.atis import AtisBook, Issue
from proatc.centers import CenterIndex, parse_atc
from proatc.config import Config, Llm, validate
from proatc.debrief import as_text, review
from proatc.engine import Engine
from proatc.flightplan import FlightPlan, from_simbrief
from proatc.intents import Intent, parse_json
from proatc.llm import Controller, LlmError
from proatc.navdata import AirportDB, build_index
from proatc.procedures import choose_approach, circling_approach, parse_cifp
from proatc.radio import AtisBroadcaster
from proatc.server import AppServer
from proatc.taxi import aircraft_category, assign_stand, parse_taxi, route_to_runway
from proatc.vectors import locate, next_vector
from proatc.voice_loop import hearing_hints, known_names
from proatc.weather import Cloud, Weather
from tests.test_ifr import APT, ATC, CALLSIGN, CIFP_WEST, ISSUE, PLAN, at
from tests.test_next import with_traffic
from tests.test_radio_loop import FakeBridge, FakePlayer, FakeTts

# Eastfield with a published missed approach on the ILS, an RNAV to the same runway and a VOR to the other end.
LEG = ", ,   ,CF, , , , , ,      ,    ,    ,    ,    , ,{alt},     ,     , ,   ,    ,   , , , , , ,0,D,S;"
CIFP_EAST = [
    "APPCH:010,I,I27, ,CF27,LF,P,C,E   " + LEG.format(alt="02500"),
    "APPCH:020,I,I27, ,RW27,LF,P,G,G   " + LEG.format(alt="00350"),
    "APPCH:030,I,I27, ,MISSD,LF,E,A,E   " + LEG.format(alt="04000"),
    "APPCH:010,R,R27, ,RW27,LF,P,G,G   " + LEG.format(alt="00350"),
    "APPCH:010,D,D09, ,RW09,LF,P,G,G   " + LEG.format(alt="00350"),
]
ONLY_09 = [CIFP_EAST[-1]]
MURK = Issue("D", Weather(wind_dir_true=270, wind_kt=10, visibility_sm=2, clouds=[Cloud(0.8, 600)], qnh_hpa=1009), 12 * 3600)
EASTERLY = Issue("D", Weather(wind_dir_true=90, wind_kt=12, qnh_hpa=1013.2), 13 * 3600)
THRESHOLD_27 = (51.0, 5.37)


def make(tmp_path, east=CIFP_EAST, plan=PLAN, **options):
    apt = tmp_path / "apt.dat"
    if not (tmp_path / "a.sqlite").exists():  # a second world in one test shares the index
        apt.write_text("\n".join(APT), encoding="utf-8")
        build_index(apt, tmp_path / "a.sqlite")
    db = AirportDB(tmp_path / "a.sqlite")
    procedures = {"EGXX": parse_cifp(CIFP_WEST), "LFXX": parse_cifp(east)}
    engine = Engine(
        db, seed=1, procedures=lambda ident: procedures.get(ident, parse_cifp([])), centers=CenterIndex(parse_atc(ATC)), **options
    )
    engine.set_plan(plan)
    engine.flight.has_been_airborne, engine.flight.origin_ident, engine.flight.cleared_ft = True, "EGXX", 9000
    return db, engine, db.station(124300, 51.0, 5.0), db.station(119100, 51.0, 5.3)


# ---- vectors ----------------------------------------------------------------


def test_an_aircraft_already_out_on_the_approach_side_closes_on_a_dogleg():
    course = 270.0
    out_east = locate(51.1, 5.9, THRESHOLD_27, course)  # 20 miles out along the approach, six north of it
    assert next_vector(out_east, course, 2500, 6000, inbound=True).heading_true == 225.0
    assert next_vector(out_east, course, 2500, 6000).heading_true == 180.0  # after a downwind leg: a square base turn
    # A shorter final for this arrival moves the base turn in. Base is turned about four miles beyond where final is
    # joined, so that the 30 degree intercept from three and a half miles off meets final in time.
    abeam = locate(51.1, 5.73, THRESHOLD_27, course)  # thirteen and a half miles out
    assert next_vector(abeam, course, 2500, 4000, final_nm=10).stage == "downwind"
    assert next_vector(abeam, course, 2500, 4000, final_nm=9).stage == "base"
    assert next_vector(abeam, course, 2500, 4000, final_nm=9, extend=True).stage == "downwind"


def test_first_vector_for_an_arrival_from_the_approach_side_is_the_dogleg(tmp_path):
    _, engine, approach, _ = make(tmp_path)
    first = engine.handle(Intent("check_in"), approach, ISSUE, at(51.1, 5.9, feet=6000, heading=270))
    assert "heading two two five" in first.text and "expect I L S approach runway two seven" in first.text
    assert 9 <= engine.flight.arrival.final_nm <= 12


def test_downwind_is_extended_for_traffic_on_final(tmp_path):
    _, engine, approach, _ = make(tmp_path)
    engine.handle(Intent("check_in"), approach, ISSUE, at(51.1, 5.0, feet=7000, heading=90))
    assert engine.flight.arrival.stage == "downwind"
    engine.flight.pending = None  # read back: the next instruction waits for it
    turning_point = at(51.1, 5.75, feet=3500, heading=90)
    other_on_final = (51.0, 5.53, 2200.0, 270.0, False)  # six miles out on the I L S
    held = engine.tick(approach, ISSUE, with_traffic(turning_point, other_on_final))
    assert held.text == f"{CALLSIGN}, extending your downwind for traffic on final, I will call your base turn"
    assert engine.tick(approach, ISSUE, with_traffic(turning_point, other_on_final)) is None  # said once
    base = engine.tick(approach, ISSUE, with_traffic(turning_point))
    assert "heading one eight zero" in base.text and engine.flight.arrival.stage == "base"


# ---- approach types ---------------------------------------------------------


def test_missed_approach_and_approach_families_are_read_from_the_procedures():
    east = parse_cifp(CIFP_EAST)
    ils = east.approaches["I27"]
    assert (ils.platform_ft, ils.missed_ft, ils.missed_fix) == (2500, 4000, "MISSD")
    assert choose_approach(east, "27").ident == "I27" and choose_approach(east, "27", "RNAV").ident == "R27"
    assert choose_approach(east, "27", "NDB") is None
    assert circling_approach(parse_cifp(ONLY_09), ["09"]).ident == "D09"


def test_the_pilot_can_ask_for_another_type_of_approach(tmp_path):
    _, engine, approach, _ = make(tmp_path)
    here = at(51.1, 4.3, feet=9000, heading=90)
    engine.handle(Intent("check_in"), approach, ISSUE, here)
    assert engine.handle(Intent("request_approach", approach="NDB"), approach, ISSUE, here).text == (
        f"{CALLSIGN}, unable, no N D B approach runway two seven, expect I L S approach runway two seven"
    )
    given = engine.handle(Intent("request_approach", approach="RNAV"), approach, ISSUE, here)
    assert given.text == f"{CALLSIGN}, roger, expect R NAV approach runway two seven"
    labels = [item["label"] for item in engine.suggestions(approach, ISSUE, here)]
    assert "Request ILS approach" in labels  # and back again
    assert parse_json('{"intent": "request_approach", "approach": "r nav"}').approach == "RNAV"
    assert parse_json('{"intent": "request_approach", "approach": "localizer"}').approach == "LOC"


def test_field_in_sight_gets_a_visual_approach_when_the_weather_allows(tmp_path):
    _, engine, approach, _ = make(tmp_path)
    close = at(51.1, 5.0, feet=5000, heading=90)
    engine.handle(Intent("check_in"), approach, ISSUE, close)
    assert engine.handle(Intent("request_visual"), approach, MURK, close).text == (
        f"{CALLSIGN}, unable visual approach, expect I L S approach runway two seven"
    )
    cleared = engine.handle(Intent("request_visual"), approach, ISSUE, close)
    assert cleared.text == (
        f"{CALLSIGN}, cleared visual approach runway two seven, contact Eastfield Tower one one niner decimal one"
    )
    assert cleared.expect == {"runway": "27", "frequency": "119.100"}
    assert engine.flight.arrival.stage == "tower" and engine.summary()["approach"] == "visual runway 27"


def test_a_runway_without_an_approach_is_reached_by_circling(tmp_path):
    _, engine, approach, _ = make(tmp_path, east=ONLY_09)
    first = engine.handle(Intent("check_in"), approach, ISSUE, at(51.1, 5.0, feet=7000, heading=90))
    assert "expect V O R D M E approach runway zero niner, circle to land runway two seven" in first.text
    arrival = engine.flight.arrival
    assert (arrival.runway, arrival.flown, engine.flight.runway) == ("27", "09", "27")
    # In weather too poor to circle, the approach is not offered: the old behaviour, a visual to the runway in use.
    _, low, approach, _ = make(tmp_path, east=ONLY_09)
    assert "circle" not in low.handle(Intent("check_in"), approach, MURK, at(51.1, 5.0, feet=7000, heading=90)).text


def test_a_change_of_runway_in_use_replans_an_arrival_not_yet_on_final(tmp_path):
    _, engine, approach, _ = make(tmp_path)
    here = at(51.1, 5.0, feet=7000, heading=90)
    engine.handle(Intent("check_in"), approach, ISSUE, here)
    engine.flight.pending = None
    changed = engine.tick(approach, EASTERLY, here)
    assert changed.text == f"{CALLSIGN}, runway change, expect V O R D M E approach runway zero niner"
    assert changed.expect == {"runway": "09"} and engine.flight.arrival.runway == "09"
    engine.flight.pending = None
    vector = engine.tick(approach, EASTERLY, here)
    assert vector is not None and "heading" in vector.text  # new vectors, for the other end


# ---- missed approach and diversion -------------------------------------------


def test_go_around_flies_the_published_missed_approach_and_can_end_in_a_diversion(tmp_path):
    plan = FlightPlan("EGXX", "LFXX", PLAN.route, 36000, alternate="EGXX")
    db, engine, approach, tower = make(tmp_path, plan=plan)
    engine.handle(Intent("check_in"), approach, ISSUE, at(51.1, 5.0, feet=7000, heading=90))
    short_final = at(51.0, 5.42, feet=900, heading=270)
    around = engine.handle(Intent("going_around"), tower, ISSUE, short_final)
    assert around.text == (
        f"{CALLSIGN}, roger, standard missed approach, climb altitude four thousand feet, "
        "contact Eastfield Approach one two four decimal three"
    )
    assert around.expect == {"altitude": 4000, "frequency": "124.300"} and engine.flight.go_arounds == 1

    climbing = at(51.0, 5.2, feet=3000, heading=270)
    engine.flight.pending = None
    labels = [item["label"] for item in engine.suggestions(approach, ISSUE, climbing)]
    assert "Divert to EGXX" in labels
    diverted = engine.handle(Intent("request_diversion", destination="EGXX"), approach, ISSUE, climbing)
    assert diverted.text == (
        f"{CALLSIGN}, roger, cleared to Westfield, fly heading two seven zero, climb flight level two four zero"
    ) or diverted.text == (
        f"{CALLSIGN}, roger, cleared to Westfield, turn left heading two seven zero, climb flight level two four zero"
    )
    flight = engine.flight
    assert (flight.plan.destination, flight.plan.origin, flight.arrival, flight.diverting) == ("EGXX", "LFXX", None, True)
    assert flight.cleared_ft == 24000 and flight.destination == "Westfield"
    # The approach controller is now a departure controller to this flight, and hands it to the centre.
    engine.flight.pending = None
    handoff = engine.tick(approach, ISSUE, at(51.0, 4.4, feet=23500, heading=270))
    assert handoff.text.startswith(f"{CALLSIGN}, contact ")


def test_a_diversion_without_a_known_airport_asks_for_one(tmp_path):
    _, engine, approach, _ = make(tmp_path)
    reply = engine.handle(Intent("request_diversion", destination="somewhere nice"), approach, ISSUE, at(51.0, 5.2, feet=3000))
    assert reply.text.endswith("say the four letter code of the airport you are diverting to")
    assert (
        from_simbrief(
            {"origin": {"icao_code": "egll"}, "destination": {"icao_code": "egph"}, "alternate": {"icao_code": "egpf"}}
        ).alternate
        == "EGPF"
    )


# ---- the ground: crossings, progressive taxi, stands --------------------------

# Two parallel runways; the apron is south of both, so a departure from the northern one crosses the southern one.
CROSSING = """A
1200 test

1    100 0 0 EGXX Testfield
1302 icao_code EGXX
100 45.00 1 1 0.25 1 3 0  09R 51.0000 -1.0200 0 0 3 2 1 0 27L 51.0000 -0.9800 0 0 3 2 1 0
100 45.00 1 1 0.25 1 3 0  09L 51.0040 -1.0200 0 0 3 2 1 0 27R 51.0040 -0.9800 0 0 3 2 1 0
1201 50.9960 -1.0000 both 0 apron
1201 50.9990 -1.0000 both 1 hold_south
1201 51.0000 -1.0000 both 2 on_27L
1201 51.0020 -1.0000 both 3 between
1201 51.0020 -0.9800 both 4 between_east
1201 51.0030 -0.9800 both 5 hold_27R
1201 51.0040 -0.9800 both 6 on_27R
1201 51.0000 -0.9800 both 7 end_27L
1201 51.0000 -1.0200 both 8 end_09R
1201 51.0040 -1.0200 both 9 end_09L
1202 0 1 twoway taxiway_E C
1202 1 2 twoway taxiway_E C
1202 2 3 twoway taxiway_E C
1202 3 4 twoway taxiway_E B
1202 4 5 twoway taxiway_E B1
1202 5 6 twoway taxiway_E B1
1202 8 2 twoway runway 09R/27L
1202 2 7 twoway runway 09R/27L
1202 9 6 twoway runway 09L/27R
1300 50.9960 -1.0000 0.0 gate heavy|jets 5
1301 E airline baw
1300 50.9961 -1.0010 0.0 gate heavy|jets 7
1301 E airline dlh
1300 50.9962 -1.0020 0.0 tie_down props GA1
1301 A general_aviation
1053 121700 Testfield Ground
1054 118100 Testfield Tower
""".splitlines()
APRON = (50.9960, -1.0000)


def ground_world(tmp_path, **options):
    apt = tmp_path / "crossing.dat"
    if not (tmp_path / "crossing.sqlite").exists():
        apt.write_text("\n".join(CROSSING), encoding="utf-8")
        build_index(apt, tmp_path / "crossing.sqlite")
    db = AirportDB(tmp_path / "crossing.sqlite")
    return db, Engine(db, seed=1, procedures=lambda ident: parse_cifp([]), **options), db.station(121700, *APRON)


def taxiing(lat, lon=-1.0, speed=0.0):
    return {"tail_number": "G-ABCD", "lat": lat, "lon": lon, "on_ground": 1, "agl_m": 0.0, "groundspeed_ms": speed,
            "elevation_m": 30.0, "heading_true": 0.0}  # fmt: skip


def test_taxi_route_knows_its_turns_and_stands_know_who_uses_them(tmp_path):
    db, _, _ = ground_world(tmp_path)
    net = parse_taxi(db.record_lines(db.airport("EGXX")))
    route = route_to_runway(net, *APRON, "27R", (51.004, -0.98))
    assert (route.taxiways, route.crossings, route.turns) == (["C", "B", "B1"], ["09R/27L"], ["right", "left"])
    rng = random.Random(1)
    assert assign_stand(net, "jets", "BAW", APRON, rng).name == "5"  # the airline's own stand
    assert assign_stand(net, "jets", "DLH", APRON, rng).name == "7"
    assert assign_stand(net, "props", None, APRON, rng).name == "GA1"
    assert assign_stand(net, "helos", None, APRON, rng) is None
    assert aircraft_category("A20N") == "jets" and aircraft_category("B77W") == "heavy"
    assert aircraft_category("C172") == "props" and aircraft_category("", airline=True) == "jets"


def test_a_runway_on_the_taxi_route_is_held_short_of_until_cleared_to_cross(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    parked = taxiing(*APRON)
    taxi = engine.handle(Intent("request_taxi"), ground, ISSUE, parked)
    assert taxi.text == (
        f"{CALLSIGN}, Testfield Ground, taxi to holding point runway two seven right via Charlie, Bravo and Bravo one, "
        "hold short runway zero niner right, Q N H one zero one three"
    )
    assert taxi.expect == {"hold_short": "09R/27L"} and engine.summary()["hold_short"] == "09R"
    # A readback naming either runway is not taken for a wrong one.
    assert engine.handle(Intent("readback", runway="09R"), ground, ISSUE, parked).text == ""
    labels = [item["label"] for item in engine.suggestions(ground, ISSUE, parked)]
    assert "Holding short" in labels and "Progressive taxi" in labels

    cross = engine.handle(Intent("request_crossing"), ground, ISSUE, taxiing(50.999))
    assert cross.text == f"{CALLSIGN}, cross runway zero niner right, continue to holding point runway two seven right"
    assert engine.flight.crossings == [] and "hold_short" not in engine.summary()


def test_ground_clears_the_crossing_unasked_and_speaks_up_about_an_incursion(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    engine.handle(Intent("request_taxi"), ground, ISSUE, taxiing(*APRON))
    engine.flight.pending = None
    assert engine.tick(ground, ISSUE, taxiing(50.998, speed=6.0)) is None  # still rolling toward the runway
    stopped = engine.tick(ground, ISSUE, taxiing(50.9990))
    assert stopped.text == f"{CALLSIGN}, cross runway zero niner right, continue to holding point runway two seven right"

    _, engine, ground = ground_world(tmp_path)
    engine.handle(Intent("request_taxi"), ground, ISSUE, taxiing(*APRON))
    told = engine.tick(ground, ISSUE, taxiing(51.0000, speed=6.0))
    assert told.text == f"{CALLSIGN}, you were to hold short of runway zero niner right, expedite crossing"
    assert engine.flight.crossings == [] and engine.tick(ground, ISSUE, taxiing(51.001, speed=6.0)) is None


def test_a_crossing_waits_for_landing_traffic(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    engine.handle(Intent("request_taxi"), ground, ISSUE, taxiing(*APRON))
    engine.flight.pending = None
    short = taxiing(50.9990)
    landing = (51.0, -0.94, 600.0, 270.0, False)  # a mile and a half out for 27L
    assert engine.tick(ground, ISSUE, with_traffic(short, landing)) is None
    held = engine.handle(Intent("request_crossing"), ground, ISSUE, with_traffic(short, landing))
    assert held.text == f"{CALLSIGN}, hold short runway zero niner right, landing traffic"
    assert engine.handle(Intent("request_crossing"), ground, ISSUE, with_traffic(short)).text.startswith(
        f"{CALLSIGN}, cross runway zero niner right"
    )


def test_progressive_taxi_gives_the_next_turn(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    engine.handle(Intent("request_taxi"), ground, ISSUE, taxiing(*APRON))
    lost = engine.handle(Intent("request_progressive"), ground, ISSUE, taxiing(50.9970))
    assert lost.text == f"{CALLSIGN}, continue on Charlie, then turn right onto Bravo, hold short runway zero niner right"
    engine.handle(Intent("request_crossing"), ground, ISSUE, taxiing(50.999))
    after = engine.handle(Intent("request_progressive"), ground, ISSUE, taxiing(51.002, -0.99))
    assert after.text == f"{CALLSIGN}, continue on Bravo, then turn left onto Bravo one"
    assert parse_json('{"intent": "request_progressive"}').intent == "request_progressive"


def test_an_arrival_is_given_a_stand_that_fits_and_keeps_it(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    engine.set_plan(FlightPlan("LFXX", "EGXX", callsign="BAW123"))
    engine.flight.has_been_airborne = True
    rolled_out = {**taxiing(51.004, -1.0), "aircraft_type": "A20N"}
    engine._observe({**rolled_out, "on_ground": 0, "agl_m": 300.0})
    engine._observe(rolled_out)
    first = engine.handle(Intent("runway_vacated"), ground, ISSUE, rolled_out)
    assert "taxi to stand five" in first.text and engine.summary()["stand"] == "5"
    again = engine.handle(Intent("request_taxi"), ground, ISSUE, rolled_out)
    assert "taxi to stand five" in again.text


# ---- voices and chatter -------------------------------------------------------

VOICES = ["af_sarah", "af_bella", "am_adam", "am_michael", "bf_emma", "bm_george", "bm_lewis"]


def test_every_controller_keeps_one_voice_and_the_accent_follows_the_country():
    tower = voices.voice_for("EGLL:tower", "EGLL", VOICES, ("af_sarah", "am_adam"))
    assert tower == voices.voice_for("EGLL:tower", "EGLL", VOICES, ("af_sarah", "am_adam"))
    assert tower[0][0] == "b" and voices.language(tower[0]) == "en-gb"
    assert voices.voice_for("KJFK:tower", "KJFK", VOICES, ("af_sarah", "am_adam"))[0] in ("af_bella", "am_michael")
    british = {voices.voice_for(f"EGLL:{kind}", "EGLL", VOICES)[0] for kind in ("delivery", "ground", "tower", "approach", "x")}
    assert len(british) > 1  # not everyone at one airport sounds the same
    assert voices.voice_for("LFPG:tower", "LFPG", VOICES)[0] in VOICES and voices.voice_for("X", "EGLL", []) is None
    assert voices.language("am_adam") == "en-us" and voices.accent("YSSY") == "b" and voices.accent("LFPG") is None


def test_chatter_is_built_from_the_station_and_never_uses_your_callsign(tmp_path):
    db, engine, approach, tower = make(tmp_path)
    rng = random.Random(3)
    for station in (
        approach,
        tower,
        db.station(121700, 51.0, -1.0),
        db.station(121900, 51.0, -1.0),
        db.station(125500, 51.0, -1.0),
    ):
        for _ in range(12):
            callsign, said, answered = chatter.exchange(station, ISSUE, engine, rng, "Speedbird one two three")
            assert callsign != "Speedbird one two three" and said.startswith(f"{callsign}, ") and answered.endswith(callsign)
    said = {chatter.exchange(tower, ISSUE, engine, rng)[1].split(", ", 1)[1] for _ in range(40)}
    assert any("runway two seven, cleared to land" in line for line in said)  # the real runway in use
    centre = engine.centers.station_for(engine.centers.at(51.0, 0.0, 30000))
    assert "flight level" in " ".join(chatter.exchange(centre, None, engine, rng)[1] for _ in range(12))
    assert 20 <= chatter.gap_seconds("tower", "some", rng) <= 80 and chatter.gap_seconds("tower", "busy", rng) <= 40


def test_chatter_plays_on_an_idle_frequency_and_keeps_out_of_the_way(tmp_path):
    db, engine, _, tower = make(tmp_path)
    state = {**at(51.0, 5.3, feet=3000), "com1_khz": 119100, "zulu_seconds": 43200.0, "wx_qnh_pas": 101300.0}
    events, tts, player = [], FakeTts(), FakePlayer()
    caster = AtisBroadcaster(FakeBridge(state), db, tts, player, AtisBook(), Config(), threading.Event(), engine, events.append)
    caster._chatter(tower, state)  # first sight of the frequency: nothing yet, a start is scheduled
    assert player.played == 0 and caster._chatter_key == tower.key
    caster._chatter_at = 0.0
    caster._chatter(tower, state)
    assert player.played == 1 and len(tts.spoken) == 2 and events[-1]["type"] == "chatter"
    assert events[-1]["said"] == tts.spoken[0] and caster._chatter_at > 0

    caster._chatter_at = caster._chatter_until = 0.0
    engine.flight.pending, engine.flight.pending_key = object(), tower.key  # the controller waits for a readback
    caster._chatter(tower, state)
    assert player.played == 1
    engine.flight.pending, caster._chatter_at = None, 0.0
    caster.config.atc.chatter = "off"
    caster._chatter(tower, state)
    assert player.played == 1
    # Switched off in the settings while an exchange is playing: it stops at once.
    stopped = []
    player.stop = lambda: stopped.append(True)
    caster._chatter_until = time.monotonic() + 30
    caster._chatter(tower, state)
    assert stopped == [True] and caster._chatter_until == 0.0
    caster._chatter(tower, state)
    assert stopped == [True]


# ---- debrief ------------------------------------------------------------------


def test_debrief_counts_what_went_wrong_on_the_radio(tmp_path):
    def atc(t, text, unprompted=False):
        return {"t": t, "type": "atc", "station": "Eastfield Approach", "text": text, "unprompted": unprompted}

    records = [
        {"t": 1000.0, "type": "pilot", "text": "check in", "source": "voice"},
        atc(1005.0, "Golf, identified, descend flight level one one zero"),
        {"t": 1010.0, "type": "pilot", "text": "descend one two zero", "source": "voice"},
        {"t": 1011.0, "type": "understood", "intent": "readback"},
        atc(1012.0, "Golf, negative, descend flight level one one zero"),
        {"t": 1020.0, "type": "pilot", "text": "descend one one zero", "source": "clicked"},
        {"t": 1021.0, "type": "understood", "intent": "readback"},
        atc(1300.0, "Golf, check your level, cleared flight level one one zero", True),
        atc(1400.0, "Golf, fly heading zero niner zero", True),
        atc(1500.0, "Golf, turn right heading one eight zero, descend altitude two thousand five hundred feet", True),
        {"t": 1600.0, "type": "info", "text": "nobody answers: no station in range on 121.500"},
    ]
    result = review(records)
    assert [f["kind"] for f in result["findings"]] == ["readback", "level", "heading", "frequency"]
    assert result["readbacks"] == {"given": 2, "corrected": 1, "missed": 0} and result["by_source"] == {"voice": 2, "clicked": 1}
    assert result["score"] == 100 - 6 - 15 - 5 - 2 and result["minutes"] == 8 and result["stations"] == ["Eastfield Approach"]
    assert "serious" in result["verdict"] and "Level bust" in as_text(result)
    clean = review(records[:2])
    assert clean["score"] == 100 and clean["findings"] == [] and clean["verdict"].startswith("Clean")
    assert review([])["score"] is None


def test_debrief_is_emitted_and_saved_with_the_log(tmp_path, monkeypatch):
    from proatc.voice_loop import Session

    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr("proatc.voice_loop.PROJECT_DIR", tmp_path)
    events = []
    session = Session(Config(), emit=events.append)
    session.emit({"type": "pilot", "text": "radio check", "source": "typed"})
    session.emit({"type": "atc", "station": "Tower", "text": "G, I say again, squawk one two three four", "unprompted": True})
    result = session.debrief()
    assert result["readbacks"]["missed"] == 1 and events[-1]["type"] == "debrief" and "notes" not in result
    session.save_log()
    saved = events[-1]["path"]
    assert "Instruction not read back" in open(saved, encoding="utf-8").read()
    assert json.loads(open(saved.replace(".txt", ".json"), encoding="utf-8").read())["debrief"]["score"] == 96
    assert all(entry["type"] != "debrief" for entry in session.log.records)


# ---- names the recogniser should expect ---------------------------------------


def test_the_flights_own_names_are_given_to_speech_recognition_and_the_parser(tmp_path):
    _, engine, approach, _ = make(tmp_path)
    engine.flight.direct_to = "BRAVO"
    assert known_names(engine) == ["EGXX", "LFXX", "ALFAX", "BRAVO", "DELTA"]
    hints = hearing_hints(engine, CALLSIGN, approach)
    assert hints == f"{CALLSIGN}. Eastfield Approach. Eastfield. Waypoints: Egxx, Lfxx, Alfax, Bravo, Delta."

    class Recorder:
        def complete(self, system, user, **kwargs):
            self.user = user
            return '{"intent": "request_direct", "fix": "BRAVO"}'

    from proatc.intents import IntentParser

    recorder = Recorder()
    intent = IntentParser(recorder).parse("request direct bravo", "", known_names(engine))
    assert intent.fix == "BRAVO" and recorder.user.startswith(
        "Waypoints and airports of this flight: EGXX LFXX ALFAX BRAVO DELTA\n"
    )


# ---- the local model ----------------------------------------------------------


def test_a_local_model_needs_no_key_and_costs_nothing(monkeypatch):
    sent = {}

    def post(self, url, json=None):
        sent.update(url=url, body=json, headers=dict(self.headers))
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"intent": "radio_check"}'}}]})

    monkeypatch.setattr(httpx.Client, "post", post)
    settings = Llm(provider="local", local_model="qwen2.5:7b", local_url="http://localhost:11434/v1/chat/completions")
    controller = Controller(settings, None)
    assert controller.complete("system", "user") == '{"intent": "radio_check"}'
    assert sent["url"] == settings.local_url and sent["body"]["model"] == "qwen2.5:7b"
    assert "models" not in sent["body"] and "reasoning" not in sent["body"] and "authorization" not in sent["headers"]
    assert controller.usage["calls"] == 1 and controller.usage["cost"] == 0.0

    def refused(self, url, json=None):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.Client, "post", refused)
    with pytest.raises(LlmError, match="local model could not be reached"):
        controller.complete("system", "user")


def test_new_settings_are_checked(tmp_path, monkeypatch):
    from proatc.voice_loop import Session

    good = {"llm": {"provider": "local", "local_url": "http://box:1234/v1/chat/completions", "local_model": "phi4"},
            "atc": {"chatter": "busy"}, "tts": {"vary_voices": False}, "remote": {"enabled": True}}  # fmt: skip
    assert validate(good) == good
    for bad in ({"llm": {"provider": "cloud"}}, {"llm": {"local_url": "ftp://x"}}, {"atc": {"chatter": "loud"}},
                {"remote": {"port": 80}}, {"remote": {"enabled": "yes"}}):  # fmt: skip
        with pytest.raises(ValueError):
            validate(bad)
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(config_module, "openrouter_key", lambda: None)
    monkeypatch.setattr("proatc.voice_loop.openrouter_key", lambda: None)
    session = Session(Config(), emit=lambda event: None)
    session.set_key(None)
    assert session.controller is None  # no key: echo mode
    session.apply_settings({"llm": {"provider": "local"}})
    assert session.controller is not None and session.controller.local and session.parser is not None


# ---- the view for other devices -----------------------------------------------


def test_another_device_gets_the_page_and_pairs_with_the_code(tmp_path, monkeypatch):
    from websockets.exceptions import ConnectionClosed
    from websockets.sync.client import connect

    from proatc import server

    web = tmp_path / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text("<!doctype html><title>Pro ATC</title>", encoding="utf-8")
    (web / "assets" / "app.js").write_text("console.log('hi')", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("not for the network", encoding="utf-8")
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr(server, "PROJECT_DIR", tmp_path)
    config = Config()
    probe = __import__("socket").socket()
    probe.bind(("127.0.0.1", 0))
    config.remote.port = probe.getsockname()[1]
    probe.close()
    app = AppServer(config, 0, "app-token")

    monkeypatch.delenv("PROATC_WEB_DIR", raising=False)
    with pytest.raises(ValueError, match="needs the desktop app"):
        app.set_remote(True)
    monkeypatch.setenv("PROATC_WEB_DIR", str(web))
    app.session.apply_settings({"remote": {"enabled": True}})
    app.set_remote(True)
    try:
        base = f"http://127.0.0.1:{config.remote.port}"
        page = httpx.get(base + "/")
        assert page.status_code == 200 and "Pro ATC" in page.text and page.headers["content-type"].startswith("text/html")
        assert httpx.get(base + "/assets/app.js").headers["content-type"].startswith("text/javascript")
        assert httpx.get(base + "/../secret.txt").status_code == 404 and httpx.get(base + "/%2e%2e/secret.txt").status_code == 404
        assert httpx.get(base + "/nothing.css").status_code == 404

        info = app.remote_info()
        code = info["code"]
        assert info["running"] and len(code) == 8 and info["url"].endswith(f":{config.remote.port}")
        assert server.pairing_code() == code  # the same code next time

        app.backlog.append({"type": "info", "text": "hello phone"})
        address = f"ws://127.0.0.1:{config.remote.port}/"
        with connect(f"{address}?token={code.lower()}") as phone:
            assert json.loads(phone.recv(timeout=5)) == {"type": "info", "text": "hello phone"}
            # The key and the model are the computer's to change.
            phone.send(json.dumps({"cmd": "set_key", "key": "sk-or-stolen"}))
            assert "only be changed in the app" in json.loads(phone.recv(timeout=5))["text"]
            phone.send(json.dumps({"cmd": "settings", "changes": {"llm": {"provider": "local"}, "atc": {"chatter": "off"}}}))
            phone.send(json.dumps({"cmd": "settings", "changes": {"atc": {"holding": "always"}}}))
        for _ in range(50):
            if app.session.config.atc.holding == "always":
                break
            threading.Event().wait(0.05)
        assert app.session.config.atc.chatter == "off" and app.session.config.llm.provider == "openrouter"
        assert not (tmp_path / ".env").exists()

        for wrong in ("WRONGCOD", "app-token", ""):  # the app's own token is not accepted from outside either
            with pytest.raises(ConnectionClosed):
                with connect(f"{address}?token={wrong}") as stranger:
                    stranger.recv(timeout=5)
        # After enough wrong codes, even the right one is refused for a while.
        for _ in range(server.MAX_BAD_CODES):
            assert not app._allowed("NOPE", remote=True)
        assert not app._allowed(code, remote=True) and app._allowed("app-token", remote=False)
    finally:
        app.set_remote(False)
    assert app.remote is None and not app.remote_info()["running"]


def test_spoken_helpers_used_by_the_new_calls():
    assert say.runway("09R") == "zero niner right" and say.taxiway("B1") == "Bravo one"


def test_a_runway_the_airport_calls_9r_is_found_in_a_taxi_network_that_calls_it_09r(tmp_path):
    # Found machine-flying gate to gate: at Boston, Dulles and JFK the airport calls a runway "4R" or "1C" while
    # its taxi network says "04R/22L", and no taxi route was ever found to it.
    from proatc.taxi import use_airport_runway_names

    american = [line.replace(" 09R ", " 9R ").replace(" 09L ", " 9L ") if line.startswith("100 ") else line for line in CROSSING]
    apt = tmp_path / "american.dat"
    apt.write_text("\n".join(american), encoding="utf-8")
    build_index(apt, tmp_path / "american.sqlite")
    db = AirportDB(tmp_path / "american.sqlite")
    airport = db.airport("EGXX")
    assert [runway.end1 for runway in airport.runways] == ["9R", "9L"]
    as_written = parse_taxi(db.record_lines(airport))
    assert sorted(as_written.runway_nodes) == ["09L/27R", "09R/27L"]
    assert route_to_runway(as_written, *APRON, "9L", (51.004, -1.02)) is None  # as it was

    engine = Engine(db, seed=1, procedures=lambda ident: parse_cifp([]))
    net = engine.network(airport)
    assert sorted(net.runway_nodes) == ["9L/27R", "9R/27L"]
    assert route_to_runway(net, *APRON, "9L", (51.004, -1.02)) is not None
    to_27r = route_to_runway(net, *APRON, "27R", (51.004, -0.98))
    assert to_27r.crossings == ["9R/27L"]  # the runway crossed on the way is still known as one
    assert use_airport_runway_names(as_written, ["09L", "27R"]) is as_written  # nothing to rename: untouched


def test_a_runway_read_back_as_the_model_writes_it_is_the_runway_the_airport_calls_9r(tmp_path):
    # The language model writes "09R"; an airport in the United States writes "9R". A correct voice readback of
    # "runway niner right" was answered "negative" because the two were compared as text.
    american = [line.replace(" 09R ", " 9R ").replace(" 09L ", " 9L ") if line.startswith("100 ") else line for line in CROSSING]
    apt = tmp_path / "american.dat"
    apt.write_text("\n".join(american), encoding="utf-8")
    build_index(apt, tmp_path / "american.sqlite")
    db = AirportDB(tmp_path / "american.sqlite")
    engine = Engine(db, seed=1, procedures=lambda ident: parse_cifp([]))
    ground = db.station(121700, *APRON)
    taxi = engine.handle(Intent("request_taxi"), ground, ISSUE, taxiing(*APRON))
    assert taxi.expect == {"hold_short": "9R/27L"}
    assert engine.handle(Intent("readback", runway="09R"), ground, ISSUE, taxiing(*APRON)).text == ""  # accepted
    engine.handle(Intent("request_crossing"), ground, ISSUE, taxiing(50.999))
    wrong = engine.handle(Intent("readback", runway="09L"), ground, ISSUE, taxiing(50.999))
    assert wrong.text.startswith(f"{CALLSIGN}, negative")  # a wrong runway is still wrong
