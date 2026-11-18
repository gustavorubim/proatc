"""Every question a controller asks has an answer the pilot can give: a button, a rule for it, and for a yes or
no question both. The instructor's hint and the Phraseology button answer what was asked. The language model is
stood in for."""

import copy
import json

import pytest

from proatc import phraseology as say
from proatc import phrasing, questions, voice_loop
from proatc.atis import AtisBook
from proatc.bench import UNDERSTOOD
from proatc.config import Config
from proatc.engine import Reply
from proatc.flightplan import FlightPlan
from proatc.intents import INTENTS, ITEMS, Intent, parse_json
from proatc.llm import LlmError
from proatc.voice_loop import Session
from tests.deadends import ISSUE, PLAN, at, build_world, read_back
from tests.test_answers import climbing_with_departure, vectored
from tests.test_procedures import departure
from tests.test_radio_loop import FakeBridge

CALLSIGN = "Golf Alfa Bravo Charlie Delta"
ASKED = f"{CALLSIGN}, you appear to be seven miles off the departure track, confirm you are following the departure"
OFF = {**at(51.12, -1.45, 7000), "ias_kt": 250}  # seven miles north of the West two Alfa departure


@pytest.fixture
def world(tmp_path):
    return build_world(tmp_path)


def off_track(tmp_path):
    """The flight from a real flight: departure sees it off the SID and asks, as it did."""
    engine, radar = departure(tmp_path)
    clock = [0.0]
    engine.now = lambda: clock[0]
    engine.flight.on_track.add(engine.flight.sid)  # it had been on the departure: a track is watched once joined
    assert engine.tick(radar, ISSUE, OFF) is None
    clock[0] = 50.0
    assert engine.tick(radar, ISSUE, OFF).text == ASKED
    return engine, radar, clock


def offered(engine, station, raw, issue=ISSUE):
    return {item["label"]: item for item in engine.suggestions(station, issue, raw)}


def click(engine, station, raw, label, issue=ISSUE):
    """The pilot clicks a suggested call, as the app sends it: its intent, no language model."""
    call = offered(engine, station, raw, issue)[label]
    return engine.handle(Intent.from_dict(call["intent"]), station, issue, raw)


# ---- the off-track call from a real flight --------------------------------------------------


def test_the_off_track_call_is_answered_with_the_suggested_button_and_the_controller_says_roger(tmp_path):
    engine, radar, clock = off_track(tmp_path)
    question = engine.flight.question
    assert (question.kind, question.station, question.data) == ("off_track", radar.key, {"track": "departure"})
    calls = engine.suggestions(radar, ISSUE, OFF)
    assert [call["label"] for call in calls[:3]] == ["Affirm", "Request vectors", "Request direct WCOM"]
    assert calls[0]["primary"] and all(call.get("answers") for call in calls[:3])
    assert calls[0]["text"] == f"{CALLSIGN}, affirm, following the West two Alfa departure"
    assert [call["label"] for call in calls if call.get("primary")] == ["Affirm"]

    assert click(engine, radar, OFF, "Affirm").text == f"{CALLSIGN}, roger"
    assert engine.flight.question is None
    assert not any(call.get("answers") for call in engine.suggestions(radar, ISSUE, OFF))
    clock[0] = 500.0
    assert engine.tick(radar, ISSUE, OFF) is None  # the track is not called again


def test_a_negative_to_the_off_track_call_gets_a_way_back_to_the_departure(tmp_path):
    engine, radar, _ = off_track(tmp_path)
    saved = copy.deepcopy(engine.flight)

    direct = engine.handle(Intent("negative"), radar, ISSUE, OFF)
    assert direct.text == f"{CALLSIGN}, roger, route direct Wcom"  # the next waypoint of the SID ahead
    assert engine.flight.direct_to == "WCOM" and engine.flight.question is None

    engine.flight = copy.deepcopy(saved)
    assert click(engine, radar, OFF, "Request direct WCOM").text == f"{CALLSIGN}, roger, route direct Wcom"

    engine.flight = copy.deepcopy(saved)
    vectors = click(engine, radar, OFF, "Request vectors")
    assert vectors.text == (
        f"{CALLSIGN}, roger, turn left heading two two zero, vectors to rejoin the departure, direct Wcom when able"
    )
    assert vectors.expect == {"heading": 220} and engine.flight.pending is vectors
    # Read back like any instruction, with the button the app offers.
    calls = engine.suggestions(radar, ISSUE, OFF)
    assert calls[0]["label"] == "Read back" and calls[0]["primary"]
    assert engine.handle(Intent.from_dict(calls[0]["intent"]), radar, ISSUE, OFF).text == ""

    engine.flight = copy.deepcopy(saved)
    assert engine.handle(Intent("unable"), radar, ISSUE, OFF).text == f"{CALLSIGN}, roger, route direct Wcom"


def test_a_flight_off_its_filed_route_is_sent_back_to_the_next_waypoint_on_it(tmp_path):
    from tests.test_airways import make

    engine, clock = make(tmp_path)
    centre = engine.centers.on_frequency(127500, 51.0, 0.0)
    engine.flight.contacted.add(centre.key)
    adrift = at(51.55, -0.2, 36000, heading=90)
    engine.flight.on_track.add("route")  # it had been on its route
    engine.tick(centre, None, adrift)
    clock[0] = 130.0
    assert engine.tick(centre, None, adrift).text.endswith("off your filed route, confirm your routing")
    calls = offered(engine, centre, adrift, None)
    assert calls["Affirm"]["text"] == f"{CALLSIGN}, affirm, routing as filed" and calls["Affirm"]["primary"]
    assert calls["Request vectors"]["text"] == f"{CALLSIGN}, negative, request vectors to rejoin your route"
    back = engine.handle(Intent("negative"), centre, None, adrift)
    assert back.text.startswith(f"{CALLSIGN}, roger, route direct ") and engine.flight.direct_to


def test_the_off_track_question_has_a_way_back_without_any_waypoint_known(world):
    engine, departure_radar, raw = climbing_with_departure(world)
    asked = Reply(f"{CALLSIGN}, you appear to be six miles off the departure track, confirm you are following the departure")
    engine.flight.last_said[departure_radar.key] = asked.text
    questions.record(engine, departure_radar.key, asked)
    # No navigation data in this world: no waypoint to send the flight to, so the controller asks what it wants.
    assert engine.handle(Intent("negative"), departure_radar, ISSUE, raw).text == f"{CALLSIGN}, roger, say intentions"
    assert engine.flight.question.kind == "intentions"


# ---- every other question ------------------------------------------------------------------


@pytest.mark.parametrize(
    "said, kind",
    [
        ("you appear to be one five miles off your filed route, confirm your routing", "off_track"),
        ("roger mayday, squawk seven seven zero zero, say intentions", "intentions"),
        ("roger, landing clearance cancelled, say intentions", "intentions"),
        ("the weather is below V F R minima, special V F R is available, say intentions", "intentions"),
        ("Westtown is I F R, say intentions", "intentions"),
        ("low altitude alert, check your altitude immediately, minimum safe altitude two thousand feet", "low_altitude"),
        ("check your level, cleared flight level one two zero", "level_bust"),
        ("check your altitude, cleared one two thousand", "level_bust"),
        ("roger, maintain flight level one one zero, advise when able", "able_level"),
        ("roger, continue present heading, advise when able to turn", "able_heading"),
        ("roger, advise when able", "able"),
        ("hold at Lambourne as published, maintain flight level one zero zero, advise when ready for the approach",
         "ready_approach"),
        ("readback correct, report ready for departure", "ready_departure"),
        ("roger, advise ready", "ready_departure"),
        ("roger, report ready", "ready_departure"),
        ("cleared I L S approach runway two seven, report established", "established"),
        ("say destination", "destination"),
        ("roger, say the four letter code of the airport you are diverting to", "destination"),
        ("say the runway you request", "runway"),
        ("join left hand downwind runway two seven, Q N H one zero one three, report downwind", "report"),
        ("number one, report final runway two seven", "report"),
        ("make straight in, report three mile final", "report"),
        ("cleared to transit the control zone, V F R, not above altitude two thousand feet, report leaving", "report"),
        ("deviation left of track approved, report clear of weather", "report"),
        ("approved, report back on frequency", "report"),
        ("climb flight level three six zero, report reaching", "report"),
        ("roger, maintain flight level three five zero, report Bravo", "report"),
    ],
)  # fmt: skip
def test_every_question_the_engine_asks_is_recognised(said, kind):
    assert questions.classify(f"{CALLSIGN}, {said}")[0] == kind


def test_instructions_that_ask_nothing_open_no_question():
    for said in ("climb flight level one two zero", "contact Eastfield Approach one two four decimal three",
                 "you were to cross Wmid at or above altitude five thousand feet, expedite climb", "reading you five"):  # fmt: skip
        assert questions.classify(f"{CALLSIGN}, {said}") is None


def ask(engine, station, words):
    """The controller asks, as the engine records anything it says."""
    reply = Reply(f"{CALLSIGN}, {words}")
    engine.flight.last_said[station.key] = reply.text
    questions.record(engine, station.key, reply)
    return reply


def test_a_level_bust_is_answered_correcting_or_unable(world):
    engine, radar, raw = climbing_with_departure(world)
    bust = {**raw, "elevation_m": 6000 * 0.3048, "vs_fpm": 1500}
    engine.flight.cleared_ft = 5000
    called = engine.tick(radar, ISSUE, bust)
    assert called.text == f"{CALLSIGN}, check your level, cleared altitude five thousand feet"
    calls = engine.suggestions(radar, ISSUE, bust)
    assert calls[0]["label"] == "Correcting" and calls[0]["primary"]
    assert calls[0]["text"] == f"{CALLSIGN}, correcting, descending altitude five thousand feet"
    saved = copy.deepcopy(engine.flight)
    assert click(engine, radar, bust, "Correcting").text == f"{CALLSIGN}, roger"
    engine.flight = saved
    unable = engine.handle(Intent("unable"), radar, ISSUE, bust)
    assert unable.text == f"{CALLSIGN}, roger, maintain altitude six thousand feet, advise when able"
    assert engine.flight.cleared_ft == 6000 and unable.expect == {"altitude": 6000}


def test_able_now_gives_back_the_level_or_the_heading_that_could_not_be_flown(world):
    engine, radar, raw = climbing_with_departure(world)
    engine.handle(Intent("request_climb", altitude=9000), radar, ISSUE, raw)
    assert engine.flight.pending.expect == {"altitude": 9000}
    held = engine.handle(Intent("unable"), radar, ISSUE, raw)
    assert held.text == f"{CALLSIGN}, roger, maintain altitude three thousand feet, advise when able"
    read_back(engine, radar, raw)
    calls = engine.suggestions(radar, ISSUE, raw)
    assert (calls[0]["label"], calls[0]["primary"]) == ("Able now", True)
    assert calls[0]["text"] == f"{CALLSIGN}, now able flight level zero niner zero"
    again = click(engine, radar, raw, "Able now")
    assert again.text == f"{CALLSIGN}, climb flight level zero niner zero" and again.expect == {"altitude": 9000}

    engine, approach, raw = vectored(world)
    heading = engine.flight.arrival.heading
    vector = Reply(f"{CALLSIGN}, turn left heading zero seven zero", {"heading": heading})
    engine._said(vector, approach.key)  # a heading the pilot has not read back yet
    turned = engine.handle(Intent("unable"), approach, ISSUE, raw)
    assert turned.text.endswith("continue present heading, advise when able to turn")
    assert engine.flight.question.data == {"heading": heading}
    back = engine.handle(Intent("affirm"), approach, ISSUE, raw)
    assert back.expect == {"heading": heading} and engine.flight.arrival.heading == heading


def test_say_intentions_after_an_emergency_offers_the_ways_on(world):
    plan = FlightPlan("EGXX", "LFXX", PLAN.route, 36000, alternate="EGXX")
    engine = world.engine()
    engine.set_plan(plan)
    engine.flight.origin_ident = "EGXX"
    radar = world.station("EGXX", "departure")
    raw = at(51.0, -1.12, 3000)
    engine.flight.contacted.add(radar.key)
    asked = engine.handle(Intent("mayday"), radar, ISSUE, raw)
    assert asked.text.endswith("say intentions") and engine.flight.question.kind == "intentions"
    calls = engine.suggestions(radar, ISSUE, raw)
    assert [call["label"] for call in calls[:3]] == ["Return to EGXX", "Divert to EGXX", "Continue to LFXX"]
    saved = copy.deepcopy(engine.flight)
    assert click(engine, radar, raw, "Continue to LFXX").text == (f"{CALLSIGN}, roger, advise if you require further assistance")
    engine.flight = saved
    back = click(engine, radar, raw, "Return to EGXX")
    assert "cleared to Westfield" in back.text and engine.flight.question is None


def test_the_other_questions_put_their_answer_first_and_primary(world):
    engine, approach, raw = vectored(world)
    read_back(engine, approach, raw)
    cases = [
        ("cleared I L S approach runway two seven, report established", "Established", "established"),
        ("hold at Lambourne as published, advise when ready for the approach", "Ready for approach", "cancel_hold"),
        ("low altitude alert, check your altitude immediately", "Climbing", "affirm"),
        ("no instrument approach is available for runway two seven and the weather is below visual minima, say intentions",
         "Divert to…", "request_diversion"),
        ("roger, say the four letter code of the airport you are diverting to", "Divert to…", "request_diversion"),
        ("say the runway you request", "Request runway 09", "request_runway"),
        ("roger, landing clearance cancelled, say intentions", "Going around", "going_around"),
    ]  # fmt: skip
    for words, label, intent in cases:
        ask(engine, approach, words)
        first = engine.suggestions(approach, ISSUE, raw)[0]
        assert (first["label"], first["intent"]["intent"], first.get("primary")) == (label, intent, True), words
    # Once the controller has said something else, the answer is still offered first, no longer primary.
    engine.flight.last_said[approach.key] = f"{CALLSIGN}, traffic one o'clock"
    first = engine.suggestions(approach, ISSUE, raw)[0]
    assert first["label"] == "Going around" and not first.get("primary")


def test_report_ready_and_report_downwind_have_their_calls_marked(world):
    engine = world.engine()
    tower = world.station("EGXX", "tower")
    short = at(51.0006, -0.979, 100, on_ground=True)
    engine.handle(Intent("request_delay"), tower, ISSUE, short)
    calls = engine.suggestions(tower, ISSUE, short)
    assert (calls[0]["label"], calls[0].get("primary")) == ("Ready for departure", True)
    assert [call["label"] for call in calls].count("Ready for departure") == 1  # the button it had, not a second one
    engine.handle(Intent.from_dict(calls[0]["intent"]), tower, ISSUE, short)
    assert engine.flight.question is None

    engine = world.engine()
    arriving = world.station("LFXX", "tower")
    raw = at(51.0, 5.15, 2000, heading=90)
    joined = engine.handle(Intent("inbound", flight_rules="VFR"), arriving, ISSUE, raw)
    assert joined.text.endswith("report downwind") and engine.flight.question.kind == "report"
    read_back(engine, arriving, raw)
    calls = engine.suggestions(arriving, ISSUE, raw)
    assert (calls[0]["label"], calls[0].get("primary")) == ("Downwind", True)


def test_a_question_from_another_station_is_not_answered_here(world):
    engine, radar, raw = climbing_with_departure(world)
    ask(engine, radar, "you appear to be six miles off the departure track, confirm you are following the departure")
    centre = world.centers.on_frequency(127500, 51.0, 0.0)
    assert not any(call.get("answers") for call in engine.suggestions(centre, None, raw))
    assert engine.handle(Intent("affirm"), centre, None, raw).text == ""  # nothing asked there: an acknowledgement
    assert engine.flight.question is not None


def test_a_yes_or_no_with_nothing_asked_is_an_acknowledgement_or_unable(world):
    engine, radar, raw = climbing_with_departure(world)
    assert engine.handle(Intent("affirm"), radar, ISSUE, raw).text == ""
    engine.handle(Intent("request_climb", altitude=9000), radar, ISSUE, raw)
    assert engine.handle(Intent("negative"), radar, ISSUE, raw).text.endswith("advise when able")


# ---- the approach questions from a real flight -------------------------------------------------


def test_which_runway_and_how_to_join_are_answered_from_the_arrival(world):
    engine, approach, raw = vectored(world)
    landing = engine.handle(Intent("request_information", item="runway"), approach, ISSUE, raw)
    assert landing.text == f"{CALLSIGN}, landing runway two seven"
    joining = engine.handle(Intent("request_information", item="transition"), approach, ISSUE, raw)
    assert joining.text == f"{CALLSIGN}, radar vectors for the I L S approach runway two seven"
    # "Request runway assignment" taken as a runway request with none named: the runway is said all the same.
    assigned = engine.handle(Intent("request_runway"), approach, ISSUE, raw)
    assert assigned.text == f"{CALLSIGN}, landing runway two seven, say the runway you request"

    engine.flight.arrival.stage = None
    before = engine.handle(Intent("request_information", item="transition"), approach, ISSUE, raw)
    assert before.text == f"{CALLSIGN}, expect vectors for the I L S approach runway two seven"
    engine.flight.arrival.stage, engine.flight.arrival.via = "procedure", "IAFNE"
    assert engine.handle(Intent("request_information", item="transition"), approach, ISSUE, raw).text == (
        f"{CALLSIGN}, cleared I L S approach runway two seven via Iafne"
    )


def test_how_to_join_is_answered_by_a_centre_before_the_arrival_is_set_up(world):
    engine = world.engine()
    engine.set_plan(PLAN)
    engine.flight.origin_ident, engine.flight.descent_given = "EGXX", True
    east = world.centers.on_frequency(132100, 51.0, 3.0)
    raw = at(51.0, 3.5, 20000, heading=90)
    said = engine.handle(Intent("request_information", item="transition"), east, None, raw).text
    # The centre has no weather for Eastfield: approach, who will have it, is named.
    assert said == f"{CALLSIGN}, Eastland Control, Eastfield Approach one two four decimal three will advise the approach"
    weather = engine.handle(Intent("request_information", item="transition"), world.station("LFXX", "approach"), ISSUE, raw)
    assert weather.text.endswith("expect vectors for the I L S approach runway two seven")


def test_an_initial_fix_is_named_when_the_approach_is_flown_from_one(tmp_path):
    from proatc.procedures import parse_cifp
    from tests.test_procedures import FIXES, STAR
    from tests.test_realism import make

    db, engine, _, _ = make(tmp_path, navfix=FIXES, plan=FlightPlan("EGXX", "LFXX", ["DELTA"], 36000, arrival_runway="27"))
    engine._procedures["LFXX"] = parse_cifp(STAR)
    engine.flight.requested_approach, engine.flight.has_been_airborne = "RNAV", True
    approach = db.station(124300, 51.0, 5.0)
    raw = at(51.3, 6.4, 9000, heading=270)
    engine.handle(Intent("check_in"), approach, ISSUE, raw)
    said = engine.handle(Intent("request_information", item="transition"), approach, ISSUE, raw).text
    assert "Iafne" in said and "R NAV" in said


def test_request_vectors_on_the_approach_gives_a_heading(world):
    engine, approach, raw = vectored(world)
    read_back(engine, approach, raw)
    heading = engine.flight.arrival.heading
    assert engine.handle(Intent("request_vectors"), approach, ISSUE, raw).text == (
        f"{CALLSIGN}, roger, fly heading {say.heading(heading)}"
    )


# ---- the language model sees the new calls ---------------------------------------------------


def test_the_model_is_told_about_yes_no_and_the_join_questions():
    for name in ("affirm", "negative", "request_vectors"):
        assert name in INTENTS
    assert "transition" in ITEMS
    assert parse_json('{"intent": "request_information", "item": "initial fix"}').item == "transition"
    assert parse_json('{"intent": "affirm"}').intent == "affirm"
    cases = {transcript: expected for transcript, _, expected in UNDERSTOOD}
    assert cases["which runway are landing?"] == {"intent": "request_information", "item": "runway"}
    assert cases["request runway assignment"] == {"intent": "request_information", "item": "runway"}
    assert cases["request the transition for the approach, say the transition name and the route"]["item"] == "transition"
    assert cases["affirm, following the departure"] == {"intent": "affirm"}


# ---- the hint, the instructor and the Phraseology button ---------------------------------------


def model(answer):
    asked = []

    def complete(system, user):
        asked.append((system, user))
        return json.dumps(answer)

    complete.asked = asked
    return complete


def test_phraseology_answers_the_question_the_controller_asked(tmp_path):
    engine, radar, _ = off_track(tmp_path)
    calls = engine.suggestions(radar, ISSUE, OFF)
    opening = engine.opening(radar, OFF)
    said = engine.flight.last_said[radar.key]
    # The words from the real flight: the model, told what was asked, picks the answer the engine offers.
    complete = model({"choice": 1})
    result = phrasing.phrase("confirm departure runway and initial clearance", radar.callsign, opening, calls, complete, said)
    assert result == {"text": f"{CALLSIGN}, affirm, following the West two Alfa departure", "intent": {"intent": "affirm"}}
    system, user = complete.asked[0]
    assert f"The controller last said: {ASKED}" in user
    assert "1. Affirm (answers the controller): " in user and "2. Request vectors (answers the controller)" in user
    assert "the call is that answer" in system and "(answers the controller)" in system
    # Without a model, a plain yes or no is the matching answer.
    assert phrasing.phrase("yes", "x", opening, calls)["intent"] == {"intent": "affirm"}
    assert phrasing.phrase("no, I lost it", "x", opening, calls)["intent"] == {"intent": "request_vectors"}
    assert phrasing.phrase("yes", "x", opening, calls, model({"choice": 1}))["intent"] == {"intent": "affirm"}


def test_the_hint_after_an_unforeseen_call_is_the_answer_to_the_question(tmp_path):
    engine, radar, _ = off_track(tmp_path)

    class Model:
        histories: dict = {}  # noqa: RUF012

        def complete(self, *args, **kwargs):
            raise LlmError("no model")

        def reply(self, transcript, context, station_key="free", inform_only=False):
            return f"{CALLSIGN}, say again"

        def remember(self, *args):
            pass

    context: dict = {}
    events: list[dict] = []
    voice_loop._answer("what", Intent("other"), context, radar.key, radar, ISSUE, OFF, Model(), engine, None, events.append)
    assert context["way_out"] == f"{CALLSIGN}, affirm, following the West two Alfa departure"
    assert voice_loop.what_to_say(engine.suggestions(radar, ISSUE, OFF))[0]["label"] == "Affirm"


def test_the_instructor_says_what_was_asked_and_how_to_answer(tmp_path):
    engine, radar, _ = off_track(tmp_path)
    events: list[dict] = []
    session = Session(Config(), emit=events.append)
    session.bridge = FakeBridge({**OFF, "com1_khz": 125500, "zulu_seconds": 43200.0})
    session.db, session.book, session.engine, session.ready = engine.db, AtisBook(), engine, True
    session._instructor("what_next")
    assert events[-1]["type"] == "coach"
    assert events[-1]["text"] == (
        f"The controller asked: “{ASKED}”. You could answer: “{CALLSIGN}, affirm, following the West two Alfa departure”"
    )

    class Model:
        def __init__(self):
            self.asked = []

        def complete(self, system, user, max_tokens=None, temperature=0.3):
            self.asked.append(user)
            return json.dumps({"choice": 2})

    session.controller = Model()
    session._phrase("no, what heading do I need")
    said = events[-1]
    assert said["type"] == "phrase" and said["intent"] == {"intent": "request_vectors"}
    assert f"The controller last said: {ASKED}" in session.controller.asked[0]
