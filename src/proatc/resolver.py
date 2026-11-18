"""A second reading of a call the first could not place.

The first reading (intents.py) turns one transmission into an intent, with only the controller's last
instruction for context. Off-script speech ("it's getting bumpy up here, any chance of a bit lower?") can come
back as "other", and the controller then had nothing better than "say again" or "say intentions": a dead end.

The second reading runs only then. It gets what a controller would have in mind: the conversation on this
frequency, the flight's state, the question the controller has asked and not had answered, and the calls that
fit the flight right now. It may reason before it answers, and it answers with one of three things:

- "act": the pilot clearly wants this call. It is handed to the rules engine as if the pilot had said it plainly.
- "confirm": the pilot most likely wants this call. The engine asks "confirm you request …" in its own words;
  "affirm" makes the call, "negative" asks the pilot to say it again (questions.py).
- "unclear": nothing to act on. The old free answer, which may only inform or decline, takes it.

It never says anything on the radio itself, and it cannot clear anything: whatever it reads goes through the
same rules as a click on a suggested call. Calls with consequences that a misreading must not have (declaring
or cancelling an emergency, diverting, returning, leaving the frequency) are always confirmed first.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .intents import FIELDS, INTENTS, Intent, parse_json
from .llm import LlmError

CONVERSATION = 12  # the last few exchanges on this frequency
CALLS_SHOWN = 24
MAX_TOKENS = 400
# Never acted on from a second reading alone: a misreading would declare an emergency, send the flight elsewhere
# or take it off the frequency.
ALWAYS_CONFIRM = {
    "mayday", "pan_pan", "cancel_emergency", "request_diversion", "request_return", "report_leaving",
    "cancel_flight_following", "going_around",
}  # fmt: skip
# Questions that change nothing: answered even when the reading was unsure and they cannot be asked back.
HARMLESS = {"request_information", "request_weather", "say_again", "radio_check"}
# Calls that are not requests: the second reading has nothing to add to them.
NOT_A_CALL = {"other", "readback", "acknowledge", "initial_call"}

PROMPT = (
    """You help an air traffic controller in a flight simulator understand a pilot's radio call that a first, \
quick reading could not place. You do not decide what the controller says. You work out what the pilot is asking \
for or telling, as one call of the kinds listed below, and the controller's rules decide the answer.

You get the station, the conversation on this frequency so far, the flight's state, the question the controller \
has asked and not had answered (if any), the calls that fit the flight right now (each with its call object), and \
the pilot's transmission.

Answer with one JSON object and nothing else, with the keys in this order:
{"why": "<one short sentence: what the pilot wants, and what in the words or the context shows it>",
 "decision": "act" or "confirm" or "unclear",
 "call": <the call object described below, or null>}

- "act": you are sure what the pilot wants, and it is one call. Give it in "call".
- "confirm": the pilot most likely wants this call, but there is real doubt: two calls would fit, or a value is \
inferred rather than said. The controller will ask the pilot to confirm it. Give it in "call".
- "unclear": you cannot tell, or the transmission asks for nothing a controller acts on (a remark, a joke, a \
question about something other than this flight). "call" is null.

Rules:
- Read the words in the light of the conversation and the flight's state: "a bit lower" in the cruise is a \
descent; "the longer one" is the longer of the runways the conversation or the state names; "go back" on the \
ground is a return to the stand.
- When the controller has an open question, an answer to it is that answer: "affirm", "negative", or the call the \
question asks for.
- Prefer one of the calls that fit now when one says what the pilot wants; copy its call object.
- Never invent a value: a level, heading, runway, waypoint or airport the pilot did not say and the context does \
not give stays null, and then the decision is "confirm" at most.
- The intent is never "other", "readback" or "acknowledge".

The call object:
"""
    + FIELDS
)


@dataclass
class Reading:
    decision: str  # "act", "confirm" or "unclear"
    intent: Intent | None = None
    why: str = ""


UNCLEAR = Reading("unclear")


def situation(engine, station, issue, raw: dict, history: list[dict]) -> dict:
    """What a controller would have in mind when the call came: everything the second reading may use."""
    from . import questions

    question = questions.open_question(engine, station)
    calls = [
        f'{call["label"]}: "{call["text"]}" -> {json.dumps(call["intent"])}'
        for call in engine.suggestions(station, issue, raw)[:CALLS_SHOWN]
    ]
    plan = engine.flight.plan.summary() if engine.flight.plan else None
    return {
        "station": f"{station.callsign} ({station.kind})",
        "conversation": [entry.get("content", "") for entry in history[-2 * CONVERSATION :]],
        "flight": engine.summary(),
        "plan": plan,
        "on_ground": bool(raw.get("on_ground", True)),
        "altitude_ft": round(raw.get("elevation_m", 0.0) / 0.3048, -1),
        "open_question": question.text if question else None,
        "calls_that_fit": calls,
    }


BRIEF_LINES = 6  # the last few lines of the conversation the first reading is given


def brief(about: dict) -> str:
    """The situation in a few lines, for the first reading: enough to place "go back", "the longer one", "are we
    good to land" or an answer to a question, without the second reading's whole picture."""
    flight, plan = about.get("flight") or {}, about.get("plan") or {}
    where = "on the ground" if about.get("on_ground") else f"airborne at {about.get('altitude_ft', '?')} ft"
    first = (
        f"Station: {about.get('station', '?')}; phase: {flight.get('phase', '?')}; {where}; "
        f"from {plan.get('origin') or '?'} to {plan.get('destination') or '?'}"
    )
    if flight.get("emergency"):
        first += f"; emergency: {flight['emergency']}"
    lines = [first]
    if about.get("open_question"):
        lines.append(f"The controller asked and is waiting for an answer: {about['open_question']}")
    conversation = [line for line in about.get("conversation", []) if line][-BRIEF_LINES:]
    if conversation:
        lines.append("Conversation so far on this frequency: " + " | ".join(conversation))
    return "Flight situation (for context only): " + "\n".join(lines)


def read(controller, about: dict, transcript: str) -> Reading:
    """The second reading. Anything the model gets wrong in form (no JSON, an unknown intent, a call with no
    meaning) is "unclear": the caller then answers as it did before there was a second reading."""
    user = f"Situation: {json.dumps(about, default=str)}\nPilot transmission: {transcript}"
    try:
        text = controller.complete(PROMPT, user, max_tokens=MAX_TOKENS, temperature=0)
    except LlmError:
        return UNCLEAR
    answer = _json(text)
    decision, call = answer.get("decision"), answer.get("call")
    why = re.sub(r"\s+", " ", str(answer.get("why") or "")).strip()[:240]
    if decision not in ("act", "confirm") or not isinstance(call, dict) or call.get("intent") not in INTENTS:
        return Reading("unclear", None, why)
    intent = parse_json(json.dumps(call))
    if intent.intent in NOT_A_CALL:
        return Reading("unclear", None, why)
    if intent.intent in ALWAYS_CONFIRM:
        decision = "confirm"
    return Reading(decision, intent, why)


def _json(text: str) -> dict:
    found = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(found.group(0)) if found else {}
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
