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


# ---- the same flight, many times ----------------------------------------------------------


def jittered_flight(rigs, seed):
    """Two hand-offs and a descent at random moments, random tuning delays, chatter on the frequency."""
    rng = random.Random(seed)
    rig = rigs(tune_delay=rng.uniform(0.05, 1.9), chatter="busy", seed=seed)
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
