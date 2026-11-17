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
    "mayday": "declares an emergency: mayday, or declaring an emergency",
    "pan_pan": "declares an urgency: pan-pan",
    "cancel_emergency": "cancels a mayday or pan-pan",
    "request_flight_following": "a V F R flight asking for flight following, traffic service or radar service",
    "cancel_flight_following": "cancels flight following or radar service, or leaves the frequency V F R",
    "request_hold": "asks to hold, or for a delay before the approach",
    "cancel_hold": "in a hold, says ready for the approach or asks to leave the hold",
    "request_weather": "asks for the wind, the Q N H or altimeter, or the weather at an airport",
    "request_deviation": "asks to deviate left or right of track, usually to avoid weather",
    "clear_of_weather": "reports clear of weather or able to resume navigation",
    "request_approach": "asks for a particular type of instrument approach (I L S, R NAV, V O R, localiser, N D B)",
    "request_visual": "reports the field or the runway in sight, or asks for a visual approach",
    "request_diversion": "asks to divert to another airport or to the alternate",
    "request_crossing": "on the ground, holding short of a runway or asking to cross it",
    "request_progressive": "asks for progressive taxi instructions, or is unsure where to taxi",
    "request_transit": "airborne, asks to cross or transit a control zone or controlled airspace without landing",
    "report_leaving": "reports leaving or clear of the zone, or asks for a frequency change or to leave the frequency for a while",
    "request_special_vfr": "asks for special V F R",
    "request_circuits": "asks for circuits, closed traffic, or to remain in the pattern",
    "request_low_approach": "asks for a low approach or a low pass",
    "traffic_call": "a blind call or self-announcement to other traffic at an airfield with no controller (addressed to 'traffic')",
    "request_touch_and_go": "asks for a touch and go",
    "going_around": "going around / missed approach",
    "runway_vacated": "reports the runway vacated / clear of the runway",
    "readback": "mainly repeats the controller's last instruction back",
    "say_again": "asks the controller to repeat (with item set when only one thing is to be repeated: 'say again the heading')",
    "request_information": (
        "asks for, or to confirm or verify, one piece of information (set item): the cleared level, heading, speed, "
        "squawk, runway (assigned, expected or in use), frequency, Q N H or altimeter, clearance limit, taxi route, "
        "approach (expected or in use), A T I S letter, distance to touchdown, or a wind check"
    ),
    "acknowledge": "only roger, wilco, thank you, good day, standby, disregard or cancel my request, with no instruction to read back",
    "unable": "says unable to comply with the controller's last instruction",
    "request_delay": "not ready yet, or asks to wait a few minutes before the clearance, push, taxi or takeoff",
    "request_return": "asks to return: to the stand or gate on the ground, or to the departure airport once airborne",
    "request_runway": "asks for a particular or a different runway, for departure or for landing",
    "other": "anything else",
}
# What a request for information, or a "say again", can be about.
ITEMS = (
    "altitude", "heading", "speed", "squawk", "runway", "frequency", "qnh", "clearance", "route", "approach", "atis",
    "distance", "wind",
)  # fmt: skip

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
- "service": for a radar service request, "basic", "traffic" or "deconfliction" if one of those is named, else null
- "compass": a direction of flight or of the aircraft from the airfield, as "north", "north east", "east", "south east", "south", "south west", "west" or "north west", or null
- "approach": the type of approach asked for or read back: "ILS", "LOC", "RNAV", "VOR", "NDB" or "visual", or null
- "touch_and_go": true if a touch and go is requested or read back, else false
- "item": for "request_information" or "say_again", the one thing asked about: one of {items}; or null. "Confirm cleared level" is "altitude", "runway in use" or "expected runway" is "runway", "current approach in use" is "approach", "track miles" is "distance"

Rules:
- The speech-to-text may contain errors; pick the most plausible aviation meaning.
- "niner" is 9, "tree" is 3, "fife" is 5, "decimal" or "point" separates a frequency.
- If the transmission repeats back the controller's last instruction (given below), the intent is "readback", and you still fill runway, squawk, qnh, frequency, altitude and heading with what the PILOT said, even if it differs from the instruction.
- A pilot who reports "final" or asks for landing clearance is "position_report" with position "final".
- "Roger" or "wilco" after an instruction is "readback"; with no instruction to repeat it is "acknowledge"."""

SYSTEM = PROMPT.format(
    intents="; ".join(f'"{name}" ({meaning})' for name, meaning in INTENTS.items()),
    items=", ".join(f'"{item}"' for item in ITEMS),
)


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
    approach: str | None = None
    service: str | None = None
    compass: str | None = None
    item: str | None = None  # what a request for information is about: one of ITEMS

    @classmethod
    def from_dict(cls, data: dict) -> Intent:
        """An intent carried by a suggested call (a click needs no language model)."""
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in data.items() if key in known})


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
    approach = str(data.get("approach", "")).upper().replace(" ", "").replace(".", "")
    approach = {"RNP": "RNAV", "GPS": "RNAV", "GNSS": "RNAV", "LOCALISER": "LOC", "LOCALIZER": "LOC", "GLS": "ILS"}.get(
        approach, approach
    )
    if approach in ("ILS", "LOC", "RNAV", "VOR", "NDB"):
        intent.approach = approach
    elif approach == "VISUAL":
        intent.approach = "visual"
    if data.get("service") in ("basic", "traffic", "deconfliction"):
        intent.service = data["service"]
    points = str(data.get("compass", "")).lower().replace("-", " ").replace("northeast", "north east")
    points = points.replace("northwest", "north west").replace("southeast", "south east").replace("southwest", "south west")
    if points.strip() in ("north", "north east", "east", "south east", "south", "south west", "west", "north west"):
        intent.compass = points.strip()
    intent.touch_and_go = data.get("touch_and_go") is True
    item = str(data.get("item", "")).lower().strip()
    item = {"level": "altitude", "cleared level": "altitude", "altimeter": "qnh", "pressure": "qnh", "taxi": "route",
            "taxi route": "route", "clearance limit": "clearance", "track miles": "distance", "information": "atis"}.get(item, item)  # fmt: skip
    intent.item = item if item in ITEMS else None
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

    def parse(self, transcript: str, last_instruction: str = "", known: list[str] | None = None) -> Intent:
        """`known` lists waypoint and airport identifiers of this flight, so a misheard name can be matched."""
        user = f"Controller's last instruction: {last_instruction or '(none)'}\nPilot transmission: {transcript}"
        if known:
            user = f"Waypoints and airports of this flight: {' '.join(known[:40])}\n{user}"
        return parse_json(self.controller.complete(SYSTEM, user, max_tokens=200, temperature=0))
