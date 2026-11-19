"""Other aircraft heard on the frequency.

This is sound only: nothing here is an aircraft in the sim, and none of it affects what the
controller tells you. Each exchange is one instruction from the tuned station to an invented
flight and that flight's readback, built from the same real data the controller uses: the
runway in use, the wind and pressure, the airport's own taxiways and frequencies.
"""

from __future__ import annotations

import random

from . import phraseology as say
from . import vfr
from .atis import split_runways, uses_faa_style

MODES = ("off", "some", "busy")
# Average seconds between exchanges with chatter set to "some"; "busy" halves it.
GAP_S = {"unicom": 70, "delivery": 80, "ground": 50, "tower": 45, "approach": 40, "departure": 55, "center": 60}
FIRST_AFTER_S = (6, 18)  # after tuning a frequency
SMALL_FIELD_M = 1900  # longest runway below this: the other traffic is light aircraft

EUROPE = "BAW EZY RYR DLH AFR KLM SAS SWR IBE TAP VIR EIN WZZ AUA FIN LOT NAX VLG THY BEL EXS".split()
AMERICAS = "AAL DAL UAL SWA JBU ASA ACA WJA FDX UPS AMX CMP AVA".split()
ELSEWHERE = "UAE QTR SIA CPA QFA ANA JAL ETD THY BAW DLH AFR KLM".split()
CITIES_EUROPE = ["Amsterdam", "Frankfurt", "Madrid", "Dublin", "Geneva", "Rome", "Copenhagen", "Lisbon", "Vienna", "Munich"]
CITIES_AMERICAS = ["Chicago", "Atlanta", "Denver", "Boston", "Dallas", "Seattle", "Toronto", "Miami", "Phoenix"]


def gap_seconds(kind: str, mode: str, rng: random.Random) -> float:
    mean = GAP_S.get(kind, 60) * (0.5 if mode == "busy" else 1.0)
    return mean * rng.uniform(0.6, 1.6)


def _airlines(ident: str | None) -> list[str]:
    ident = (ident or "").upper()
    if ident[:1] in ("E", "L", "B"):
        return EUROPE
    if ident[:1] in ("K", "C", "P", "M", "S", "T"):
        return AMERICAS
    return ELSEWHERE


def flight_callsign(ident: str | None, rng: random.Random, avoid: str = "") -> str:
    """An airline flight that is not the user's own."""
    for _ in range(10):
        code = rng.choice(_airlines(ident))
        number = str(rng.randint(1, 9)) + "".join(str(rng.randint(0, 9)) for _ in range(rng.choice((1, 2, 2, 3))))
        spoken = say.airline_callsign(f"{code}{number}")
        if spoken and spoken != avoid:
            return spoken
    return "Speedbird one two three"


REGISTRATION = {
    "EG": "G",
    "EI": "EI",
    "LF": "F",
    "ED": "D",
    "LE": "EC",
    "LI": "I",
    "EH": "PH",
    "LS": "HB",
    "LO": "OE",
    "ES": "SE",
    "EN": "LN",
    "EK": "OY",
    "EB": "OO",
}
LETTERS = "ABCDEFGHJKLMNOPRSTUVWXYZ"


def light_callsign(ident: str | None, rng: random.Random, avoid: str = "") -> str:
    """A light aircraft's registration, as spoken, in the style of the country."""
    ident = (ident or "").upper()
    for _ in range(10):
        if ident[:1] in ("K", "P"):
            tail = "N" + "".join(str(rng.randint(1, 9)) for _ in range(3)) + "".join(rng.choice(LETTERS) for _ in range(2))
        elif ident[:1] == "C":
            tail = "C" + rng.choice("FG") + "".join(rng.choice(LETTERS) for _ in range(3))
        elif ident[:1] == "Y":
            tail = "VH" + "".join(rng.choice(LETTERS) for _ in range(3))
        else:
            prefix = REGISTRATION.get(ident[:2], "G")
            tail = prefix + "".join(rng.choice(LETTERS) for _ in range(5 - len(prefix)))
        spoken = say.spoken_callsign(tail)
        if spoken != avoid:
            return spoken
    return "Golf Alfa Bravo Charlie Delta"


def _self_announced(station, issue, engine, rng: random.Random, own_callsign: str) -> tuple[str, str, str] | None:
    """Another aircraft's blind call at an airfield with no controller. Nobody answers it."""
    airport, place = station.airport, station.place
    callsign = light_callsign(airport.ident, rng, own_callsign)
    active = engine.active_runways(airport, issue)
    runway = f" runway {say.runway(active[0])}" if active else ""
    side = vfr.pattern_side(engine, airport, active[0]) if active else "left"
    miles = say.digits(rng.choice((5, 8, 10, 12)))
    direction = rng.choice(vfr.COMPASS)
    body = rng.choice(
        (
            f"{side} downwind{runway}",
            f"{side} base{runway}",
            f"final{runway}",
            f"final{runway}, touch and go",
            f"departing{runway}",
            f"departing{runway}, remaining in the pattern",
            f"clear of{runway}" if runway else "clear of the runway",
            f"{miles} miles {direction}, inbound for landing{runway}",
            f"taxiing to{runway}" if runway else "taxiing for departure",
        )
    )
    return callsign, f"{place} traffic, {callsign}, {body}, {place}", ""


def _informed(station, issue, engine, rng: random.Random, own_callsign: str) -> tuple[str, str, str] | None:
    """An exchange with an information officer: information, and what the pilot intends."""
    airport = station.airport
    callsign = light_callsign(airport.ident, rng, own_callsign)
    active = engine.active_runways(airport, issue)
    wind = engine._wind(issue, 0.0, False) if issue else "wind calm"
    pressure = engine._pressure(issue, False)[0] if issue else ""
    runway = say.runway(active[0]) if active else None
    options = [
        (f"no reported traffic, {wind}, land at your discretion", "Landing"),
        (f"no reported traffic, {wind}, take off at your discretion", "Taking off"),
        ("roger, report final", "Wilco"),
    ]
    if runway:
        side = vfr.pattern_side(engine, airport, active[0])
        tail = f", {pressure}" if pressure else ""
        options.append((f"runway {runway}, {side} hand circuit{tail}, report downwind", f"Runway {runway}{tail}, wilco"))
    said, answered = rng.choice(options)
    return callsign, f"{callsign}, {said}", f"{answered}, {callsign}"


def _level_word(feet: int, faa: bool) -> str:
    return say.level(feet, 18000 if faa else 6000, faa)


def exchange(station, issue, engine, rng: random.Random, own_callsign: str = "") -> tuple[str, str, str] | None:
    """(callsign, what the controller says, what the pilot answers) for this station, or None."""
    kind = station.kind
    airport = getattr(station, "airport", None)
    informing = vfr.service(station) if airport else None
    if informing in ("ctaf", "unicom"):
        return _self_announced(station, issue, engine, rng, own_callsign)
    if informing == "afis":
        return _informed(station, issue, engine, rng, own_callsign)
    ident = airport.ident if airport else getattr(getattr(station, "center", None), "facility", None)
    faa = uses_faa_style(ident or "")
    # A small airfield is used by light aircraft, not by airlines.
    small = airport is not None and max((r.length_m for r in airport.runways), default=0) < SMALL_FIELD_M
    callsign = light_callsign(ident, rng, own_callsign) if small else flight_callsign(ident, rng, own_callsign)
    heading = say.heading(rng.randrange(10, 361, 10))
    side = rng.choice(("left", "right"))
    options: list[tuple[str, str]] = []  # (instruction, readback); the readback gets the callsign appended

    active = engine.active_runways(airport, issue) if airport else []
    landing, departing = split_runways(active) if active else ([], [])
    wind = engine._wind(issue, 0.0, faa) if issue else ""
    pressure = engine._pressure(issue, faa)[0] if issue else ""

    def handoff(to: str) -> None:
        target = engine._handoff(airport, to) if airport else None
        if target:
            options.append((f"contact {target[0]} {target[1]}", f"{target[1]}"))

    if kind == "delivery":
        city = rng.choice(CITIES_AMERICAS if faa else CITIES_EUROPE)
        code = say.digits("".join(str(rng.randint(0, 7)) for _ in range(4)))
        runway = f", runway {say.runway(rng.choice(departing))}" if departing else ""
        options.append(
            (f"cleared to {city} as filed{runway}, squawk {code}", f"cleared to {city} as filed{runway}, squawk {code}")
        )
        handoff("ground")
    elif kind == "ground":
        names = _taxiways(engine, airport, rng)
        via = f" via {say.join([say.taxiway(name) for name in names])}" if names else ""
        if departing:
            runway = say.runway(rng.choice(departing))
            text = f"runway {runway}, taxi{via}" if faa else f"taxi to holding point runway {runway}{via}"
            options.append((text, text))
        options.append(("push back approved" if faa else "push back and start approved", "push and start approved"))
        stand = say.digits(rng.randint(1, 60))
        options.append((f"taxi to {'gate' if faa else 'stand'} {stand}{via}", f"{'gate' if faa else 'stand'} {stand}{via}"))
        options.append(("hold position, give way to the company traffic from your right", "holding position"))
        handoff("tower")
    elif kind == "tower":
        for runway in landing[:1]:
            spoken = say.runway(runway)
            tail = f", {wind}" if wind else ""
            options.append((f"runway {spoken}, cleared to land{tail}", f"cleared to land runway {spoken}"))
            options.append((f"continue approach runway {spoken}, number two", f"continue approach runway {spoken}"))
        for runway in departing[:1]:
            spoken = say.runway(runway)
            tail = f", {wind}" if wind else ""
            options.append((f"runway {spoken}, cleared for takeoff{tail}", f"cleared for takeoff runway {spoken}"))
            wait = "line up and wait" if faa else "line up and wait behind the landing traffic"
            options.append((f"runway {spoken}, {wait}", f"runway {spoken}, {wait}"))
        handoff("departure")
        handoff("ground")
    elif kind == "approach":
        feet = rng.choice((3000, 4000, 5000, 6000))
        tail = f", {pressure}" if pressure else ""
        verb = "descend and maintain" if faa else "descend"
        options.append((f"{verb} {_level_word(feet, faa)}{tail}", f"{verb.split()[0]} {_level_word(feet, faa)}{tail}"))
        options.append((f"turn {side} heading {heading}", f"{side} heading {heading}"))
        speed = say.digits(rng.choice((160, 180, 210, 220)))
        slow = f"reduce speed to {speed}" if faa else f"reduce speed {speed} knots"
        options.append((slow, f"speed {speed}"))
        for runway in landing[:1]:
            spoken = say.runway(runway)
            options.append((f"turn {side} heading {heading}, cleared I L S approach runway {spoken}",
                            f"{side} heading {heading}, cleared I L S runway {spoken}"))  # fmt: skip
        handoff("tower")
    elif kind == "departure":
        feet = rng.choice((8000, 10000, 12000, 15000, 19000, 24000))
        verb = "climb and maintain" if faa else "climb"
        options.append((f"{verb} {_level_word(feet, faa)}", f"climb {_level_word(feet, faa)}"))
        options.append((f"turn {side} heading {heading}", f"{side} heading {heading}"))
        options.append(("resume own navigation", "own navigation"))
    elif kind == "center":
        feet = rng.choice(range(24000, 40000, 1000))
        verb = rng.choice(("climb", "descend"))
        full = f"{verb} and maintain" if faa else verb
        options.append((f"{full} {_level_word(feet, faa)}", f"{verb} {_level_word(feet, faa)}"))
        options.append((f"turn {side} {say.digits(rng.choice((10, 15, 20)))} degrees for spacing", f"{side} for spacing"))
        options.append(("say mach number", f"mach decimal {say.digits(rng.choice((74, 76, 78, 80, 82)))}"))
    if not options:
        return None
    instruction, readback = rng.choice(options)
    return callsign, f"{callsign}, {instruction}", f"{readback[0].upper()}{readback[1:]}, {callsign}"


def _taxiways(engine, airport, rng: random.Random) -> list[str]:
    """One or two of the airport's real taxiway names."""
    try:
        network = engine.network(airport)
    except Exception:
        return []
    names = sorted(
        {edge.name for edges in network.edges.values() for edge in edges if not edge.runway and 0 < len(edge.name) <= 3}
    )
    return rng.sample(names, min(len(names), rng.choice((1, 2)))) if names else []
