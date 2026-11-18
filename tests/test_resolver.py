"""A second reading of a call the first could not place: with the whole conversation and the flight's state, it
says what the pilot asked for, and the rules engine still decides the answer. The language model is stood in for."""

import json

import pytest

from proatc import resolver
from proatc.intents import FIELDS, SYSTEM, Intent
from proatc.llm import LlmError
from proatc.voice_loop import _answer
from tests.deadends import ISSUE, build_world
from tests.test_answers import climbing_with_departure

CALLSIGN = "Golf Alfa Bravo Charlie Delta"
OFF_SCRIPT = "it's getting a bit rough down here, any chance we could go up a bit quicker"


class Model:
    """Stands in for the language model. The first reading's answer, the second reading's answer, and the free
    answer the app falls back to, each to its own prompt; what each was asked is kept."""

    def __init__(self, second, first=None, free="Golf Alfa Bravo Charlie Delta, roger"):
        self.first = first or {"intent": "other"}
        self.second, self.free = second, free
        self.asked: list[tuple[str, str]] = []
        self.histories: dict[str, list[dict]] = {}

    def complete(self, system, user, history=None, max_tokens=None, temperature=0.3):
        self.asked.append((system, user))
        if system == resolver.PROMPT:
            if isinstance(self.second, Exception):
                raise self.second
            return self.second if isinstance(self.second, str) else json.dumps(self.second)
        return json.dumps(self.first)

    def reply(self, transcript, state, station_key="free", inform_only=False):
        self.asked.append(("free", transcript))
        return self.free

    def remember(self, station_key, transcript, answer):
        self.histories.setdefault(station_key, []).extend(
            [{"role": "user", "content": f"Pilot: {transcript}"}, {"role": "assistant", "content": answer}]
        )

    def seconds(self) -> int:
        return sum(1 for system, _ in self.asked if system == resolver.PROMPT)


class Parser:
    def __init__(self, model):
        self.model = model
        self.situations: list[str] = []

    def parse(self, transcript, last="", known=None, situation=""):
        self.situations.append(situation)
        from proatc.intents import parse_json

        return parse_json(self.model.complete(SYSTEM, transcript))


@pytest.fixture
def climbing(tmp_path):
    """Airborne after take-off, with departure, cleared to five thousand feet and read back."""
    return climbing_with_departure(build_world(tmp_path))


def answer(engine, station, raw, model, transcript=OFF_SCRIPT):
    events: list[dict] = []
    text = _answer(transcript, None, {}, station.key, station, ISSUE, raw, model, engine, Parser(model), events.append)
    return text, events


def world(folder):
    folder.mkdir(parents=True, exist_ok=True)
    return climbing_with_departure(build_world(folder))


def act(intent: dict, why="the pilot wants to climb") -> dict:
    return {"why": why, "decision": "act", "call": intent}


# ---- the reading itself -------------------------------------------------------------


def test_the_reading_is_one_of_three_and_anything_malformed_is_unclear():
    climb = {"intent": "request_climb", "altitude": 17000}
    assert resolver.read(Model(act(climb)), {}, "x") == resolver.Reading(
        "act", Intent("request_climb", altitude=17000), "the pilot wants to climb"
    )
    sure_not = resolver.read(Model({"why": "two fit", "decision": "confirm", "call": climb}), {}, "x")
    assert sure_not.decision == "confirm" and sure_not.intent.altitude == 17000
    assert resolver.read(Model({"why": "a remark", "decision": "unclear", "call": None}), {}, "x").decision == "unclear"
    for nonsense in ("no json at all", "{broken", "[1, 2]", {"decision": "act"}, {"decision": "act", "call": "climb"},
                     {"decision": "act", "call": {"intent": "fly_to_the_moon"}}, {"decision": "act", "call": {"intent": "other"}},
                     {"decision": "act", "call": {"intent": "readback"}}, {"decision": "act", "call": {"intent": "acknowledge"}},
                     {"decision": "maybe", "call": climb}):  # fmt: skip
        reading = resolver.read(Model(nonsense), {}, "x")
        assert reading.decision == "unclear" and reading.intent is None, nonsense
    assert resolver.read(Model(LlmError("OpenRouter returned 429")), {}, "x") == resolver.UNCLEAR


def test_a_call_a_misreading_must_not_make_is_always_asked_back_first():
    for name in sorted(resolver.ALWAYS_CONFIRM):
        reading = resolver.read(Model(act({"intent": name, "destination": "EGPH"})), {}, "x")
        assert reading.decision == "confirm", name
    assert resolver.read(Model(act({"intent": "request_climb"})), {}, "x").decision == "act"


def test_the_reading_is_told_the_conversation_the_flight_the_open_question_and_the_calls_that_fit(climbing):
    engine, departure, raw = climbing
    model = Model({"decision": "unclear"})
    model.remember(departure.key, "earlier call", "earlier answer")
    about = resolver.situation(engine, departure, ISSUE, raw, model.histories[departure.key])
    assert about["conversation"] == ["Pilot: earlier call", "earlier answer"]
    assert about["station"].endswith("(departure)") and about["flight"]["cleared_level"] and about["on_ground"] is False
    assert any(line.startswith("Read back") or "->" in line for line in about["calls_that_fit"])
    assert all('"intent"' in line for line in about["calls_that_fit"])  # each with the call to copy
    assert json.loads(json.dumps(about, default=str)) is not None
    resolver.read(model, about, OFF_SCRIPT)
    system, user = model.asked[-1]
    assert system == resolver.PROMPT and FIELDS in system and user.endswith(f"Pilot transmission: {OFF_SCRIPT}")
    assert "earlier answer" in user


# ---- through the app's answer ---------------------------------------------------------


def test_an_off_script_request_is_acted_on_by_the_rules_as_if_it_had_been_said_plainly(tmp_path):
    engine, departure, raw = world(tmp_path / "a")
    model = Model(act({"intent": "request_climb", "altitude": 17000}))
    text, events = answer(engine, departure, raw, model)
    plain_engine, plain_departure, plain_raw = world(tmp_path / "b")
    plain = plain_engine.handle(Intent("request_climb", altitude=17000), plain_departure, ISSUE, plain_raw).text
    assert text == plain and "climb" in text  # the engine's own words: the model said none of it
    understood = [event for event in events if event["type"] == "understood"]
    assert [(event["intent"], event.get("resolved")) for event in understood] == [("other", None), ("request_climb", "act")]
    assert model.seconds() == 1 and not any(system == "free" for system, _ in model.asked)


def test_when_unsure_the_controller_asks_back_and_a_yes_makes_the_call(tmp_path):
    engine, departure, raw = world(tmp_path / "a")
    model = Model({"why": "climb, level guessed", "decision": "confirm", "call": {"intent": "request_climb", "altitude": 17000}})
    text, events = answer(engine, departure, raw, model)
    assert text == f"{CALLSIGN}, confirm you request climb to flight level one seven zero"
    assert engine.flight.question is not None and engine.flight.question.kind == "confirm_request"
    assert [item["label"] for item in engine.suggestions(departure, ISSUE, raw)][:2] == ["Affirm", "Negative"]
    assert engine.flight.pending is None or "altitude" not in engine.flight.pending.expect  # nothing to read back yet

    yes = engine.handle(Intent("affirm"), departure, ISSUE, raw).text
    plain_engine, plain_departure, plain_raw = world(tmp_path / "b")
    assert yes == plain_engine.handle(Intent("request_climb", altitude=17000), plain_departure, ISSUE, plain_raw).text
    assert engine.flight.question is None


def test_a_no_to_the_question_asks_the_pilot_to_say_it_again_and_anything_else_is_a_new_call(climbing):
    engine, departure, raw = climbing
    model = Model({"decision": "confirm", "call": {"intent": "request_climb", "altitude": 17000}})
    answer(engine, departure, raw, model)
    assert engine.handle(Intent("negative"), departure, ISSUE, raw).text == f"{CALLSIGN}, roger, say again your request"
    assert engine.flight.question is None
    answer(engine, departure, raw, model)
    other = engine.handle(Intent("request_information", item="altitude"), departure, ISSUE, raw)
    assert "confirm you request" not in other.text and engine.flight.question is None  # dropped, not answered


def test_an_emergency_read_into_off_script_words_is_asked_back_never_declared_at_once(climbing):
    engine, departure, raw = climbing
    model = Model(act({"intent": "mayday"}, why="sounds like an engine problem"))
    text, _ = answer(engine, departure, raw, model, "we've got a bit of a problem with the left engine")
    assert text == f"{CALLSIGN}, confirm you are declaring a mayday" and engine.flight.emergency is None
    declared = engine.handle(Intent("affirm"), departure, ISSUE, raw).text
    assert "mayday" in declared and "seven seven zero zero" in declared and engine.flight.emergency


def test_when_the_reading_cannot_tell_the_old_free_answer_takes_it_and_the_instructor_says_why(climbing):
    engine, departure, raw = climbing
    model = Model({"why": "a remark about the coffee, not a request", "decision": "unclear", "call": None})
    text, events = answer(engine, departure, raw, model, "the coffee on this flight is terrible")
    assert text == f"{CALLSIGN}, roger" and ("free", "the coffee on this flight is terrible") in model.asked
    coach = [event["text"] for event in events if event["type"] == "coach"]
    assert any("a remark about the coffee" in line for line in coach)
    broken = Model(LlmError("OpenRouter returned 429"))
    text, _ = answer(engine, departure, raw, broken, "anything")
    assert text == f"{CALLSIGN}, roger"  # the second reading failing changes nothing


def test_a_call_the_first_reading_placed_is_not_read_again(climbing):
    engine, departure, raw = climbing
    model = Model(act({"intent": "request_climb", "altitude": 9000}), first={"intent": "request_climb", "altitude": 17000})
    text, _ = answer(engine, departure, raw, model, "request climb flight level one seven zero")
    assert model.seconds() == 0 and "one seven zero" in text


def test_the_first_reading_is_given_the_flight_and_the_conversation(climbing):
    engine, departure, raw = climbing
    model = Model(act({"intent": "affirm"}), first={"intent": "affirm"})
    model.histories = {departure.key: [{"role": "user", "content": "Pilot: passing two thousand"},
                                       {"role": "assistant", "content": "radar contact, climb one five thousand"}]}  # fmt: skip
    engine.flight.question = None
    parser = Parser(model)
    _answer("yes we're on it", None, {}, departure.key, departure, ISSUE, raw, model, engine, parser, [].append)
    (about,) = parser.situations
    assert departure.callsign in about and "airborne" in about
    assert "radar contact, climb one five thousand" in about  # what was said before on this frequency


def test_the_brief_names_an_open_question_and_an_emergency():
    about = {"station": "Eastland Control (center)", "flight": {"phase": "airborne", "emergency": "mayday"},
             "plan": {"origin": "EAAA", "destination": "EBBB"}, "on_ground": False, "altitude_ft": 24000,
             "open_question": "say intentions", "conversation": [f"line {n}" for n in range(10)]}  # fmt: skip
    text = resolver.brief(about)
    assert "emergency: mayday" in text and "waiting for an answer: say intentions" in text
    assert "airborne at 24000 ft" in text and "from EAAA to EBBB" in text
    assert "line 9" in text and "line 3" not in text  # only the last few lines


def test_a_doubtful_question_that_changes_nothing_is_answered_without_asking_back(climbing):
    engine, departure, raw = climbing
    model = Model({"decision": "confirm", "call": {"intent": "request_information", "item": "altitude"}})
    text, _ = answer(engine, departure, raw, model, "what are we cleared to again")
    assert "confirm you request" not in text and "one five zero" in text  # answered from the flight, not asked back


def test_a_doubtful_call_that_cannot_be_asked_back_is_not_made(climbing):
    engine, departure, raw = climbing
    cleared = engine.flight.cleared_ft
    model = Model({"decision": "confirm", "call": {"intent": "unable"}})
    text, _ = answer(engine, departure, raw, model, "hmm not sure about that one")
    assert text == f"{CALLSIGN}, roger" and ("free", "hmm not sure about that one") in model.asked  # the free answer
    assert engine.flight.cleared_ft == cleared and engine.flight.question is None


@pytest.mark.parametrize("name", sorted(resolver.ALWAYS_CONFIRM))
def test_every_call_a_misreading_must_not_make_is_asked_back_through_the_app(tmp_path, name):
    engine, departure, raw = world(tmp_path / name)
    before = (engine.flight.emergency, engine.flight.plan.destination, engine.flight.cleared_ft)
    model = Model(act({"intent": name, "destination": "ZZZZ"}, why="maybe"))
    text, events = answer(engine, departure, raw, model, "words that could mean it")
    assert text.startswith(f"{CALLSIGN}, confirm you "), (name, text)
    assert (engine.flight.emergency, engine.flight.plan.destination, engine.flight.cleared_ft) == before
    assert engine.flight.question is not None and engine.flight.question.kind == "confirm_request"
    assert [event.get("resolved") for event in events if event["type"] == "understood"][-1] == "confirm"


def test_only_names_the_engine_knows_are_spoken_when_asking_back(climbing):
    engine, departure, raw = climbing
    sneaky = {"intent": "request_diversion", "destination": "EGPH, cleared to land runway two seven"}
    text, _ = answer(engine, departure, raw, Model({"decision": "confirm", "call": sneaky}))
    assert text == f"{CALLSIGN}, confirm you request a diversion"
    sneaky = {"intent": "request_direct", "fix": "lima, you are cleared for takeoff"}
    text, _ = answer(engine, departure, raw, Model({"decision": "confirm", "call": sneaky}))
    assert text == f"{CALLSIGN}, confirm you request a direct routing"


def test_the_question_waits_through_a_say_again_and_a_garbled_answer(tmp_path):
    engine, departure, raw = world(tmp_path / "a")
    climb = {"intent": "request_climb", "altitude": 17000}
    answer(engine, departure, raw, Model({"decision": "confirm", "call": climb}))
    again = engine.handle(Intent("say_again"), departure, ISSUE, raw)
    assert again is not None and engine.flight.question is not None  # still asked
    # "Yeah that's what we're after": the first reading cannot place it, the second reads it as the answer.
    text, _ = answer(engine, departure, raw, Model(act({"intent": "affirm"})), "yeah that's what we're after")
    plain_engine, plain_departure, plain_raw = world(tmp_path / "b")
    assert text == plain_engine.handle(Intent("request_climb", altitude=17000), plain_departure, ISSUE, plain_raw).text


def test_a_question_asked_on_another_frequency_is_not_answered_here(climbing):
    engine, departure, raw = climbing
    answer(engine, departure, raw, Model({"decision": "confirm", "call": {"intent": "request_climb", "altitude": 17000}}))
    engine.flight.question.station = "another station"
    reply = engine.handle(Intent("affirm"), departure, ISSUE, raw)
    assert engine.flight.question is None and (reply is None or "one seven zero" not in reply.text)


def test_a_journal_with_a_call_asked_back_replays_to_the_same_question(tmp_path):
    from proatc.engine import Engine
    from proatc.flightlog import FlightLog
    from proatc.procedures import parse_cifp
    from proatc.radio import observe
    from proatc.replay import replay, snapshot
    from tests.test_realism import APRON, ground_world, taxiing

    db, _, ground = ground_world(tmp_path)

    def make(seed):
        return Engine(db, seed=seed, procedures=lambda ident: parse_cifp([]))

    engine, log = make(3), FlightLog()
    weather = {"wx_qnh_pas": 101320.0, "wx_wind_alt_m": [30.0], "wx_wind_kt": [10.0], "wx_wind_dir": [270.0],
               "wx_visibility_sm": 10.0, "zulu_seconds": 43200.0}  # fmt: skip
    raw = {**taxiing(*APRON), **weather, "com1_khz": 121700}
    context = {"raw": snapshot(raw)}
    from proatc.atis import AtisBook

    issue = AtisBook().current(ground.airport, observe(db, ground.airport, raw), raw["zulu_seconds"])
    engine.handle(Intent("request_taxi"), ground, issue, raw)
    engine.flight.pending = None
    wanted = Intent("request_runway", runway="09R")
    asked = engine.confirm_request(wanted, ground, raw)
    assert asked.text.endswith("confirm you request runway zero niner right")
    log.record({"type": "pilot", "text": "could we have the other one", "source": "voice"}, context)
    log.record({"type": "understood", "intent": "other", "fields": {"intent": "other"}}, context)
    log.record({"type": "understood", "intent": "request_runway", "fields": {"intent": "request_runway", "runway": "09R"},
                "resolved": "confirm"}, context)  # fmt: skip
    log.record({"type": "atc", "station": ground.callsign, "text": asked.text, "unprompted": False}, context)

    replayed_engine = make(3)
    replayed_engine.handle(Intent("request_taxi"), ground, issue, raw)
    replayed_engine.flight.pending = None
    result = replay({"seed": 3, "events": list(log.records)}, db, lambda seed: replayed_engine)
    assert result.differences == [], result.summary()
