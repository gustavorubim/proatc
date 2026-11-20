"""The radio's threads together, in simulated time: hand-offs, readbacks and tuning when things overlap.

Every test runs the real session job loop, ATIS broadcaster and second radio against the stand-ins of
tests/sim.py, and checks what a pilot needs at the end: the right frequency, checked in with the right
station, no readback owed, no hand-off left to follow, nobody talking over the pilot, nothing said twice.

A real flight lost its hand-off to approach this way: a centre said "contact <next centre>", the copilot read
it back, and while it was tuning, the centre said "contact approach". The copilot checked in with the next
centre and the approach hand-off was never followed (first test).
"""

import random

import pytest

from proatc.intents import Intent
from proatc.voice_loop import TUNE_WAIT_S
from proatc.xpbridge import SimUnavailable
from tests.sim import APPROACH, EAST, EAST_2, WEST, Rig, descend_to, handoff_to_approach, handoff_to_next_centre

CALLSIGN = "Golf Alfa Bravo Charlie Delta"


@pytest.fixture
def rigs(tmp_path):
    made = []

    def make(**options):
        rig = Rig(tmp_path, **options)
        rig.check_in_first()
        made.append(rig)
        return rig

    yield make
    for rig in made:
        rig.stop()


def ok(rig, problems):
    assert not problems, "\n".join(problems) + "\n" + rig.transcript()


def readbacks(rig, station=None):
    return [item for item in rig.said("pilot") if item.text.startswith("Contact") or item.text.startswith("Descend")
            if station is None or item.station == station]  # fmt: skip


# ---- two hand-offs close together -------------------------------------------------


@pytest.mark.parametrize("tune_delay", [0.3, 1.8], ids=["fast tuning", "slow tuning"])
@pytest.mark.parametrize("apart", range(31))
def test_two_hand_offs_any_seconds_apart_end_on_the_last_one_checked_in(rigs, apart, tune_delay):
    rig = rigs(tune_delay=tune_delay)
    rig.controller_says(5, handoff_to_next_centre)
    rig.controller_says(5 + apart, handoff_to_approach)
    rig.run_until(5 + apart + 40)
    ok(rig, rig.settled_on(APPROACH, "Eastfield Approach"))
    assert len(rig.said("atc")) >= 3  # both hand-offs and approach's answer


def test_the_second_hand_off_while_the_copilot_is_tuning_the_first_is_followed_without_a_detour(rigs):
    rig = rigs(tune_delay=1.8)
    rig.controller_says(5, handoff_to_next_centre)
    rig.controller_says(16, handoff_to_approach)  # said by Westland while the radio is still changing over
    rig.run_until(50)
    ok(rig, rig.settled_on(APPROACH, "Eastfield Approach"))
    assert [item.station for item in rig.said("atc")][:2] == ["Westland Control", "Westland Control"]
    # Straight on to approach: nobody calls the centre in between to be told the same thing again.
    assert not [item for item in rig.said("pilot") if item.station == "Eastland Control"]


# ---- a call that arrives while something else is happening ------------------------


def test_a_hand_off_due_while_the_copilot_reads_back_waits_for_the_readback_and_is_read_back_too(rigs):
    rig = rigs()
    rig.controller_says(5, descend_to(11000))
    rig.controller_says(5, handoff_to_next_centre)  # due at the same moment as the descent
    rig.run_until(50)
    ok(rig, rig.settled_on(EAST, "Eastland Control"))
    descent, handoff = rig.said("atc")[:2]
    readback = next(item for item in rig.said("pilot") if item.text.startswith("Descend"))
    assert descent.text.endswith("descend flight level one one zero") and "contact Eastland" in handoff.text
    assert handoff.t > readback.t  # not over the copilot, and not in place of the descent
    assert len(readbacks(rig, "Westland Control")) == 2


def test_without_a_copilot_the_next_instruction_waits_for_the_pilots_readback_of_the_last(rigs):
    rig = rigs(copilot="off")
    rig.controller_says(5, descend_to(11000))
    rig.controller_says(6, handoff_to_next_centre)  # due a moment after the descent
    rig.run_until(16)
    assert len(rig.said("atc")) == 1  # the pilot is still thinking about the descent
    rig.say("Descend flight level one one zero, Golf Alfa Bravo Charlie Delta", Intent("readback", altitude=11000))
    rig.run_until(30)
    handoff = rig.said("atc")[-1]
    assert "contact Eastland" in handoff.text and handoff.t > rig.said("pilot")[0].t
    rig.say("Contact Eastland Control one three two decimal one, Golf Alfa Bravo Charlie Delta",
            Intent("readback", frequency="132.100"))  # fmt: skip
    rig.run_until(40)
    assert rig.engine.flight.pending is None  # both were read back: neither took the other's place
    ok(rig, rig.errors() + rig.talked_over())


def test_an_instruction_due_while_the_pilot_transmits_waits_until_the_pilot_and_the_reply_have_finished(rigs):
    rig = rigs(copilot="readbacks")
    rig.controller_says(5.2, descend_to(11000))
    rig.run_until(5)
    rig.say("Westland, request direct Delta", Intent("request_direct", fix="DELTA"), seconds=4)
    rig.run_until(40)
    reply, descent = rig.said("atc")[:2]
    assert reply.t >= 9 and "descend" in descent.text
    reply_ends = reply.t + len(reply.text.split()) / 2.7
    assert descent.t >= reply_ends, rig.transcript()  # not during the pilot's call, nor over the reply
    ok(rig, rig.talked_over() + rig.said_twice())
    assert rig.engine.flight.pending is None and rig.engine.flight.cleared_ft == 11000


def test_nobody_talks_while_the_key_is_down_even_while_the_copilot_is_still_tuning(rigs):
    rig = rigs(tune_delay=1.8)
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(15.8)  # the readback is done; the copilot is waiting for the radio to report the new frequency
    rig.controller_says(15.8, descend_to(11000))
    rig.say("Say again", Intent("say_again"), seconds=4)
    rig.run_until(60)
    ok(rig, rig.talked_over())
    ok(rig, rig.settled_on(EAST, "Eastland Control"))
    # The pilot pressed on Westland's frequency and let go on Eastland's: neither heard the whole call.
    assert any("changed during the call" in item.text for item in rig.said("info"))


# ---- the pilot's own hand on the knob ---------------------------------------------


def test_tuning_away_in_the_middle_of_a_hand_off_stops_the_sound_and_the_hand_off_is_still_followed(rigs):
    rig = rigs()
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(6)
    assert rig.session.player.playing
    rig.tune_by_hand(EAST_2)  # Eastland's other frequency
    rig.run_until(6.2)
    assert not rig.session.player.playing  # Westland is not heard on another frequency
    rig.run_until(40)
    ok(rig, rig.settled_on(EAST, "Eastland Control"))
    assert not readbacks(rig)  # the hand-off was obeyed: nobody to read it back to


def test_tuning_away_in_the_middle_of_a_descent_leaves_it_for_when_the_pilot_comes_back(rigs):
    rig = rigs()
    rig.controller_says(5, descend_to(11000))
    rig.run_until(6)
    rig.tune_by_hand(EAST_2)
    rig.run_until(20)
    assert rig.engine.flight.pending is not None and not rig.said("pilot")  # nobody here to read it back to
    rig.tune_by_hand(WEST)
    rig.run_until(40)
    ok(rig, rig.settled_on(WEST, "Westland Control"))
    assert [item.station for item in readbacks(rig)] == ["Westland Control"]


def test_tuning_away_in_the_middle_of_the_readback_loses_it_and_the_hand_off_is_still_followed(rigs):
    rig = rigs()
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(12)  # the copilot is reading back
    assert rig.session.busy.is_set()
    rig.tune_by_hand(EAST_2)
    rig.run_until(50)
    ok(rig, rig.settled_on(EAST, "Eastland Control"))
    assert not readbacks(rig)  # neither Westland nor Eastland got half a readback as a whole one
    assert any("changed during the call" in item.text for item in rig.said("info"))


# ---- a readback to the wrong controller -------------------------------------------


def test_a_readback_to_another_controller_is_not_taken_as_read_back(rigs):
    rig = rigs(copilot="off")
    rig.controller_says(5, descend_to(11000))
    rig.run_until(10)
    rig.tune_by_hand(EAST)
    rig.run_until(11)
    rig.say("Eastland Control, Golf Alfa Bravo Charlie Delta, descending flight level one three zero", Intent("check_in"))
    rig.run_until(20)
    rig.say("Descend flight level one one zero, Golf Alfa Bravo Charlie Delta", Intent("readback", altitude=11000))
    rig.run_until(30)
    silent = rig.said("silent")[-1]
    assert silent.station == "Eastland Control" and "not for Eastland Control" in silent.text
    assert rig.engine.flight.pending is not None  # Westland is still waiting for it
    calls = rig.engine.suggestions(rig.station(), None, rig.bridge.snapshot())
    assert "Read back" not in [call["label"] for call in calls]  # not offered to the wrong controller
    rig.tune_by_hand(WEST)
    rig.run_until(31)
    rig.say("Descend flight level one one zero, Golf Alfa Bravo Charlie Delta", Intent("readback", altitude=11000))
    rig.run_until(40)
    assert rig.engine.flight.pending is None


# ---- copilot settings ---------------------------------------------------------------


@pytest.mark.parametrize(
    "copilot, auto_tune, reads_back, tunes, checks_in",
    [
        ("off", False, False, False, False),
        ("off", True, False, False, False),  # auto-tune waits for the pilot's readback
        ("readbacks", False, True, False, False),
        ("readbacks", True, True, True, False),
        ("full", False, True, True, True),
        ("full", True, True, True, True),
    ],
)
def test_each_copilot_setting_does_its_part_of_a_hand_off_and_no_more(rigs, copilot, auto_tune, reads_back, tunes, checks_in):
    rig = rigs(copilot=copilot, auto_tune=auto_tune)
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(60)
    assert bool(readbacks(rig)) == reads_back, rig.transcript()
    assert bool(rig.bridge.written) == tunes and len(rig.bridge.written) <= 1
    assert any(item.station == "Eastland Control" for item in rig.said("pilot")) == checks_in
    if checks_in:
        ok(rig, rig.settled_on(EAST, "Eastland Control"))
    elif not reads_back:
        # Westland says it once more, and only once.
        assert [item.text for item in rig.said("atc")].count(f"{CALLSIGN}, Westland Control, I say again, contact "
                                                             "Eastland Control one three two decimal one") == 1  # fmt: skip
    ok(rig, rig.errors() + rig.said_twice() + rig.talked_over())


def test_with_auto_tune_the_frequency_is_tuned_once_the_pilot_has_read_it_back(rigs):
    rig = rigs(copilot="off", auto_tune=True)
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(12)
    assert not rig.bridge.written
    rig.say("Contact Eastland Control one three two decimal one, Golf Alfa Bravo Charlie Delta",
            Intent("readback", frequency="132.100"))  # fmt: skip
    rig.run_until(30)
    assert [value for _, _, value in rig.bridge.written] == [EAST] and rig.tuned_mhz() == "132.100"
    assert rig.engine.flight.next_frequency == "132.100"  # still to call: the app keeps it until then


# ---- tuning that fails ------------------------------------------------------------------


@pytest.mark.parametrize("refuse", ["error", "ignored"])
def test_a_tune_the_sim_will_not_take_is_reported_once_and_the_pilot_tuning_it_by_hand_is_checked_in(rigs, refuse):
    rig = rigs(refuse=refuse)
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(40)
    assert rig.tuned_mhz() == "127.500" and len(rig.bridge.written) == 1 and len(rig.errors()) == 1
    rig.tune_by_hand(EAST)
    rig.run_until(60)
    ok(rig, rig.settled_on(EAST, "Eastland Control", errors=1))


def test_a_tune_the_sim_takes_after_the_copilot_stopped_waiting_is_checked_in_when_it_arrives(rigs):
    rig = rigs(tune_delay=TUNE_WAIT_S + 3)  # longer than the copilot waits
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(60)
    ok(rig, rig.settled_on(EAST, "Eastland Control", errors=1))
    assert len(rig.bridge.written) == 1


def test_a_tune_the_sim_reports_after_three_and_a_half_seconds_is_no_error_and_is_checked_in(rigs):
    rig = rigs(tune_delay=3.5)  # as a real sim has taken
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(60)
    ok(rig, rig.settled_on(EAST, "Eastland Control"))
    assert len(rig.bridge.written) == 1


def test_a_hand_off_the_sim_snaps_to_the_next_channel_name_is_no_error_and_is_checked_in(rigs):
    rig = rigs(snap=5)  # 132.100 is reported as 132.105: the same frequency
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(60)
    station, flight = rig.station(), rig.engine.flight
    assert rig.tuned_mhz() == "132.105" and not rig.errors(), rig.transcript()
    assert station.callsign == "Eastland Control" and station.key in flight.contacted, rig.transcript()
    assert flight.next_frequency is None and flight.pending is None and len(rig.bridge.written) == 1


# A frequency tuned at the pilot's request in the app (the pilot's own click: no copilot involved).


@pytest.mark.parametrize(
    "asked, snap, reported",
    [("118.420", 5, "118.425"), ("118.420", 10, "118.430"), ("118.420", -5, "118.415"), ("118.000", 5, "118.005"),
     ("118.005", -5, "118.000")],
)  # fmt: skip
def test_a_frequency_the_sim_snaps_to_an_8_33_channel_name_next_to_it_is_tuned_without_an_error(rigs, asked, snap, reported):
    rig = rigs(snap=snap)
    rig.session.tune(asked)
    assert rig.tuned_mhz() == reported and not rig.errors()
    assert [item.text for item in rig.said("info")] == [f"COM1 tuned to {reported}"]


@pytest.mark.parametrize("delay", [3.5, TUNE_WAIT_S], ids=["late", "as the wait ends"])
def test_a_frequency_the_sim_reports_late_is_tuned_without_an_error(rigs, delay):
    rig = rigs(tune_delay=delay)
    rig.session.tune("118.420")
    assert rig.tuned_mhz() == "118.420" and not rig.errors() and len(rig.said("info")) == 1


@pytest.mark.parametrize("refuse, snap", [("ignored", 0), (None, 5)], ids=["stays where it was", "another channel"])
def test_a_frequency_the_radio_does_not_take_is_reported_once(rigs, refuse, snap):
    rig = rigs(refuse=refuse, snap=snap)
    with pytest.raises(SimUnavailable, match="did not take 118.005 on COM1") as failed:
        rig.session.tune("118.005")  # with `snap` the radio lands on 118.010, the next 8.33 kHz channel
    assert rig.clock.monotonic() >= TUNE_WAIT_S and len(rig.bridge.written) == 1 and not rig.said("info")
    assert rig.tuned_mhz() == ("127.500" if refuse else "118.010") and str(failed.value).count("118.005") == 1


# ---- while a readback is owed -------------------------------------------------------------


def test_a_level_bust_is_called_at_once_even_while_a_readback_is_owed(rigs):
    rig = rigs(copilot="off")
    rig.controller_says(5, descend_to(11000))
    rig.run_until(6)
    rig.bridge.cleared, rig.bridge.vertical_fpm = (lambda: 9000), 6000  # the pilot dials too low, and fast
    rig.run_until(40)
    crossed = 6 + (13000 - (11000 - 400)) / 100  # 400 ft through the cleared level, at 100 ft a second
    bust = next(item for item in rig.said("atc") if "check your level" in item.text)
    assert bust.t < crossed + 1.5, rig.transcript()  # not held back until the descent is read back
    assert rig.engine.flight.pending is not None and "descend" in rig.engine.flight.pending.text  # still owed


def test_a_low_altitude_alert_is_given_at_once_even_while_a_readback_is_owed(rigs):
    rig = rigs(copilot="off")
    engine, raw, station = rig.engine, rig.bridge.snapshot(), rig.station()
    rig.controller_says(0, descend_to(11000))
    assert "descend" in engine.tick(station, None, raw).text and engine.flight.pending is not None
    engine.flight.cleared_ft = 500  # so that it is the ground, not the cleared level, that is close
    low = {**raw, "agl_m": 600 * 0.3048, "elevation_m": 600 * 0.3048, "vs_fpm": -2000.0}
    alert = engine.tick(station, None, low)
    assert alert is not None and "low altitude alert" in alert.text and not alert.expect
    assert "descend" in engine.flight.pending.text  # the descent is still owed, not replaced


def test_after_the_controller_says_it_again_the_pilot_has_time_to_read_it_back_before_the_next(rigs):
    rig = rigs(copilot="off")
    rig.controller_says(5, descend_to(11000))
    rig.controller_says(6, handoff_to_next_centre)  # waiting behind the descent
    rig.run_until(45)
    again = next(item for item in rig.said("atc") if "I say again" in item.text)
    assert "descend" in again.text and not any("contact Eastland" in item.text for item in rig.said("atc"))
    rig.say("Descend flight level one one zero, Golf Alfa Bravo Charlie Delta", Intent("readback", altitude=11000))
    rig.run_until(60)
    assert rig.said("silent") and not any("negative" in item.text or "read back" in item.text for item in rig.said("atc"))
    handoff = next(item for item in rig.said("atc") if "contact Eastland" in item.text)
    assert handoff.t > rig.said("pilot")[0].t  # given once the descent was read back
    ok(rig, rig.talked_over())


def test_with_auto_tune_alone_a_hand_off_not_read_back_does_not_pull_the_pilot_off_another_frequency(rigs):
    rig = rigs(copilot="off", auto_tune=True)
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(10)
    rig.tune_by_hand(121500)  # the pilot listens elsewhere: the hand-off has not been read back
    rig.run_until(30)
    assert not rig.bridge.written and rig.tuned_mhz() == "121.500"


def test_a_copilot_readback_lost_to_a_retune_is_given_again_when_the_pilot_comes_back(rigs):
    rig = rigs(copilot="readbacks")
    rig.controller_says(5, descend_to(11000))
    rig.run_until(12)  # the copilot is reading back
    assert rig.session.busy.is_set()
    rig.tune_by_hand(EAST_2)
    rig.run_until(16)
    assert any("changed during the call" in item.text for item in rig.said("info"))
    rig.tune_by_hand(WEST)  # back on Westland, who is still waiting for it
    rig.run_until(30)  # well before Westland would say it again
    assert rig.engine.flight.pending is None, rig.transcript()
    assert [item.station for item in readbacks(rig)] == ["Westland Control"]


# ---- slow voices, slow jobs, a sim that rounds its frequencies ----------------------------


def test_a_controller_whose_voice_is_slow_to_make_does_not_cut_into_a_call_begun_meanwhile(rigs):
    rig = rigs(copilot="off", voice_delay=2.0)
    rig.controller_says(5, descend_to(11000))
    rig.run_until(5.6)  # the descent has been decided; its voice is being made
    rig.click("Westland, Golf Alfa Bravo Charlie Delta, request direct Delta", Intent("request_direct", fix="DELTA"))
    rig.run_until(40)
    ok(rig, rig.talked_over())  # neither the pilot's call, nor the reply, nor the descent was cut off
    reply, descent = rig.said("atc")[:2]
    assert "descend" in descent.text and descent.t > reply.t
    assert rig.engine.flight.pending is not None and "descend" in rig.engine.flight.pending.text


def test_a_call_keyed_while_the_copilot_is_busy_tuning_is_recorded_from_the_moment_the_key_went_down(rigs):
    rig = rigs()
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(16)  # the readback is done and the radio is on Eastland; the copilot pauses before calling
    assert rig.tuned_mhz() == "132.100"
    rig.say("Eastland Control, Golf Alfa Bravo Charlie Delta, level flight level one three zero", Intent("check_in"),
            seconds=1.2)  # fmt: skip
    rig.run_until(40)
    assert not [item for item in rig.said("info") if "too short" in item.text], rig.transcript()
    assert [item.station for item in rig.said("pilot") if item.event.get("source") == "voice"] == ["Eastland Control"]
    ok(rig, rig.settled_on(EAST, "Eastland Control"))


def test_a_frequency_the_sim_rounds_after_tuning_does_not_lose_a_call_made_meanwhile(rigs):
    # 132.100 is reported as 132.105 half a second after it was taken. No copilot pause: the call is answered at once.
    rig = rigs(copilot="readbacks", auto_tune=True, settle_khz=5)
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(15.9)
    assert rig.tuned_mhz() == "132.100"
    rig.say("Eastland Control, Golf Alfa Bravo Charlie Delta, level flight level one three zero", Intent("check_in"),
            seconds=1.0)  # fmt: skip
    rig.run_until(40)
    assert not any("changed during the call" in item.text for item in rig.said("info")), rig.transcript()
    assert [item.station for item in rig.said("pilot") if item.event.get("source") == "voice"] == ["Eastland Control"]
    ok(rig, rig.settled_on(EAST + 5, "Eastland Control"))


def test_a_readback_on_the_frequency_the_instruction_was_given_on_counts_when_another_station_answers_there(rigs):
    # Where a centre and an airport share a frequency, which of them answers can change as the aircraft moves,
    # while the pilot has not touched the radio. The readback is for whoever is on that frequency.
    rig = rigs(copilot="off")
    engine, centres = rig.engine, rig.db.centers
    approach = rig.db.station(APPROACH, 51.0, 5.0)
    here = {**rig.bridge.snapshot(), "lon": 4.4, "com1_khz": APPROACH}
    engine.flight.contacted.add(approach.key)  # checked in with approach on 124.300
    rig.controller_says(0, descend_to(9000))
    given = engine.tick(approach, None, here)
    assert "descend" in given.text
    engine.on_air(given)  # heard: the radio that would say it is passed by here
    assert engine.flight.pending is not None and engine.flight.pending_key == approach.key
    centre = centres.on_frequency(EAST, 51.0, 4.4)  # a different station, as if found on 124.300
    readback = Intent("readback", altitude=engine.flight.pending.expect["altitude"])
    elsewhere = engine.handle(readback, centre, None, {**here, "com1_khz": EAST})
    assert "not for Eastland Control" in elsewhere.note and engine.flight.pending is not None  # really another one
    assert [call["label"] for call in engine.suggestions(centre, None, here)][0] == "Read back"
    assert engine.handle(readback, centre, None, here).text == "" and engine.flight.pending is None


# ---- the same flight, many times ----------------------------------------------------------


def jittered_flight(rigs, seed):
    """Two hand-offs and a descent at random moments, random tuning delays, chatter on the frequency."""
    rng = random.Random(seed)
    rig = rigs(tune_delay=rng.uniform(0.05, 1.9), chatter="busy", seed=seed, voice_delay=rng.uniform(0, 2))
    first = rng.uniform(1, 10)
    rig.controller_says(first, handoff_to_next_centre)
    rig.controller_says(first + rng.uniform(0, 12), descend_to(11000))
    rig.controller_says(first + rng.uniform(0, 30), handoff_to_approach)
    rig.run_until(90)
    return rig


@pytest.mark.parametrize("seed", range(25))
def test_with_random_small_delays_the_flight_ends_the_same_way_every_time(rigs, seed):
    rig = jittered_flight(rigs, seed)
    ok(rig, rig.settled_on(APPROACH, "Eastfield Approach"))
    assert rig.engine.flight.cleared_ft is not None


def test_a_run_gives_the_same_transcript_every_time(rigs):
    first, second = jittered_flight(rigs, 7).transcript(), jittered_flight(rigs, 7).transcript()
    assert first == second and first


# ---- an instruction that is decided and not yet heard ----------------------------------------
#
# A real flight lost its approach clearance this way. The clearance took the voice a long time to make; the "Read
# back" button showed it from the moment it was decided; the pilot clicked it; the readback settled the instruction,
# and the controller never said it. The journal had no trace of the clearance.


def labels(rig):
    return [call["label"] for call in rig.engine.suggestions(rig.station(), None, rig.bridge.snapshot())]


def test_a_readback_is_not_offered_until_the_controller_has_started_saying_the_instruction(rigs):
    rig = rigs(copilot="off", voice_delay=6.0)
    rig.controller_says(5, descend_to(11000))
    rig.run_until(8)  # decided at five seconds; its voice takes six to make
    flight = rig.engine.flight
    assert flight.pending is not None and "descend" in flight.pending.text and not rig.said("atc")
    assert rig.engine.not_yet_said() and "Read back" not in labels(rig)
    rig.run_until(13)
    assert "descend" in rig.said("atc")[0].text  # on the air: now it is read back
    assert not rig.engine.not_yet_said() and labels(rig)[0] == "Read back"


def test_a_readback_made_before_the_instruction_was_heard_does_not_take_it_off_the_air(rigs):
    rig = rigs(copilot="off", voice_delay=6.0)
    rig.controller_says(5, descend_to(11000))
    rig.run_until(8)
    readback = "Descend flight level one one zero, Golf Alfa Bravo Charlie Delta"
    rig.click(readback, Intent("readback", altitude=11000))  # a button from before, or a pilot reading the screen
    rig.run_until(40)
    said = [item.text for item in rig.said("atc")]
    assert any("descend flight level one one zero" in text for text in said), rig.transcript()  # still said
    assert rig.engine.flight.cleared_ft == 11000
    ok(rig, rig.talked_over())


def test_the_copilot_does_not_read_back_or_follow_a_hand_off_nobody_has_heard(rigs):
    rig = rigs(copilot="full", voice_delay=6.0)
    rig.controller_says(5, handoff_to_next_centre)
    rig.run_until(9)  # decided, not said
    rig.session.jobs.put(("after", None, None))  # something else happened on the radio meanwhile
    rig.run_until(10.5)
    assert not rig.said("pilot") and rig.bridge.snapshot()["com1_khz"] == WEST  # nothing read back, nothing tuned
    rig.run_until(60)
    handoff, readback = rig.said("atc")[0], rig.said("pilot")[0]
    assert "contact" in handoff.text and readback.t > handoff.t
    ok(rig, rig.settled_on(EAST, "Eastland Control"))


def test_a_call_that_was_decided_and_not_said_is_kept_in_the_journal(rigs):
    rig = rigs(copilot="off", voice_delay=3.0)
    notes = []
    rig.session.log.note = lambda kind, context=None, **fields: notes.append((kind, fields))
    rig.controller_says(5, descend_to(11000))
    rig.run_until(6)
    rig.click("Westland, Golf Alfa Bravo Charlie Delta, request direct Delta", Intent("request_direct", fix="DELTA"))
    rig.run_until(40)
    unsaid = [fields for kind, fields in notes if kind == "unsaid"]
    assert unsaid and "descend" in unsaid[0]["text"] and "frequency was taken" in unsaid[0]["why"]
    assert not [item for item in rig.heard if item.type == "unsaid"]  # for the journal only: not shown in the app
    descent = next(item for item in rig.said("atc") if "descend" in item.text)
    assert descent.event["voice_s"] >= 3.0  # and how long the voice took is kept with the call
