"""The health report: what went wrong on the radio in a flight, found in its journal.

Every flight here is made up: invented times, positions and the placeholder callsign G-ABCD.
Each fault has a test that finds it and, as important, tests that the ordinary things a flight
does (sitting parked, pausing the sim, taking a few seconds to tune) are not reported.
"""

import json

from proatc import health, voice_loop
from proatc.config import Config
from proatc.health import as_text, check, for_app
from proatc.intents import Intent
from proatc.llm import LlmError
from tests.test_journal import lines, session_with_journal
from tests.test_realism import APRON, taxiing

T0 = 1_800_000_000.0
CALLSIGN = "Golf Alfa Bravo Charlie Delta"
DESTINATION = (51.0, 0.0)  # one degree of latitude is sixty miles
FAR = (52.0, 0.0)


def context(tuned, station, ground=False, paused=False, at=FAR, feet=5000, phase=None):
    phase = phase or ("parked" if ground else "airborne")
    raw = {"lat": at[0], "lon": at[1], "elevation_m": feet / 3.28084, "on_ground": int(ground), "paused": int(paused)}
    return {"tuned": tuned, "station": station, "flight": {"phase": phase}, "raw": raw}


def trace(t, tuned, station, **where):
    return {"t": T0 + t, "type": "trace", "context": context(tuned, station, **where)}


def atc(t, tuned, station, text, unprompted=False, **where):
    extra = {key: where.pop(key) for key in ("fallback", "event") if key in where}
    return {"t": T0 + t, "type": "atc", "station": station, "text": f"{CALLSIGN}, {text}", "unprompted": unprompted,
            **extra, "context": context(tuned, station, **where)}  # fmt: skip


def pilot(t, tuned, station, text="request", **where):
    return {"t": T0 + t, "type": "pilot", "text": text, "source": "clicked", "context": context(tuned, station, **where)}


def understood(t, tuned, station, intent, **where):
    return {"t": T0 + t, "type": "understood", "intent": intent, "context": context(tuned, station, **where)}


def tuned_to(t, mhz, station, **where):
    return {"t": T0 + t, "type": "info", "text": f"COM1 tuned to {mhz}", "context": context(mhz, station, **where)}


def kinds(records, locate=None):
    return [finding["kind"] for finding in check(records, locate)]


def flying(seconds, tuned="127.100", station="Westland Center", start=0, every=20, **where):
    return [trace(start + t, tuned, station, **where) for t in range(0, seconds + 1, every)]


# ---- a frequency nobody answers on ------------------------------------------------------


def test_a_silent_frequency_in_flight_is_reported_with_where_and_for_how_long():
    records = [
        atc(0, "127.100", "Westland Center", "contact Eastland Center one three two decimal four"),
        *flying(100, start=5),
        *flying(100, tuned="132.400", station=None, start=110),
        *flying(40, tuned="132.450", station="Eastland Center", start=220),
    ]
    [finding] = [f for f in check(records) if f["kind"] == "silent_frequency"]
    assert finding["text"] == "Nobody answered on 132.400 for 1 min 50 s while airborne."
    assert finding["detail"]["frequency"] == "132.400" and finding["detail"]["seconds"] == 110
    assert finding["detail"]["position"] == "52.000, 0.000, 5,000 ft"
    assert finding["detail"]["last_station"] == "Westland Center"


def test_a_parked_aircraft_a_paused_sim_and_a_short_silence_are_not_reported():
    parked = flying(600, tuned="132.400", station=None, ground=True)
    assert kinds(parked) == []
    # Paused in flight on a silent frequency: one trace, then nothing until the pilot comes back.
    paused = [trace(0, "132.400", None, paused=True), trace(1800, "132.400", None, paused=True)]
    assert kinds(paused) == []
    brief = flying(40, tuned="132.400", station=None)
    assert kinds(brief) == []
    # An ATIS frequency has a station: the broadcast. Nobody talks on it, and that is not a fault.
    atis = flying(300, tuned="128.075", station="Westfield Information")
    assert kinds(atis) == []


def test_the_time_the_app_was_closed_does_not_count_as_silence():
    records = [trace(0, "132.400", None), trace(20, "132.400", None), trace(3600, "132.400", None)]
    assert kinds(records) == []  # twenty seconds, and then a gap of an hour that is not flight


# ---- the controller with nothing to say -------------------------------------------------


def test_replies_from_the_free_form_fallback_that_lead_nowhere_are_counted_exactly():
    records = [
        understood(0, "127.100", "Westland Center", "request_approach"),
        atc(1, "127.100", "Westland Center", "unable, say again your request", fallback=True),
        understood(10, "127.100", "Westland Center", "other"),
        atc(11, "127.100", "Westland Center", "roger, say intentions.", fallback=True),
        # The fallback answered a question: no dead end.
        atc(20, "127.100", "Westland Center", "the wind is two seven zero at one zero", fallback=True),
        # The rules engine asking an emergency for intentions is doing its job.
        atc(30, "127.100", "Westland Center", "roger mayday, squawk seven seven zero zero, say intentions"),
    ]
    [finding] = check(records)
    assert finding["kind"] == "dead_end" and finding["detail"]["count"] == 2
    assert finding["text"] == "The controller had no answer 2 times and asked the pilot to say again or say intentions."
    assert finding["detail"]["understood_as"] == "request_approach, other" and finding["detail"]["exact"] == "yes"


def test_journals_written_before_replies_were_marked_are_read_by_their_words():
    records = [
        atc(1, "127.100", "Westland Center", "unable, say again your request"),
        atc(11, "127.100", "Westland Center", "Westland Center, roger, say intentions."),
        atc(30, "127.100", "Westland Center", "roger mayday, squawk seven seven zero zero, say intentions"),
        atc(40, "127.100", "Westland Center", "the weather is below V F R minima, special V F R is available, say intentions"),
    ]
    [finding] = check(records)
    assert finding["detail"]["count"] == 2 and finding["detail"]["exact"].startswith("no")


class Model:
    histories: dict = {}  # noqa: RUF012 - no conversation kept

    def complete(self, *args, **kwargs):  # the second reading: no model here, so the free answer takes the call
        raise LlmError("no model")

    def __init__(self, answer):
        self.answer = answer

    def reply(self, transcript, context, station_key="free", inform_only=False):
        assert "fallback" not in context  # the model is not told about the journal's bookkeeping
        return self.answer

    def remember(self, *args):
        pass


def test_the_radio_loop_marks_a_reply_that_came_from_the_free_form_fallback(tmp_path):
    from tests.test_realism import ground_world

    _, engine, ground = ground_world(tmp_path)
    parked = taxiing(*APRON)
    fallback: dict = {}
    said = voice_loop._answer(
        "what now",
        Intent("other"),
        fallback,
        ground.key,
        ground,
        None,
        parked,
        Model(f"{CALLSIGN}, standby"),
        engine,
        None,
        lambda event: None,
    )
    assert said == f"{CALLSIGN}, standby" and fallback["fallback"] is True
    rules: dict = {}
    voice_loop._answer(
        "radio check", Intent("radio_check"), rules, ground.key, ground, None, parked, Model("x"), engine, None, lambda e: None
    )
    assert "fallback" not in rules


# ---- a station that changes under the pilot -----------------------------------------------


def test_a_station_that_changes_mid_conversation_on_one_frequency_is_reported():
    records = [
        tuned_to(0, "125.050", "Eastland Approach"),
        trace(3, "125.050", "Eastland Approach"),
        pilot(7, "125.050", "Eastland Approach"),
        atc(7, "125.050", "Eastland Approach", "radar contact"),
        trace(23, "125.050", "Westfield Departure"),
        trace(43, "125.050", "Westfield Departure"),
        trace(103, "125.050", "Eastland Approach"),
    ]
    [finding] = check(records)
    assert finding["kind"] == "station_changed" and finding["time"] == health._clock(T0 + 23)
    assert finding["detail"]["stations"] == "Eastland Approach -> Westfield Departure -> Eastland Approach"
    assert finding["text"].startswith("On 125.050 the answering station changed twice during a conversation")


def test_a_station_change_without_a_conversation_on_the_ground_or_from_a_retune_is_not_reported():
    passing_by = [trace(0, "122.800", "Northfield Traffic"), trace(20, "122.800", "Southfield Traffic")]
    assert kinds(passing_by) == []
    on_the_ground = [
        pilot(0, "121.800", "Westfield Ground", ground=True),
        trace(20, "121.800", "Eastfield Ground", ground=True),
    ]
    assert kinds(on_the_ground) == []
    # The tune confirmation is read a moment before the new station is found: not a change.
    retuned = [
        pilot(0, "127.100", "Westland Center"),
        tuned_to(5, "127.100", "Westland Center"),
        tuned_to(10, "118.550", "Westland Center"),
        pilot(19, "118.550", "Eastland Center"),
        trace(20, "118.550", "Eastland Center"),
    ]
    assert kinds(retuned) == []


# ---- hand-offs -----------------------------------------------------------------------------


def test_a_hand_off_that_is_never_tuned_is_reported():
    records = [
        atc(0, "127.100", "Westland Center", "contact Eastland Approach one two eight decimal five two five", unprompted=True),
        *flying(200, start=10),
    ]
    [finding] = check(records)
    assert finding["kind"] == "handoff_missed"
    assert finding["text"] == "Handed to Eastland Approach on 128.525, which was not tuned within two minutes."
    assert finding["detail"]["tuned_instead"] == "127.100" and finding["detail"]["replaced_by_later_handoff"] == "no"


def test_the_time_between_a_hand_off_and_its_tuning_is_not_reported():
    handed = atc(0, "127.100", "Westland Center", "contact Eastland Center one three two decimal four", unprompted=True)
    in_time = [
        handed,
        *flying(60, start=10),
        tuned_to(70, "132.400", "Eastland Center"),
        *flying(200, "132.400", "Eastland Center", 80),
    ]
    assert kinds(in_time) == []
    # The sim shows 118.425 for 118.420: the same channel.
    rounded = atc(0, "127.100", "Westland Center", "contact Eastland Center one one eight decimal four two", unprompted=True)
    assert kinds([rounded, trace(30, "118.425", "Eastland Center"), *flying(200, "118.425", "Eastland Center", 40)]) == []
    # The flight's record ends before two minutes have passed: the pilot may still have tuned it.
    assert kinds([handed, *flying(60, start=10)]) == []
    # Paused for half an hour, then tuned a minute later.
    paused = [handed, trace(30, "127.100", "Westland Center", paused=True), trace(1830, "127.100", "Westland Center")]
    assert kinds([*paused, tuned_to(1890, "132.400", "Eastland Center"), *flying(200, "132.400", "Eastland Center", 1900)]) == []
    # On the ground, "contact ground when ready for push" is followed when the pilot is ready.
    ground = atc(0, "121.650", "Westfield Delivery", "readback correct, contact Westfield Ground one two one decimal niner",
                 ground=True)  # fmt: skip
    assert kinds([ground, *flying(600, tuned="121.650", station="Westfield Delivery", ground=True)]) == []


def test_two_hand_offs_seconds_apart_are_reported_and_the_one_that_was_lost():
    records = [
        atc(0, "127.100", "Westland Center", "contact Eastland Center one one eight decimal zero two", unprompted=True),
        atc(15, "127.100", "Westland Center", "contact Eastland Approach one two eight decimal five two five", unprompted=True),
        tuned_to(17, "118.020", "Eastland Center"),
        *flying(200, tuned="118.025", station="Eastland Center", start=20),
    ]
    close, missed = check(records)
    assert close["kind"] == "handoffs_close"
    assert close["text"] == "Two hand-offs came 15 seconds apart: to Eastland Center and then to Eastland Approach."
    assert missed["kind"] == "handoff_missed" and missed["detail"]["frequency"] == "128.525"
    assert missed["detail"]["tuned_instead"] == "118.020"


def test_a_hand_off_replaced_by_a_later_one_before_it_was_tuned_says_so():
    records = [
        atc(0, "127.100", "Westland Center", "contact Eastland Center one one eight decimal zero two", unprompted=True),
        atc(40, "127.100", "Westland Center", "contact Northland Center one three four decimal one", unprompted=True),
        tuned_to(50, "134.100", "Northland Center"),
        *flying(200, tuned="134.100", station="Northland Center", start=60),
    ]
    [missed] = check(records)
    assert (
        missed["detail"]["frequency"] == "118.020" and missed["detail"]["replaced_by_later_handoff"] == "Northland Center 134.100"
    )


# ---- readbacks -----------------------------------------------------------------------------


def test_many_corrected_readbacks_are_reported_and_a_few_are_not():
    corrections = [
        atc(t, "127.100", "Westland Center", "negative, climb flight level one two zero", event="readback_corrected")
        for t in (0, 60, 120)
    ]
    [finding] = check(corrections)
    assert finding["kind"] == "readback_trouble" and finding["detail"]["corrected"] == 3
    assert kinds(corrections[:2]) == []
    said_again = [understood(t, "127.100", "Westland Center", "say_again") for t in (0, 30, 60)]
    assert kinds(said_again) == ["readback_trouble"]
    go_around = [atc(t, "118.700", "Westfield Tower", "go around, I say again, go around") for t in (0, 60, 120)]
    assert kinds(go_around) == []


# ---- the arrival ---------------------------------------------------------------------------


def arrival(*calls, last=(51.0, 0.0)):
    """A flight from sixty miles out to a landing at the destination, with these calls on the way."""
    return [
        *flying(60, at=FAR),
        *calls,
        trace(600, "118.700", "Westfield Tower", at=(51.05, 0.0), feet=500),
        trace(620, "118.700", "Westfield Tower", at=last, ground=True, phase="landed"),
    ]


def test_a_flight_that_landed_without_approach_or_tower_is_reported():
    records = arrival(pilot(100, "127.100", "Westland Center", at=(51.5, 0.0)))
    assert kinds(records) == ["no_arrival_contact"]
    findings = check(records)
    assert findings[0]["detail"]["last_station"] == "Westland Center"
    reached = arrival(pilot(100, "127.100", "Westland Center", at=(51.5, 0.0)), pilot(500, "118.700", "Westfield Tower",
                                                                                         at=(51.1, 0.0)))  # fmt: skip
    assert kinds(reached) == []
    # An airfield with no controller: the pilot's calls go to other traffic, and that is all there is.
    uncontrolled = arrival(pilot(500, "122.800", "Westfield Traffic", at=(51.1, 0.0)))
    assert kinds(uncontrolled) == []


def test_approach_first_reached_inside_fifteen_miles_is_reported():
    def locate(ident):
        return DESTINATION if ident == "EGXX" else None

    plan = {"t": T0 - 10, "type": "plan", "plan": {"origin": "EGYY", "destination": "EGXX"}}
    late = [plan, *flying(60, at=FAR), pilot(300, "125.050", "Eastland Approach", at=(51.17, 0.0))]
    [finding] = check(late, locate)
    assert finding["kind"] == "late_approach" and finding["detail"]["distance_nm"] == 10.2
    assert finding["text"] == "The first call to approach was only 10 NM from the destination."
    early = [plan, *flying(60, at=FAR), pilot(300, "125.050", "Eastland Approach", at=(51.5, 0.0))]
    assert check(early, locate) == []
    # A circuit near the airport never came from far away: nothing is late about it.
    circuit = [plan, *flying(60, at=(51.1, 0.0)), pilot(300, "125.050", "Eastland Approach", at=(51.1, 0.0))]
    assert check(circuit, locate) == []
    # Without the airport's position and without a landing, distance is not judged at all.
    assert check(late) == []


# ---- the report ----------------------------------------------------------------------------


def test_errors_are_listed_once_with_how_many_there_were():
    records = [{"t": T0 + t, "type": "error", "text": text} for t, text in ((0, "tuning failed"), (5, "model timed out"))]
    [finding] = check(records)
    assert finding["text"] == "The app reported 2 errors." and finding["detail"]["others"] == "model timed out"


def test_the_report_is_grouped_by_kind_and_says_when_there_is_nothing():
    assert as_text([]) == "Flight health: nothing to report.\n"
    assert check([]) == [] and check([{"type": "trace"}, "garbage"]) == []
    records = [
        atc(0, "127.100", "Westland Center", "unable, say again your request", fallback=True),
        atc(60, "127.100", "Westland Center", "unable, say again your request", fallback=True),
        {"t": T0 + 5, "type": "error", "text": "tuning failed"},
    ]
    text = as_text(check(records))
    assert text.startswith("Flight health: 2 findings\n")
    assert text.index("Controller had no answer (1)") < text.index("Errors (1)")
    assert "            count: 2" in text
    [shown, _] = for_app(check(records))
    assert shown["title"] == "Controller had no answer" and shown["detail"].startswith("count: 2; times: ")
    assert set(shown) == {"time", "kind", "title", "text", "detail"}


def test_the_debrief_carries_the_health_report_and_writes_it_into_the_journal(tmp_path, monkeypatch):
    monkeypatch.setattr("proatc.voice_loop.TRACE_EVERY_S", 3600)
    session, *_ = session_with_journal(tmp_path, monkeypatch)
    session._start_journal()
    for _ in range(2):
        session.emit({"type": "atc", "station": "Testfield Ground", "text": f"{CALLSIGN}, unable, say again your request",
                      "unprompted": False, "fallback": True})  # fmt: skip
    result = session.debrief()
    [shown] = result["health"]
    assert shown["kind"] == "dead_end" and "2 times" in shown["text"]
    note = lines(session.log.journal)[-1]
    assert note["type"] == "health" and note["findings"][0]["kind"] == "dead_end"
    assert json.dumps(note).count(CALLSIGN) == 0  # the report says what happened, not who flew
    # A session that never connected still gets a debrief, with nothing on its health.
    quiet = voice_loop.Session(Config(), emit=lambda event: None)
    assert quiet.debrief()["health"] == []
