"""What a controller asked, and how the pilot can answer it.

A controller asks things: "confirm you are following the departure", "say intentions", "check your level",
"advise when able", "report established". Each is recorded on the flight as the open question, with the
station that asked, so that:

- the suggested calls offer the answer first, marked primary while it is the last thing that controller said;
- a "yes" or a "no" to it has a rule: "affirm, following the departure" is "roger", "negative" gets a heading
  or a direct back to the track, from the engine's own route;
- the instructor's hint and the Phraseology button know what to answer.

The question is read from the controller's own words, which are the engine's templates: one place sees every
question, including ones added later. Functions over the Engine, like ifr.py; every one runs under its lock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import phraseology as say
from .navdata import bearing

# The controller's words that make a question, most particular first. Matched on the text after the callsign.
PATTERNS = (
    (re.compile(r"off the (departure|arrival) track, confirm you are following"), "off_track"),
    (re.compile(r"off your filed route, confirm your routing"), "off_track"),
    (re.compile(r"confirm you (?:request|are declaring|are cancelling|are going)"), "confirm_request"),
    (re.compile(r"say intentions"), "intentions"),
    (re.compile(r"low altitude alert"), "low_altitude"),
    (re.compile(r"check your (?:altitude|level), cleared"), "level_bust"),
    (re.compile(r"advise when able to turn"), "able_heading"),
    (re.compile(r"maintain [^,]+, advise when able$"), "able_level"),
    (re.compile(r"advise when able$"), "able"),
    (re.compile(r"advise when ready for the approach"), "ready_approach"),
    (re.compile(r"(?:report|advise) ready(?: for departure)?$"), "ready_departure"),
    (re.compile(r"report established$"), "established"),
    (re.compile(r"say destination|code of the airport you are diverting to"), "destination"),
    (re.compile(r"say the runway you request"), "runway"),
    (re.compile(r"report ([^,]+)$"), "report"),
)
# The calls that answer each question in the ordinary way, through the engine's usual rules: once one of them
# has been made to the station that asked, the question is closed. A readback never is: it answers the instruction
# the question came with ("maintain flight level one one zero, advise when able"), not the question.
ANSWERED_BY = {
    "confirm_request": {"affirm", "negative"},
    "off_track": {"affirm", "negative", "unable", "request_vectors", "request_direct", "request_deviation"},
    "intentions": {"affirm", "negative", "request_return", "request_diversion", "request_hold", "going_around",
                   "request_special_vfr", "request_vectors", "inbound", "request_approach", "request_visual",
                   "report_leaving", "request_clearance", "position_report"},
    "low_altitude": {"affirm", "request_climb", "check_in"},
    "level_bust": {"affirm", "negative", "unable", "check_in"},
    "able_heading": {"affirm", "negative"},
    "able_level": {"affirm", "negative", "request_climb", "request_descent"},
    "able": {"affirm", "negative"},
    "ready_approach": {"cancel_hold", "request_approach", "request_vectors"},
    "ready_departure": {"ready_for_departure", "request_delay", "request_return"},
    "established": {"established", "going_around", "request_vectors", "unable", "negative"},
    "destination": {"request_clearance", "request_diversion"},
    "runway": {"request_runway"},
    "report": {"position_report", "report_leaving", "clear_of_weather", "check_in", "established", "inbound", "affirm"},
}  # fmt: skip


@dataclass
class Question:
    kind: str  # one of the kinds in PATTERNS
    station: str  # the key of the station that asked
    text: str  # the controller's words
    data: dict = field(default_factory=dict)  # what the answer needs: the track, the level or heading to come back to


# ---- what was asked -----------------------------------------------------------------


def classify(text: str) -> tuple[str, dict] | None:
    """(kind, details) when the controller's words ask the pilot something, else None."""
    body = text.split(", ", 1)[1] if ", " in text else text
    for pattern, kind in PATTERNS:
        found = pattern.search(body)
        if not found:
            continue
        data: dict = {}
        if kind == "off_track":
            data["track"] = found.group(1) if found.groups() else "route"
        elif kind == "report":
            data["what"] = found.group(1)
        elif kind == "intentions":
            data["why"] = _why(body)
        return kind, data
    return None


def _why(body: str) -> str:
    if "seven seven zero zero" in body:
        return "emergency"
    if "landing clearance cancelled" in body:
        return "landing_cancelled"
    if "no instrument approach" in body:
        return "no_approach"
    return "weather"  # below V F R minima, or the field is I F R


def record(engine, key: str, reply) -> None:
    """Note a controller's transmission: a question opens; a new instruction from the same station closes one."""
    if reply is None or not reply.text:
        return
    flight = engine.flight
    found = classify(reply.text)
    if found:
        kind, data = found
        flight.question = Question(kind, key, reply.text, {**data, **getattr(reply, "asks", {})})
    elif flight.question and flight.question.station == key and reply.expect and not reply.correction:
        flight.question = None  # the controller has moved on to something new to read back


def open_question(engine, station) -> Question | None:
    """The question this station asked and the pilot has not answered."""
    question = engine.flight.question
    return question if question and question.station == station.key else None


def latest(engine, station) -> bool:
    """Whether the open question is the last thing this station said, with nothing waiting to be read back."""
    question, flight = open_question(engine, station), engine.flight
    if question is None or flight.last_said.get(station.key) != question.text:
        return False
    return not (flight.pending and flight.pending_key == station.key)


def settle(engine, intent, station) -> None:
    """A call that answers the open question in the usual way closes it."""
    question = open_question(engine, station)
    if question and intent.intent in ANSWERED_BY.get(question.kind, ()):
        engine.flight.question = None


# ---- the answers that have a rule here ----------------------------------------------------


# Calls that leave "confirm you request …" waiting: a request to say it again, a "roger", and words the first
# reading could not place, which the second reading then reads with the question still in view.
KEEPS_CONFIRM_OPEN = {"say_again", "acknowledge", "other"}


def confirmed(engine, intent, station, callsign: str):
    """The pilot's answer to "confirm you request …" (asked after a second reading, resolver.py).

    Returns (the call to go on with, a reply to give instead). "Affirm" is the call that was asked about, as if the
    pilot had made it plainly. "Negative" is answered with a request to say it again. Any other call is a new one:
    the question is dropped and the call goes on as it is."""
    waiting = engine.flight.question
    if waiting and waiting.kind == "confirm_request" and waiting.station != station.key:
        engine.flight.question = None  # asked on another frequency: an "affirm" much later must not make that call
        return intent, None
    question = open_question(engine, station)
    if question is None or question.kind != "confirm_request":
        return intent, None
    if intent.intent in KEEPS_CONFIRM_OPEN:
        return intent, None
    engine.flight.question = None
    asked = question.data.get("intent")
    if intent.intent == "affirm" and isinstance(asked, dict):
        from .intents import Intent

        return Intent.from_dict(asked), None
    if intent.intent == "negative":
        from .engine import Reply

        return intent, Reply(f"{callsign}, roger, say again your request")
    return intent, None


def answer(engine, intent, station, issue, raw, variation, callsign, hello):
    """The controller's reply to an answer to its question, when the answer needs a rule of its own; else None."""
    question = open_question(engine, station)
    if question is None:
        return None
    flight, name, kind = engine.flight, intent.intent, question.kind
    if flight.pending and flight.pending_key == station.key and name in ("readback", "unable", "acknowledge"):
        return None  # an instruction is waiting for its readback: that comes first
    reply = None
    if kind == "off_track":
        reply = _off_track(engine, question, intent, station, raw, variation, callsign, hello)
    elif kind in ("level_bust", "low_altitude") and name in ("affirm", "readback"):
        reply = _roger(engine, callsign, hello)
    elif kind == "level_bust" and name in ("negative", "unable"):
        reply = _keep_level(engine, station, raw, callsign, hello)
    elif kind == "able_level" and name == "affirm" and question.data.get("altitude"):
        reply = _level_again(engine, question, station, raw, callsign, hello)
    elif kind == "able_heading" and name == "affirm" and question.data.get("heading") is not None:
        reply = _heading_again(engine, question, raw, callsign, hello)
    elif kind in ("able", "able_level", "able_heading") and name in ("affirm", "negative"):
        reply = _roger(engine, callsign, hello)
    elif kind == "intentions" and name == "affirm":
        tail = ", advise if you require further assistance" if question.data.get("why") == "emergency" else ""
        reply = _roger(engine, callsign, hello, tail)
    elif kind == "established" and name in ("negative", "unable", "request_vectors"):
        from . import ifr

        reply = ifr.vectors_again(engine, station, issue, raw, variation, callsign, hello)
    elif kind == "report" and name == "affirm":
        reply = _roger(engine, callsign, hello)
    if reply is not None:
        flight.question = None
    return reply


def _roger(engine, callsign, hello, tail: str = ""):
    from .engine import Reply

    return Reply(f"{callsign}, {hello}roger{tail}")


def _off_track(engine, question, intent, station, raw, variation, callsign, hello):
    """'Affirm, following the departure' is enough; 'negative' or a request for vectors or a direct gets a way back."""
    from . import ifr

    name = intent.intent
    if name == "affirm":
        return _roger(engine, callsign, hello)
    if name == "request_direct" and intent.fix and intent.fix.upper() not in track_names(engine, question.data["track"]):
        return None  # a direct to somewhere else on the route: the usual rule decides
    if name not in ("negative", "unable", "request_vectors", "request_direct"):
        return None
    track = question.data["track"]
    back = ifr.rejoin(engine, track, raw, variation, callsign, hello, heading=name == "request_vectors", faa=ifr._faa(station))
    if back is None:
        from .engine import Reply

        return Reply(f"{callsign}, {hello}roger, say intentions")
    return back


def _keep_level(engine, station, raw, callsign, hello):
    """Cannot get back to the cleared level: the level the aircraft is at is cleared instead, until it can."""
    from . import answers, ifr

    faa = ifr._faa(station)
    feet = int(round(ifr._feet(raw) / 100) * 100)
    engine.flight.cleared_ft, engine.flight.bust_called_for = feet, feet
    level = ifr._level(engine, feet, answers._level_ident(engine, station), faa)
    return ifr._reply(engine, f"{callsign}, {hello}roger, maintain {level}, advise when able", {"altitude": feet})


def _level_again(engine, question, station, raw, callsign, hello):
    """Able now: the level that could not be flown is given again."""
    from . import answers, ifr

    faa, target = ifr._faa(station), int(question.data["altitude"])
    engine.flight.cleared_ft = target
    climbing = target > ifr._feet(raw)
    verb = ("climb and maintain" if faa else "climb") if climbing else ("descend and maintain" if faa else "descend")
    level = ifr._level(engine, target, answers._level_ident(engine, station), faa)
    return ifr._reply(engine, f"{callsign}, {hello}{verb} {level}", {"altitude": target})


def _heading_again(engine, question, raw, callsign, hello):
    """Able to turn now: the heading that could not be flown is given again, and watched again."""
    from . import ifr
    from .vectors import turn_direction

    heading = int(question.data["heading"])
    arrival = engine.flight.arrival
    if arrival:
        arrival.heading, arrival.heading_at, arrival.heading_asked = heading, engine.now(), False
    current = raw.get("heading_mag", heading)
    turn = "fly" if abs((heading - current + 180) % 360 - 180) < 8 else f"turn {turn_direction(current, heading)}"
    return ifr._reply(engine, f"{callsign}, {hello}{turn} heading {say.heading(heading)}", {"heading": heading})


def track_names(engine, track: str) -> set[str]:
    return {name for name, _, _ in track_points(engine, track) if name}


def track_points(engine, track: str) -> list[tuple[str | None, float, float]]:
    """The waypoints of the track the flight was asked about: its SID, its STAR, or its filed route."""
    flight = engine.flight
    if track == "departure":
        return list(flight.sid_fixes)
    if track == "arrival":
        return list(flight.star_fixes)
    return list(engine.route()) if flight.plan else []


def heading_to(raw: dict, lat: float, lon: float, variation: float) -> int:
    """The magnetic heading, to the nearest five degrees, from the aircraft to a point."""
    return round(((bearing(raw["lat"], raw["lon"], lat, lon) + variation) % 360) / 5) * 5 % 360 or 360


# ---- the calls the pilot can make -------------------------------------------------------


def calls(engine, station, issue, raw, opening: str) -> list[dict]:
    """The answers to the open question, as suggested calls (label, text, intent), the most likely first."""
    question = open_question(engine, station)
    if question is None:
        return []
    found: list[dict] = []

    def add(label: str, body: str, intent: str, **fields) -> None:
        needs = fields.pop("needs", None)
        item = {"label": label, "text": f"{opening}{body}", "intent": {"intent": intent, **fields}, "answers": True}
        if needs:
            item["needs"] = needs
        found.append(item)

    offer = {
        "off_track": _offer_off_track, "intentions": _offer_intentions, "level_bust": _offer_level,
        "low_altitude": _offer_low, "able_level": _offer_able, "able_heading": _offer_able, "able": _offer_able,
        "ready_approach": _offer_ready_approach, "ready_departure": _offer_ready, "established": _offer_established,
        "destination": _offer_destination, "runway": _offer_runway, "report": _offer_report,
        "confirm_request": _offer_confirm,
    }.get(question.kind)  # fmt: skip
    if offer:
        offer(engine, question, station, issue, raw, add)
    return found


def arrange(engine, station, issue, raw, items: list[dict], opening: str) -> list[dict]:
    """The suggested calls with the answers to the open question first (after a readback), primary while it is the latest."""
    answers = calls(engine, station, issue, raw, opening)
    if not answers:
        return items
    primary = latest(engine, station)
    by_label = {item["label"]: item for item in items}
    first = []
    for call in answers:
        existing = by_label.get(call["label"])
        chosen = existing if existing is not None else call
        chosen["answers"] = True
        first.append(chosen)
    if primary:
        first[0]["primary"] = True
    rest = [item for item in items if item not in first]
    readback = [item for item in rest if item["label"] == "Read back"]
    rest = [item for item in rest if item["label"] != "Read back"]
    return readback + first + rest


def _procedure_words(engine, track: str, raw: dict) -> str:
    """'the Wesit two Alfa departure', 'the Inpip one Echo arrival', or the filed route."""
    from .procedures import spoken_name

    flight = engine.flight
    if track == "route":
        return "routing as filed"
    procedure = None
    if track == "departure" and flight.sid and flight.origin_ident:
        procedure = engine.procedures(flight.origin_ident).sids.get(flight.sid)
    elif track == "arrival":
        procedure = flight.star_procedure
    if procedure is None:
        return f"following the {track}"
    return f"following the {spoken_name(procedure, engine.navaids, raw.get('lat', 0.0), raw.get('lon', 0.0))} {track}"


def _offer_off_track(engine, question, station, issue, raw, add) -> None:
    from . import ifr
    from .airways import next_point

    track = question.data["track"]
    add("Affirm", f"affirm, {_procedure_words(engine, track, raw)}", "affirm")
    what = "your route" if track == "route" else f"the {track}"
    add("Request vectors", f"negative, request vectors to rejoin {what}", "request_vectors")
    ahead = next_point(track_points(engine, track), raw["lat"], raw["lon"]) if "lat" in raw else None
    if ahead and ahead[0]:
        word = ifr._fix_word(engine, ahead[0], ahead[1], ahead[2])
        add(f"Request direct {ahead[0]}", f"negative, request direct {word}", "request_direct", fix=ahead[0])


def _offer_intentions(engine, question, station, issue, raw, add) -> None:
    flight, plan = engine.flight, engine.flight.plan
    why = question.data.get("why")
    on_ground = bool(raw.get("on_ground", True))
    if why == "emergency":
        if flight.origin_ident and not on_ground:
            add(f"Return to {flight.origin_ident}", f"request return to {flight.origin_ident}", "request_return")
        if plan and plan.alternate:
            add(f"Divert to {plan.alternate}", f"request diversion to {plan.alternate}", "request_diversion",
                destination=plan.alternate)  # fmt: skip
        if plan and plan.destination and not on_ground:
            add(f"Continue to {plan.destination}", f"continuing to {plan.destination}", "affirm")
        return
    if why == "landing_cancelled":
        add("Going around", "going around", "going_around")
        return
    if why == "no_approach":
        if plan and plan.alternate:
            add(f"Divert to {plan.alternate}", f"request diversion to {plan.alternate}", "request_diversion",
                destination=plan.alternate)  # fmt: skip
        else:
            add("Divert to…", "request diversion to {destination}", "request_diversion", needs="destination")
        add("Request hold", "request holding", "request_hold")
        return
    # The weather is below V F R minima.
    add("Request special VFR", "request special V F R", "request_special_vfr", flight_rules="VFR")
    if on_ground:
        add("Wait for weather", "we will wait for the weather to improve", "affirm")
    else:
        add("Remain outside", "will remain outside controlled airspace", "affirm")


def _offer_level(engine, question, station, issue, raw, add) -> None:
    from . import answers, ifr

    cleared = engine.flight.cleared_ft
    if cleared is not None:
        level = ifr._level(engine, cleared, answers._level_ident(engine, station), ifr._faa(station))
        verb = "climbing" if cleared > ifr._feet(raw) else "descending"
        add("Correcting", f"correcting, {verb} {level}", "affirm")
    add("Unable", "unable", "unable")


def _offer_low(engine, question, station, issue, raw, add) -> None:
    add("Climbing", "climbing", "affirm")


def _offer_confirm(engine, question, station, issue, raw, add) -> None:
    add("Affirm", "affirm", "affirm")
    add("Negative", "negative", "negative")


def _offer_able(engine, question, station, issue, raw, add) -> None:
    from . import answers, ifr

    if question.kind == "able_level" and question.data.get("altitude"):
        feet = int(question.data["altitude"])
        level = ifr._level(engine, feet, answers._level_ident(engine, station), ifr._faa(station))
        add("Able now", f"now able {level}", "affirm")
    elif question.kind == "able_heading" and question.data.get("heading") is not None:
        add("Able now", f"now able heading {say.heading(int(question.data['heading']))}", "affirm")
    else:
        add("Able now", "now able", "affirm")


def _offer_ready_approach(engine, question, station, issue, raw, add) -> None:
    add("Ready for approach", "ready for the approach", "cancel_hold")


def _offer_ready(engine, question, station, issue, raw, add) -> None:
    runway = engine.flight.runway
    holding = f"holding short runway {say.runway(runway)}, " if runway and station.kind == "tower" else ""
    add("Ready for departure", f"{holding}ready for departure", "ready_for_departure")


def _offer_established(engine, question, station, issue, raw, add) -> None:
    arrival = engine.flight.arrival
    if arrival:
        add("Established", f"established {arrival.named} runway {say.runway(arrival.flown)}", "established")
    add("Request vectors", "unable to establish, request vectors", "request_vectors")


def _offer_destination(engine, question, station, issue, raw, add) -> None:
    if "diverting" in question.text:
        add("Divert to…", "request diversion to {destination}", "request_diversion", needs="destination")
    else:
        add("Request IFR clearance…", "request I F R clearance to {destination}", "request_clearance", flight_rules="IFR",
            needs="destination")  # fmt: skip


def _offer_runway(engine, question, station, issue, raw, add) -> None:
    from .answers import destination

    airport = station.airport or destination(engine)
    if airport is None:
        return
    for end in [end for runway in airport.runways for end, _ in runway.ends()][:4]:
        add(f"Request runway {end}", f"request runway {say.runway(end)}", "request_runway", runway=end)


def _offer_report(engine, question, station, issue, raw, add) -> None:
    """The report asked for: downwind, final, leaving the zone, clear of weather, a waypoint."""
    from . import ifr

    what = question.data.get("what", "")
    runway = f" runway {say.runway(engine.flight.runway)}" if engine.flight.runway else ""
    if what in ("downwind", "base") or what.endswith("final"):
        position = "final" if what.endswith("final") else what
        add(position.title(), f"{position}{runway}", "position_report", position=position)
    elif what.startswith("leaving") or what.startswith("clear of the"):
        add("Leaving the zone", "leaving the zone", "report_leaving")
    elif what == "clear of weather":
        add("Clear of weather", "clear of weather", "clear_of_weather")
    elif what == "back on frequency":
        add("Back on frequency", "back on your frequency", "check_in")
    elif what == "reaching":
        level = engine.flight.cleared_ft
        if level is not None:
            add("Reaching", f"reaching {say.level(level, 0)}", "affirm")
    elif "lat" in raw and not bool(raw.get("on_ground", True)):
        # 'Report Bravo' over the ocean: the position report, when the waypoint is passed.
        feet = engine.flight.cleared_ft or round(ifr._feet(raw) / 1000) * 1000
        add("Position report", f"position {what}, {say.level(feet, 0)}", "position_report")
