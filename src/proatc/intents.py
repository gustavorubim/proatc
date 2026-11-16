"""Turn a pilot transmission into a structured intent with the language model.

The model only interprets speech here. What the controller answers is decided by engine.py.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, fields

INTENTS = {
    "radio_check": "radio check or test call",
    "initial_call": "only a greeting or callsign, no request yet",
    "request_clearance": "asks for IFR or VFR departure clearance",
    "request_pushback": "asks for push back, start-up, or both",
    "request_taxi": "asks to taxi (for departure, or to parking after landing)",
    "ready_for_departure": "ready for departure / ready for takeoff / holding short ready",
    "inbound": "airborne first call wanting to land or join the circuit, or established on an approach",
    "position_report": "reports downwind, base or final, or asks for landing clearance",
    "check_in": "first call to a departure, approach or en-route controller, giving altitude or level",
    "request_climb": "asks for a higher level",
    "request_descent": "asks for descent",
    "established": "reports established on the localiser, I L S or final approach course",
    "request_direct": "asks to route direct to a waypoint",
    "request_hold": "asks to hold, or for a delay before the approach",
    "cancel_hold": "in a hold, says ready for the approach or asks to leave the hold",
    "request_weather": "asks for the wind, the Q N H or altimeter, or the weather at an airport",
    "request_deviation": "asks to deviate left or right of track, usually to avoid weather",
    "clear_of_weather": "reports clear of weather or able to resume navigation",
    "request_touch_and_go": "asks for a touch and go",
    "going_around": "going around / missed approach",
    "runway_vacated": "reports the runway vacated / clear of the runway",
    "readback": "mainly repeats the controller's last instruction back",
    "say_again": "asks the controller to repeat",
    "other": "anything else",
}

PROMPT = """You convert one pilot radio transmission from a flight simulator into JSON. Output only a JSON object, no other text.

Keys:
- "intent": one of: {intents}
- "callsign": the aircraft callsign as spoken, or null
- "runway": runway mentioned, digits plus optional L/R/C, e.g. "27L", or null
- "atis": the ATIS information letter the pilot says they have, as one capital letter, or null
- "destination": destination airport or city for a clearance request, or null
- "flight_rules": "IFR", "VFR" or null
- "squawk": four digits if a transponder code is said, or null
- "qnh": the pressure setting said, as a number (hPa such as 1006, or inches such as 29.92), or null
- "frequency": a radio frequency said, as a string like "118.505", or null
- "position": one of "downwind", "base", "final", or null
- "altitude": the altitude or level the pilot was CLEARED to or asks for, in feet as a number ("flight level two four zero" is 24000, "six thousand" is 6000), not the altitude being passed; or null
- "heading": a heading said, as a number of degrees, or null
- "direction": "left" or "right" for a requested deviation, or null
- "fix": the waypoint or navaid of a "direct to" request, as its identifier in capital letters (e.g. "BIBAX"), or null
- "touch_and_go": true if a touch and go is requested or read back, else false

Rules:
- The speech-to-text may contain errors; pick the most plausible aviation meaning.
- "niner" is 9, "tree" is 3, "fife" is 5, "decimal" or "point" separates a frequency.
- If the transmission repeats back the controller's last instruction (given below), the intent is "readback", and you still fill runway, squawk, qnh and frequency with what the PILOT said, even if it differs from the instruction.
- A pilot who reports "final" or asks for landing clearance is "position_report" with position "final"."""

SYSTEM = PROMPT.format(intents="; ".join(f'"{name}" ({meaning})' for name, meaning in INTENTS.items()))


@dataclass
class Intent:
    intent: str = "other"
    callsign: str | None = None
    runway: str | None = None
    atis: str | None = None
    destination: str | None = None
    flight_rules: str | None = None
    squawk: str | None = None
    qnh: float | None = None
    frequency: str | None = None
    position: str | None = None
    touch_and_go: bool = False
    altitude: float | None = None
    heading: float | None = None
    fix: str | None = None
    direction: str | None = None


def _clean(data: dict) -> Intent:
    """Normalise whatever the model returned; unknown or malformed values become defaults."""
    intent = Intent()
    known = {f.name for f in fields(Intent)}
    data = {k: v for k, v in data.items() if k in known and v not in (None, "", "null")}
    if data.get("intent") in INTENTS:
        intent.intent = data["intent"]
    for key in ("callsign", "destination", "fix"):
        if isinstance(data.get(key), str):
            setattr(intent, key, data[key].strip())
    runway = re.fullmatch(
        r"0?(\d{1,2})\s*([LRC])?",
        str(data.get("runway", ""))
        .upper()
        .replace("LEFT", "L")
        .replace("RIGHT", "R")
        .replace("CENTER", "C")
        .replace("CENTRE", "C")
        .strip(),
    )
    if runway and 1 <= int(runway.group(1)) <= 36:
        intent.runway = f"{int(runway.group(1)):02d}{runway.group(2) or ''}"
    atis = str(data.get("atis", "")).strip().upper()
    if atis and atis[0].isalpha():
        intent.atis = atis[0]
    rules = str(data.get("flight_rules", "")).upper()
    intent.flight_rules = rules if rules in ("IFR", "VFR") else None
    squawk = re.sub(r"\D", "", str(data.get("squawk", "")))
    intent.squawk = squawk if len(squawk) == 4 else None
    try:
        intent.qnh = float(data["qnh"]) if "qnh" in data else None
    except (TypeError, ValueError):
        intent.qnh = None
    frequency = re.fullmatch(r"(1[123]\d)[.,]?(\d{0,3})", str(data.get("frequency", "")).replace(" ", ""))
    if frequency:
        intent.frequency = f"{frequency.group(1)}.{frequency.group(2).ljust(3, '0')}"
    if data.get("direction") in ("left", "right"):
        intent.direction = data["direction"]
    if data.get("position") in ("downwind", "base", "final"):
        intent.position = data["position"]
    intent.touch_and_go = data.get("touch_and_go") is True
    for key, low, high in (("altitude", 0, 60000), ("heading", 0, 360)):
        try:
            value = float(data[key]) if key in data else None
        except (TypeError, ValueError):
            value = None
        setattr(intent, key, value if value is not None and low <= value <= high else None)
    return intent


def parse_json(text: str) -> Intent:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return Intent()
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return Intent()
    return _clean(data) if isinstance(data, dict) else Intent()


class IntentParser:
    def __init__(self, controller):
        self.controller = controller  # reuses the OpenRouter client, retries and fallback models

    def parse(self, transcript: str, last_instruction: str = "") -> Intent:
        user = f"Controller's last instruction: {last_instruction or '(none)'}\nPilot transmission: {transcript}"
        return parse_json(self.controller.complete(SYSTEM, user, max_tokens=200, temperature=0))
