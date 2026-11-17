"""What the pilot wants, typed in their own words, becomes the proper call. The language model is stood in for."""

import json
import threading

from proatc import config as config_module
from proatc import phrasing
from proatc.atis import AtisBook
from proatc.config import Config
from proatc.intents import Intent
from proatc.llm import LlmError
from proatc.server import AppServer
from proatc.voice_loop import Session
from tests.test_ifr import ISSUE
from tests.test_radio_loop import FakeBridge
from tests.test_realism import APRON, ground_world, taxiing

OPENING = "Testfield Ground, Golf Alfa Bravo Charlie Delta, "
CALLS = [
    {"label": "Request taxi", "text": f"{OPENING}request taxi", "intent": {"intent": "request_taxi"}},
    {"label": "Radio check", "text": f"{OPENING}radio check", "intent": {"intent": "radio_check"}},
    {"label": "Flight following", "text": f"{OPENING}request flight following to {{destination}}",
     "intent": {"intent": "request_flight_following"}, "needs": "destination"},
]  # fmt: skip


def model(answer):
    asked = []

    def complete(system, user):
        asked.append((system, user))
        return answer if isinstance(answer, str) else json.dumps(answer)

    complete.asked = asked
    return complete


def test_a_listed_call_is_chosen_with_its_intent():
    complete = model({"choice": 1})
    result = phrasing.phrase("  i want to drive to the runway  ", "Testfield Ground", OPENING, CALLS, complete)
    assert result == {"text": f"{OPENING}request taxi", "intent": {"intent": "request_taxi"}}
    system, user = complete.asked[0]
    assert system == phrasing.PHRASER and "1. Request taxi: " in user and "3. Flight following" in user
    assert user.endswith("The pilot wants: i want to drive to the runway") and "Station: Testfield Ground" in user
    # The model's reply may come wrapped in words or a code block.
    wrapped = phrasing.phrase("is my radio ok", "x", OPENING, CALLS, model('Sure:\n```json\n{"choice": 2}\n```'))
    assert wrapped["intent"] == {"intent": "radio_check"}


def test_a_call_that_needs_a_place_gets_it_or_asks_for_it():
    there = phrasing.phrase("radar service to Manchester", "x", OPENING, CALLS, model({"choice": 3, "destination": "Manchester"}))
    assert there["text"] == f"{OPENING}request flight following to Manchester"
    assert there["intent"] == {"intent": "request_flight_following", "destination": "Manchester"}
    assert CALLS[2]["intent"] == {"intent": "request_flight_following"}  # the engine's own list is not changed
    nowhere = phrasing.phrase("radar service", "x", OPENING, CALLS, model({"choice": 3, "destination": ""}))
    assert "text" not in nowhere and "where to" in nowhere["note"]


def test_a_request_not_on_the_list_is_worded_and_sent_as_typed():
    result = phrasing.phrase(
        "can I climb, it is bumpy",
        "x",
        OPENING,
        CALLS,
        model({"call": ' "Request climb flight level one two zero due turbulence." '}),
    )
    assert result == {"text": f"{OPENING}request climb flight level one two zero due turbulence", "intent": None}
    assert phrasing.phrase("x", "x", OPENING, CALLS, model({"call": "QNH check"}))["text"] == f"{OPENING}QNH check"
    # The model was told to leave out the station and the callsign; if it does not, they are not said twice.
    twice = model({"call": "testfield ground, Golf Alfa Bravo Charlie Delta, Testfield Ground, request runway two seven"})
    assert phrasing.phrase("x", "x", OPENING, CALLS, twice)["text"] == f"{OPENING}request runway two seven"
    assert phrasing.phrase("x", "x", OPENING, CALLS, model({"call": "Testfield Ground"})).get("text") is None
    assert phrasing.phrase("x", "x", OPENING, [], model({"call": "request taxi"}))["text"] == f"{OPENING}request taxi"


def test_what_cannot_be_worded_is_said_so():
    unclear = phrasing.phrase("pizza", "x", OPENING, CALLS, model({"unclear": "Ask the cabin crew, not Ground."}))
    assert unclear == {"note": "Ask the cabin crew, not Ground."}
    for nonsense in ("no json here", "{broken", "[1, 2]", {"choice": 9}, {"choice": 0}, {"choice": True}, {"choice": "1"},
                     {"call": ""}, {"call": "x" * 400}, {}):  # fmt: skip
        result = phrasing.phrase("something", "x", OPENING, CALLS, model(nonsense))
        assert "text" not in result and result["note"], nonsense
    assert phrasing.phrase("   ", "x", OPENING, CALLS, model({"choice": 1})) == {"note": "Type what you want first."}


def test_without_a_language_model_the_nearest_button_is_used():
    assert phrasing.phrase("I would like to taxi please", "x", OPENING, CALLS)["intent"] == {"intent": "request_taxi"}
    assert phrasing.phrase("check the radio", "x", OPENING, CALLS)["text"] == f"{OPENING}radio check"
    assert phrasing.phrase("can I climb", "x", OPENING, CALLS) == {"note": phrasing.NO_MODEL}
    assert phrasing.phrase("flight following", "x", OPENING, CALLS) == {"note": phrasing.NO_MODEL}  # it needs a place


def test_a_call_opens_with_the_station_until_it_has_been_contacted(tmp_path):
    _, engine, ground = ground_world(tmp_path)
    parked = taxiing(*APRON)
    assert engine.opening(ground, parked) == "Testfield Ground, Golf Alfa Bravo Charlie Delta, "
    engine.handle(Intent("request_taxi"), ground, ISSUE, parked)
    assert engine.opening(ground, parked) == "Golf Alfa Bravo Charlie Delta, "


class Model:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def complete(self, system, user, max_tokens=None, temperature=0.3):
        self.asked.append((user, max_tokens, temperature))
        if isinstance(self.answer, Exception):
            raise self.answer
        return json.dumps(self.answer)


def session_at_the_gate(tmp_path, answer, connected=True):
    db, engine, _ = ground_world(tmp_path)
    events: list[dict] = []
    session = Session(Config(), emit=events.append)
    state = {**taxiing(*APRON), "com1_khz": 121700, "zulu_seconds": 43200.0}
    session.bridge, session.db, session.book = FakeBridge(state, connected), db, AtisBook()
    session.engine, session.controller, session.ready = engine, Model(answer) if answer is not None else None, True
    return session, events


def test_the_session_words_a_request_from_the_calls_that_fit_now(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    session, events = session_at_the_gate(tmp_path, {"call": "request pushback"})
    session._phrase("push me back please")
    said = events[-1]
    assert said["type"] == "phrase" and said["said"] == "push me back please" and said["intent"] is None and said["note"] == ""
    assert said["text"] == "Testfield Ground, Golf Alfa Bravo Charlie Delta, request pushback"
    user, tokens, temperature = session.controller.asked[0]
    assert "Station: Testfield Ground" in user and "request taxi" in user.lower() and (tokens, temperature) == (160, 0.0)
    assert session.log.records == [] or all(record["type"] != "phrase" for record in session.log.records)

    raw, station, issue = session._station_and_issue()
    listed = session.engine.suggestions(station, issue, raw)
    number = next(index for index, call in enumerate(listed, start=1) if call["intent"]["intent"] == "request_taxi")
    session.controller.answer = {"choice": number}
    session._phrase("I want to go to the runway")
    assert events[-1]["intent"]["intent"] == "request_taxi" and events[-1]["text"].endswith("request taxi")
    assert session.engine.flight.taxi_goal is None  # wording a call transmits nothing

    # Failures are answered too: the window is waiting for a reply.
    session.controller.answer = LlmError("OpenRouter returned 429")
    session._phrase("anything")
    assert events[-1]["text"] == "" and events[-1]["note"] == "OpenRouter returned 429"
    session.controller.answer = RuntimeError("boom")
    session._phrase("anything")
    assert "RuntimeError" in events[-1]["note"]
    session.controller = None
    session._phrase("taxi")
    assert events[-1]["intent"]["intent"] == "request_taxi"  # no model: the nearest button


def test_no_frequency_no_wording_and_the_app_command_reaches_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(config_module, "PROJECT_DIR", tmp_path)
    monkeypatch.setattr("proatc.server.PROJECT_DIR", tmp_path)
    session, events = session_at_the_gate(tmp_path, {"choice": 1}, connected=False)
    session._phrase("taxi")
    assert events[-1]["text"] == "" and "Tune a controller" in events[-1]["note"] and session.controller.asked == []

    app = AppServer(Config(), 0, "token")
    asked: list[str] = []
    done = threading.Event()
    app.session.ready = True
    app.session.phrase = lambda text: (asked.append(text), done.set())
    app._command({"cmd": "phrase", "text": "  "})
    app._command({"cmd": "phrase", "text": "taxi please"})
    assert asked == ["taxi please"]
    session.phrase("taxi")  # the real one answers from its own thread
    for _ in range(200):
        if len(events) > 1:
            break
        threading.Event().wait(0.01)
    assert events[-1]["type"] == "phrase"
