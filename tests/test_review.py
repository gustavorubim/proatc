"""Defects found by an independent review of 0.5.0, each reproduced here so that it stays fixed.

The probes are the reviewer's: an approach clearance with no approach and no visual weather, a descent
below the terrain, a runway occupied after the landing clearance, readbacks that leave everything out,
a free-form reply that instructs, buttons that needed a language model, and a debrief that blamed the
pilot for a go-around the tower ordered.
"""

from proatc import voice_loop
from proatc.atis import Issue
from proatc.debrief import review
from proatc.engine import Engine, Phase
from proatc.intents import Intent
from proatc.navfix import NavFix
from proatc.voice_loop import informational
from proatc.weather import Cloud, Weather
from tests.test_ifr import CALLSIGN, ISSUE, at
from tests.test_next import with_traffic
from tests.test_realism import MURK, ONLY_09, ground_world, make, taxiing

LIGHT_WESTERLY = Issue("F", Weather(wind_dir_true=270, wind_kt=4, visibility_sm=2, clouds=[Cloud(0.8, 600)]), 12 * 3600)


# ---- 1. no approach, and weather that rules out a visual one --------------------------


def test_no_visual_approach_is_cleared_in_weather_that_does_not_allow_one(tmp_path):
    _, engine, approach, _ = make(tmp_path, east=[])
    inbound = at(51.1, 5.0, feet=7000, heading=90)
    first = engine.handle(Intent("check_in"), approach, MURK, inbound)
    assert first.text == (
        f"{CALLSIGN}, Eastfield Approach, identified, no instrument approach is available for runway two seven "
        "and the weather is below visual minima, say intentions"
    )
    assert "cleared" not in first.text and engine.flight.arrival.unavailable
    for _ in range(3):  # and nothing is cleared afterwards either
        assert engine.tick(approach, MURK, inbound) is None
    assert engine.flight.arrival.stage is None
    assert "Request hold" in [item["label"] for item in engine.suggestions(approach, MURK, inbound)]

    # In good weather, the same missing data still gives a visual approach.
    _, fine, approach, _ = make(tmp_path, east=[])
    assert "expect visual approach runway two seven" in fine.handle(Intent("check_in"), approach, ISSUE, inbound).text


def test_an_approach_to_the_other_end_is_used_when_the_tailwind_is_small(tmp_path):
    _, engine, approach, _ = make(tmp_path, east=ONLY_09)
    light = engine.handle(Intent("check_in"), approach, LIGHT_WESTERLY, at(51.1, 5.7, feet=7000, heading=270))
    assert "expect V O R D M E approach runway zero niner" in light.text and "circle" not in light.text
    assert engine.flight.runway == "09"  # landing on it, four knots of tailwind
    _, strong, approach, _ = make(tmp_path, east=ONLY_09)  # ten knots of tailwind is too much
    assert "say intentions" in strong.handle(Intent("check_in"), approach, MURK, at(51.1, 5.7, feet=7000, heading=270)).text


# ---- 2. the terrain floor applies to what the pilot asks for ----------------------------


def test_a_requested_descent_stops_at_the_terrain_floor(tmp_path):
    db, engine, _, _ = make(tmp_path, navfix=NavFix(mora={(51, 0): 14000}))
    centre = engine.centers.on_frequency(127500, 51.0, 0.5)
    engine.flight.cleared_ft = 20000
    reply = engine.handle(Intent("request_descent", altitude=1000), centre, None, at(51.0, 0.5, feet=20000, heading=90))
    assert reply.text == (
        f"{CALLSIGN}, Westland Control, identified, unable altitude one thousand feet due terrain, descend flight level one four zero"
    )
    assert reply.expect == {"altitude": 14000} and engine.flight.cleared_ft == 14000
    engine.flight.pending = None
    low = engine.handle(Intent("request_descent", altitude=1000), centre, None, at(51.0, 0.5, feet=14200, heading=90))
    assert low.text == f"{CALLSIGN}, unable lower, the minimum safe altitude in your area is flight level one four zero"
    assert engine.flight.cleared_ft == 14000
    # Above flat ground the same request is simply granted.
    engine.flight.pending = None
    east = engine.centers.on_frequency(132100, 51.0, 3.0)
    assert engine.handle(Intent("request_descent", altitude=9000), east, None, at(51.0, 3.0, feet=20000)).expect == {
        "altitude": 9000
    }


# ---- 3. the runway is watched after the clearance ---------------------------------------


def test_a_runway_occupied_after_the_landing_clearance_means_go_around(tmp_path):
    _, engine, _, tower = make(tmp_path)
    final = at(51.0, 5.42, feet=700, heading=270)  # two miles out
    cleared = engine.handle(Intent("position_report", position="final"), tower, ISSUE, final)
    assert "cleared to land" in cleared.text and engine.flight.phase == Phase.LANDING_CLEARED
    engine.handle(Intent("readback", runway="27"), tower, ISSUE, final)
    assert engine.tick(tower, ISSUE, final) is None  # runway clear: nothing to say
    intruder = (51.0, 5.36, 300.0, 270.0, True)
    short_final = with_traffic(at(51.0, 5.39, feet=400, heading=270), intruder)
    around = engine.tick(tower, ISSUE, short_final)
    assert around.text == f"{CALLSIGN}, go around, I say again, go around, traffic on the runway"
    assert around.event == "go_around" and engine.flight.phase == Phase.AIRBORNE
    assert engine.tick(tower, ISSUE, short_final) is None  # once


def test_a_takeoff_clearance_is_cancelled_if_the_runway_becomes_occupied(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    tower = engine.db.station(118100, 51.0, -1.0)
    lined_up = taxiing(51.004, -0.981)
    engine.flight.contacted.add(tower.key)
    assert "cleared for takeoff" in engine.handle(Intent("ready_for_departure"), tower, ISSUE, lined_up).text
    intruder = (51.004, -1.0, 100.0, 270.0, True)
    cancelled = engine.tick(tower, ISSUE, with_traffic({**lined_up, "elevation_m": 30.0}, intruder))
    assert cancelled.text.startswith(f"{CALLSIGN}, hold position, cancel takeoff clearance")
    assert engine.flight.phase == Phase.TAXI_OUT and engine.flight.waiting_takeoff
    again = engine.tick(tower, ISSUE, with_traffic({**lined_up, "elevation_m": 30.0}))  # the runway is free again
    assert "cleared for takeoff" in again.text


# ---- 4. readbacks ----------------------------------------------------------------------


def test_roger_is_not_a_readback(tmp_path):
    db, engine, ground = ground_world(tmp_path)
    tower = db.station(118100, 51.0, -1.0)
    here = taxiing(51.004, -0.981)
    engine.handle(Intent("ready_for_departure"), tower, ISSUE, here)
    asked = engine.handle(Intent("readback"), tower, ISSUE, here)
    assert asked.text == f"{CALLSIGN}, read back the runway" and asked.event == "readback_incomplete"
    assert engine.flight.pending is not None  # still owed
    assert engine.handle(Intent("readback", runway="27R"), tower, ISSUE, here).text == ""
    assert engine.flight.pending is None

    # Part of a clearance read back: the rest is asked for, once. After that it is taken as given.
    _, engine, approach, _ = make(tmp_path)
    inbound = at(51.1, 5.0, feet=7000, heading=90)
    vector = engine.handle(Intent("check_in"), approach, ISSUE, inbound)
    assert set(vector.expect) == {"heading", "altitude"}
    partial = engine.handle(Intent("readback", heading=vector.expect["heading"]), approach, ISSUE, inbound)
    assert partial.text == f"{CALLSIGN}, read back your cleared level" and engine.flight.pending is not None
    assert engine.handle(Intent("readback"), approach, ISSUE, inbound).text == "" and engine.flight.pending is None


def test_hold_short_and_north_headings_are_checked(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    parked = taxiing(50.9960, -1.0)
    taxi = engine.handle(Intent("request_taxi"), ground, ISSUE, parked)
    assert taxi.expect == {"hold_short": "09R/27L"}
    wrong = engine.handle(Intent("readback", runway="18"), ground, ISSUE, parked)
    assert wrong.text.startswith(f"{CALLSIGN}, negative, taxi to holding point") and wrong.event == "readback_corrected"
    for said in ("27L", "09R", "27R"):  # the runway to hold short of (either end), or the one being taxied to
        assert Engine._readback_errors(taxi.expect, Intent("readback", runway=said), "27R") == []
    button = next(item for item in engine.suggestions(ground, ISSUE, parked) if item["label"] == "Read back")
    assert button["intent"] == {"intent": "readback", "hold_short": "09R/27L", "runway": "09R"}
    assert engine.handle(Intent.from_dict(button["intent"]), ground, ISSUE, parked).text == ""  # complete: no question

    assert Engine._readback_errors({"heading": 90}, Intent("readback", heading=0)) == ["heading"]
    assert Engine._readback_errors({"heading": 360}, Intent("readback", heading=0)) == []
    assert Engine._readback_errors({"altitude": 3000}, Intent("readback", altitude=0)) == ["altitude"]
    assert Engine._readback_heard({"heading": 90, "altitude": 3000}, Intent("readback", heading=0)) == {"heading"}


# ---- 5 and 6. the free-form reply may not instruct; buttons need no model ------------------


class Model:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def reply(self, transcript, context, station_key="free", inform_only=False):
        self.calls.append(inform_only)
        return self.answer

    def remember(self, *args):
        pass


class NoParse:
    def parse(self, transcript, last="", known=None):
        return Intent("other")


def test_a_free_form_reply_that_instructs_is_not_spoken(tmp_path):
    for text in (
        "Golf Alfa Bravo Charlie Delta, cleared to land runway two seven",
        "Golf Alfa Bravo Charlie Delta, turn left heading one eight zero",
        "Golf Alfa Bravo Charlie Delta, taxi to stand five via Alfa",
        "Golf Alfa Bravo Charlie Delta, contact departure one two five decimal five",
        "Golf Alfa Bravo Charlie Delta, descend flight level eight zero",
        "Golf Alfa Bravo Charlie Delta, V F R departure approved",
    ):
        assert not informational(text), text
    for text in (
        "Golf Alfa Bravo Charlie Delta, standby",
        "Golf Alfa Bravo Charlie Delta, unable, say again your request",
        "Golf Alfa Bravo Charlie Delta, the airport has two runways, zero niner and two seven",
        "Golf Alfa Bravo Charlie Delta, wind two seven zero degrees one zero knots",
    ):
        assert informational(text), text

    _, engine, _, tower = make(tmp_path)
    raw = at(51.0, 5.3, feet=3000)
    events = []
    inventive = Model(f"{CALLSIGN}, cleared to land runway zero niner")
    said = voice_loop._answer(
        "can I land the other way", None, {}, tower.key, tower, ISSUE, raw, inventive, engine, NoParse(), events.append
    )
    assert said == f"{CALLSIGN}, unable, say again your request" and inventive.calls == [True]
    assert engine.flight.phase != Phase.LANDING_CLEARED and engine.flight.pending is None
    helpful = Model(f"{CALLSIGN}, the airport has one runway")
    assert voice_loop._answer(
        "how many runways", None, {}, tower.key, tower, ISSUE, raw, helpful, engine, NoParse(), events.append
    ) == (f"{CALLSIGN}, the airport has one runway")


def test_a_clicked_call_reaches_the_rules_engine_without_any_language_model(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    parked = taxiing(50.9960, -1.0)
    events = []
    said = voice_loop._answer(
        "request taxi", Intent("request_taxi"), {}, ground.key, ground, ISSUE, parked, None, engine, None, events.append
    )
    assert said.startswith(f"{CALLSIGN}, Testfield Ground, taxi to holding point runway two seven right")
    assert events == [{"type": "understood", "intent": "request_taxi"}] and engine.flight.pending is not None
    # Speech with no model still has nobody to interpret it: the audio chain echoes, as before.
    assert voice_loop._answer("request taxi", None, {}, ground.key, ground, ISSUE, parked, None, engine, None, events.append) == (
        "Readback check. request taxi"
    )


# ---- 7. the debrief ----------------------------------------------------------------------


def test_a_go_around_ordered_by_the_tower_is_not_a_missed_readback():
    def atc(t, text, event=None, unprompted=True):
        entry = {"t": t, "type": "atc", "station": "Eastfield Tower", "text": text, "unprompted": unprompted}
        return {**entry, "event": event} if event else entry

    said = "Golf Alfa Bravo Charlie Delta, go around, I say again, go around, traffic on the runway"
    pilot = {"t": 1000.0, "type": "pilot", "text": "final", "source": "voice"}
    for records in ([pilot, atc(1010.0, said, "go_around")], [pilot, atc(1010.0, said)]):  # a new log, and an old one
        result = review(records)
        assert result["readbacks"]["missed"] == 0 and result["score"] == 100
        assert [finding["kind"] for finding in result["findings"]] == ["go_around"]
    events = review(
        [
            pilot,
            atc(1010.0, "G, read back the runway", "readback_incomplete", False),
            atc(1020.0, "G, Tower, I say again, cleared to land", "readback_missed"),
            atc(1030.0, "G, negative, runway two seven", "readback_corrected", False),
        ]
    )
    assert [finding["kind"] for finding in events["findings"]] == ["incomplete", "missed", "readback"]
    assert events["score"] == 100 - 2 - 4 - 6 and events["readbacks"] == {"given": 0, "corrected": 1, "missed": 1}
