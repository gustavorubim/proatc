"""The instrument flight from clearance to the approach: SID, departure, en-route centres,
descent with the STAR, radar vectors and the approach clearance.

These are the rules engine's decisions for radar controllers (departure, centre, approach)
and for the IFR clearance. They are functions over the Engine rather than methods so that
engine.py stays readable; every one runs under the engine's lock.

How the levels work:
- climbs and descents are given in steps of about 10,000 ft, the next one as you approach
  the level you were cleared to;
- the departure controller works up to FL240 (or your cruise level if lower), the centre to
  your cruise level;
- descent starts so that a three degree path reaches the level for the hand-off to approach.

What the controller watches: a level bust, a heading not flown, and the wrong squawk.

Simplifications, since there is no other traffic and no terrain model: vectors follow one
fixed pattern (see vectors.py) and there are no holds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import atis as atis_text
from . import holding, restrictions
from . import phraseology as say
from .atis import uses_faa_style
from .navdata import Airport, distance_nm
from .procedures import choose_approach, choose_sid, choose_star, route_fixes, spoken_name
from .vectors import FINAL_NM, established, locate, next_vector, turn_direction

RADAR_KINDS = ("departure", "approach", "center")
CHECK_IN = ("check_in", "initial_call", "position_report", "inbound")
DEPARTURE_TOP_FT = 24000  # the departure controller's highest level
DEPARTURE_HANDOFF_NM = 30
APPROACH_HANDOFF_NM = 45
STEP_FT = 10000  # the size of one climb or descent clearance
NEXT_STEP_WITHIN_FT = 2000  # the next step is given this close to the cleared level
LOW_AGL_FT = 1000  # descending below this height above the ground, away from the approach, draws an alert
BUST_FT = 400  # how far through a cleared level before the controller says something
HEADING_GRACE_S = 45  # time to roll onto an assigned heading before being asked about it
STAGES = ["descend", "downwind", "base", "intercept", "tower"]
SPEEDS_KT = {"downwind": 220, "base": 180, "intercept": 160}


@dataclass
class Arrival:
    airport: str
    runway: str
    approach: str  # as spoken: "I L S"
    platform_ft: int
    course_true: float
    threshold: tuple[float, float]
    stage: str | None = None
    qnh_given: bool = False
    heading: int | None = None  # last heading assigned, magnetic
    heading_at: float = 0.0  # when it was assigned (engine clock)
    heading_asked: bool = False
    speed_kt: int | None = None
    hold: object | None = None  # holding.HoldState while holding
    delay_decided: bool = False


def _feet(raw: dict) -> float:
    return raw.get("elevation_m", 0.0) / 0.3048


def _transition_ft(engine, ident: str | None, faa: bool) -> int:
    if faa:
        return 18000
    procedures = engine.procedures(ident) if ident else None
    return (procedures.transition_altitude_ft if procedures else None) or 6000


def _level(engine, feet: int, ident: str | None, faa: bool) -> str:
    return say.level(feet, _transition_ft(engine, ident, faa), faa)


def _centre_faa(station) -> bool:
    return station.center.facility[:1] in ("K", "P")


def _faa(station) -> bool:
    return _centre_faa(station) if station.kind == "center" else uses_faa_style(station.airport.ident)


def _reply(engine, text: str, expect: dict | None = None):
    from .engine import Reply

    return Reply(text, expect or {})


def _contact_word(faa: bool) -> str:
    return "radar contact" if faa else "identified"


def _fix_word(engine, fix: str, lat: float, lon: float) -> str:
    """A waypoint as spoken: a five-letter name as a word, a navaid by its name."""
    if len(fix) <= 3:
        name = engine.navaids.name(fix, lat, lon) if engine.navaids else None
        return name or " ".join(say.PHONETIC[c] for c in fix if c in say.PHONETIC)
    return fix.title()


# ---- clearance --------------------------------------------------------------


def clearance(engine, station, issue, raw, callsign, hello, faa, atis_part, on_correct):
    """IFR clearance from the filed plan: destination, SID, initial altitude, squawk."""
    from .engine import Reply, _squawk

    flight, plan, airport = engine.flight, engine.flight.plan, station.airport
    runway = engine._departure_runway(airport, issue, raw)
    if plan.departure_runway and issue and plan.departure_runway in engine.active_runways(airport, issue):
        runway = plan.departure_runway
    procedures = engine.procedures(airport.ident)
    sid = choose_sid(procedures, runway, plan.route, plan.sid)
    destination = engine.db.airport(plan.destination)
    where = destination.spoken_name if destination else say.spoken_callsign(plan.destination)

    initial = (sid.top_altitude() if sid else None) or (5000 if faa else 6000)
    if plan.cruise_ft:
        initial = min(initial, plan.cruise_ft)
    flight.rules, flight.destination, flight.runway = "IFR", where, runway
    flight.sid, flight.cleared_ft = (sid.name if sid else None), initial
    flight.squawk = flight.squawk if flight.squawk and flight.squawk not in ("1200", "7000") else _squawk(engine._rng)

    level = _level(engine, initial, airport.ident, faa)
    runway_part = f", runway {say.runway(runway)}" if runway else ""
    squawk = f"squawk {say.digits(flight.squawk)}"
    if sid:
        name = spoken_name(sid, engine.navaids, airport.lat, airport.lon)
        route = f"{name} departure, then as filed" if faa else f"{name} departure"
    else:
        route = "as filed"
    if faa:
        departure = engine._handoff(airport, "departure")
        frequency = f", departure frequency {departure[1]}" if departure else ""
        text = f"{callsign}, {hello}cleared to {where} airport, {route}{runway_part}, climb and maintain {level}{frequency}, {squawk}"
    else:
        joiner = " via the " if sid else " "
        text = f"{callsign}, {hello}cleared to {where}{joiner}{route}{runway_part}, climb {level}, {squawk}"
    return Reply(f"{text}{atis_part}", {"squawk": flight.squawk, "altitude": initial}, on_correct)


# ---- who is this controller to us -------------------------------------------


def role(engine, station, intent=None) -> str:
    """'departure', 'center' or 'arrival'."""
    if station.kind == "center":
        return "center"
    flight, ident = engine.flight, station.airport.ident
    if intent is not None and intent.intent in ("inbound", "established"):
        return "arrival"
    if flight.arrival and flight.arrival.airport == ident:
        return "arrival"
    plan = flight.plan
    if plan and plan.destination == ident and flight.origin_ident != ident:
        return "arrival"
    if flight.origin_ident == ident:
        return "departure"
    return "arrival"


def handle(engine, intent, station, issue, raw, variation, callsign, hello):
    """A pilot transmission on a departure, centre or approach frequency."""
    if bool(raw.get("on_ground", True)):
        return None
    part = role(engine, station, intent)
    if intent.intent == "request_direct":
        reply = _direct(engine, intent, station, raw, callsign, part)
    elif intent.intent == "request_weather":
        reply = weather_request(engine, station, issue, raw, variation, callsign, hello)
    elif intent.intent in ("request_deviation", "clear_of_weather"):
        reply = _deviation(engine, intent, callsign, part)
    elif part == "departure":
        reply = _departure(engine, intent, station, raw, callsign, hello)
    elif part == "center":
        reply = _center(engine, intent, station, raw, callsign, hello)
    else:
        reply = _arrival(engine, intent, station, issue, raw, variation, callsign, hello)
    return _with_squawk_check(engine, reply, raw, hello)


def _with_squawk_check(engine, reply, raw, hello):
    """On first contact, a radar controller who sees the wrong code says so."""
    flight = engine.flight
    if not reply or not reply.text or not hello or not flight.squawk or flight.squawk_asked or "squawk" not in raw:
        return reply
    if f"{int(raw['squawk']):04d}" == flight.squawk:
        return reply
    flight.squawk_asked = True
    reply.text += f", squawk {say.digits(flight.squawk)}"
    reply.expect = {**reply.expect, "squawk": flight.squawk}
    return reply


# ---- levels -----------------------------------------------------------------


def _next_level(current_cleared: float, feet: float, goal: int) -> int:
    """One step from where the aircraft is (or is cleared to) toward the goal."""
    if goal > feet:
        step = math.ceil((max(current_cleared, feet) + STEP_FT) / 1000) * 1000
        return int(step) if goal - step >= 3000 else goal  # no separate clearance for the last sliver
    step = math.floor((min(current_cleared, feet) - STEP_FT) / 1000) * 1000
    return int(step) if step - goal >= 3000 else goal


def _climb(engine, target: int, ident: str | None, faa: bool, callsign: str, lead: str):
    engine.flight.cleared_ft = target
    verb = "climb and maintain" if faa else "climb"
    return _reply(engine, f"{callsign}, {lead}{verb} {_level(engine, target, ident, faa)}", {"altitude": target})


def _descend(engine, target: int, ident: str | None, faa: bool, callsign: str, lead: str, tail: str = ""):
    flight = engine.flight
    flight.cleared_ft, flight.descent_given = target, True
    verb = "descend and maintain" if faa else "descend"
    return _reply(engine, f"{callsign}, {lead}{verb} {_level(engine, target, ident, faa)}{tail}", {"altitude": target})


def _climb_goal(engine, station) -> int | None:
    plan = engine.flight.plan
    cruise = plan.cruise_ft if plan else None
    if station.kind == "center":
        return cruise
    return min(cruise, DEPARTURE_TOP_FT) if cruise else DEPARTURE_TOP_FT


def _near_cleared(engine, raw) -> bool:
    cleared = engine.flight.cleared_ft
    return cleared is not None and abs(_feet(raw) - cleared) <= NEXT_STEP_WITHIN_FT


# ---- departure and en-route -------------------------------------------------


def _departure(engine, intent, station, raw, callsign, hello):
    flight = engine.flight
    faa = uses_faa_style(station.airport.ident)
    if intent.intent not in (*CHECK_IN, "request_climb"):
        return None
    top = _climb_goal(engine, station)
    lead = f"{hello}{_contact_word(faa)}, " if hello else ""
    if flight.cleared_ft and flight.cleared_ft >= top:
        return _reply(engine, f"{callsign}, {hello}{_contact_word(faa)}") if hello else None
    target = _next_level(flight.cleared_ft or 0, _feet(raw), top)
    return _climb(engine, target, station.airport.ident, faa, callsign, lead)


def _descent_target(engine, destination: Airport | None, raw: dict | None = None) -> int:
    """The level to be at when handed to approach: about 9000 ft above where the approach is
    joined, and never below the minimum off-route altitude of the terrain being crossed."""
    platform = 3000 + (destination.elev_ft if destination else 0)
    target = platform + 9000
    if engine.navfix:
        cells = [(destination.lat, destination.lon)] if destination else []
        if raw and "lat" in raw:
            cells.append((raw["lat"], raw["lon"]))
        target = max([target, *(engine.navfix.grid_safe_ft(*cell) or 0 for cell in cells)])
    return int(math.ceil(target / 1000) * 1000)


def _star(engine, destination: Airport):
    """Choose the STAR and place its level restrictions, once."""
    flight, plan = engine.flight, engine.flight.plan
    if flight.constraints is None:
        flight.star_procedure = choose_star(engine.procedures(destination.ident), plan.route, plan.star) if plan else None
        flight.constraints = restrictions.build(engine, flight.star_procedure, destination) if flight.star_procedure else []
    return flight.star_procedure


def _announce_star(engine, destination: Airport) -> str:
    """', Inpip one Echo arrival', the first time a descent is given."""
    flight = engine.flight
    star = _star(engine, destination)
    if not star or flight.star:
        return ""
    flight.star = star.name
    return f", {spoken_name(star, engine.navaids, destination.lat, destination.lon)} arrival"


def _descent(engine, station, raw, callsign, lead: str, faa: bool):
    """The first descent clearance from the centre, with the arrival."""
    flight, plan = engine.flight, engine.flight.plan
    destination = engine.db.airport(plan.destination) if plan and plan.destination else None
    goal = _descent_target(engine, destination, raw)
    if goal >= _feet(raw) - 500:
        return None
    target = _next_level(_feet(raw), _feet(raw), goal)
    tail = _announce_star(engine, destination) if destination and plan else ""
    del flight
    return _descend(engine, target, destination.ident if destination else None, faa, callsign, lead, tail)


def _center(engine, intent, station, raw, callsign, hello):
    flight, plan = engine.flight, engine.flight.plan
    faa = _centre_faa(station)
    cruise = plan.cruise_ft if plan else None
    lead = f"{hello}{_contact_word(faa)}, " if hello else ""
    if intent.intent == "request_descent":
        if intent.altitude and intent.altitude < _feet(raw) - 500:
            return _descend(engine, int(intent.altitude), None, faa, callsign, lead)
        return _descent(engine, station, raw, callsign, lead, faa)
    if intent.intent == "request_climb":
        target = int(intent.altitude) if intent.altitude else cruise
        if target and target <= 45000 and target > _feet(raw):
            flight.descent_given = False
            return _climb(engine, target, None, faa, callsign, lead)
        return None
    if intent.intent not in CHECK_IN:
        return None
    if not flight.descent_given and cruise and (flight.cleared_ft or 0) < cruise:
        return _climb(engine, _next_level(flight.cleared_ft or 0, _feet(raw), cruise), None, faa, callsign, lead)
    return _reply(engine, f"{callsign}, {hello}{_contact_word(faa)}") if hello else None


def _direct(engine, intent, station, raw, callsign, part: str):
    """'Request direct BIBAX': granted for a waypoint on the filed route, outside radar vectors."""
    flight, plan = engine.flight, engine.flight.plan
    fix = (intent.fix or "").upper()
    if not plan or not fix or fix not in route_fixes(plan.route):
        return None
    if part == "arrival" and flight.arrival and flight.arrival.stage in ("downwind", "base", "intercept"):
        return _reply(engine, f"{callsign}, unable direct, continue present heading")
    flight.direct_to = fix
    return _reply(engine, f"{callsign}, route direct {_fix_word(engine, fix, raw.get('lat', 0.0), raw.get('lon', 0.0))}")


# ---- weather ----------------------------------------------------------------


def _sentences(*parts: str) -> str:
    """ATIS sentences as one radio phrase: lower case, commas instead of full stops."""

    def soften(part: str) -> str:
        part = part.rstrip(".")
        return part[0].lower() + part[1:] if part[1:2].islower() else part  # "Q N H" stays as it is

    return ", ".join(soften(part) for part in parts if part)


def weather_request(engine, station, issue, raw, variation, callsign, hello):
    """Wind and pressure at this controller's airport, or the destination's weather from a centre."""
    if station.airport and issue:
        faa = uses_faa_style(station.airport.ident)
        wind = engine._wind(issue, variation, faa)
        return _reply(engine, f"{callsign}, {hello}{wind}, {engine._pressure(issue, faa)[0]}")
    plan = engine.flight.plan
    destination = engine.db.airport(plan.destination) if plan and plan.destination else None
    reported = None
    if destination and engine.metars and raw.get("wx_source") == 1:
        reported = engine.metars.weather(destination.ident, destination.elev_ft)
    if not reported:
        return None
    faa = uses_faa_style(destination.ident)
    summary = _sentences(
        atis_text._wind(reported, variation),
        atis_text._visibility(reported, faa),
        "Thunderstorm" if reported.thunderstorm else "",
        atis_text._clouds(reported, faa),
        atis_text._pressure(reported, faa),
    )
    return _reply(engine, f"{callsign}, {hello}{destination.spoken_name} weather, {summary}")


def _deviation(engine, intent, callsign, part: str):
    """Leaving the cleared track to go around weather, and coming back."""
    flight = engine.flight
    if intent.intent == "clear_of_weather":
        if not flight.deviating:
            return None
        flight.deviating = False
        direct = f" direct {flight.direct_to.title()}" if flight.direct_to else ""
        return _reply(engine, f"{callsign}, roger, resume own navigation{direct}")
    if part == "arrival" and flight.arrival and flight.arrival.stage in ("base", "intercept"):
        return _reply(engine, f"{callsign}, unable, continue present heading")
    flight.deviating = True
    side = f"{intent.direction} of track" if intent.direction else "left or right of track"
    return _reply(engine, f"{callsign}, deviation {side} approved, report clear of weather")


# ---- arrival: vectors and the approach clearance ----------------------------


def _setup_arrival(engine, station, issue, raw, intent) -> Arrival | None:
    flight, airport, plan = engine.flight, station.airport, engine.flight.plan
    active = engine.active_runways(airport, issue)
    if not active:
        return None
    if plan and plan.arrival_runway in active and plan.destination == airport.ident:
        runway = plan.arrival_runway
    else:
        runway = engine._landing_runway(airport, issue, raw, intent)
    approach = choose_approach(engine.procedures(airport.ident), runway)
    platform = approach.platform_ft if approach and approach.platform_ft else int(math.ceil((airport.elev_ft + 3000) / 500) * 500)
    course = next(heading for rw in airport.runways for ident, heading in rw.ends() if ident == runway)
    flight.runway = runway
    flight.arrival = Arrival(
        airport.ident, runway, approach.spoken if approach else "visual", platform, course, engine._threshold(airport, runway)
    )
    return flight.arrival


def _speed(arrival: Arrival, stage: str, raw: dict, faa: bool) -> str | None:
    """Speed control as the aircraft is brought onto final. Not said if already that slow."""
    target = SPEEDS_KT.get(stage)
    if not target or (arrival.speed_kt and arrival.speed_kt <= target):
        return None
    arrival.speed_kt = target
    if "ias_kt" in raw and raw["ias_kt"] <= target + 5:
        return None
    spoken = say.digits(target)
    if stage == "intercept":
        return f"maintain {spoken} knots until four mile final" if faa else f"speed {spoken} knots until four miles"
    return f"reduce speed to {spoken}" if faa else f"reduce speed {spoken} knots"


def _vector(engine, station, issue, raw, variation, callsign, lead: str, first: bool):
    """The next vector, descent or approach clearance, or None when nothing new is due."""
    flight, arrival, airport = engine.flight, engine.flight.arrival, station.airport
    faa = uses_faa_style(airport.ident)
    position = locate(raw["lat"], raw["lon"], arrival.threshold, arrival.course_true)
    if arrival.hold:
        return None
    safe = (engine.navfix.airport_safe_ft(airport.ident) if engine.navfix else None) or 0
    vector = next_vector(position, arrival.course_true, arrival.platform_ft, _feet(raw), safe)
    if arrival.stage is not None and STAGES.index(vector.stage) <= STAGES.index(arrival.stage):
        return None if not first else _reply(engine, f"{callsign}, {lead.rstrip(', ')}") if lead else None
    arrival.stage = vector.stage

    parts: list[str] = []
    expect: dict = {}
    if vector.heading_true is not None:
        heading = round(((vector.heading_true + variation) % 360) / 5) * 5 % 360 or 360
        current = raw.get("heading_mag", heading)
        if abs((heading - current + 180) % 360 - 180) < 8:
            parts.append(f"fly heading {say.heading(heading)}")  # already pointing that way
        else:
            parts.append(f"turn {turn_direction(current, heading)} heading {say.heading(heading)}")
        expect["heading"] = heading
        arrival.heading, arrival.heading_at, arrival.heading_asked = heading, engine.now(), False
    cleared = flight.cleared_ft if flight.cleared_ft is not None else _feet(raw)
    if vector.altitude_ft < min(cleared, _feet(raw) + 200):
        flight.cleared_ft, flight.descent_given = vector.altitude_ft, True
        transition = _transition_ft(engine, airport.ident, faa)
        text = f"descend {'and maintain ' if faa else ''}{say.level(vector.altitude_ft, transition, faa)}"
        if vector.altitude_ft <= transition and not arrival.qnh_given and issue:
            arrival.qnh_given = True
            text += f", {engine._pressure(issue, faa)[0]}"
        parts.append(text)
        expect["altitude"] = vector.altitude_ft
    speed = _speed(arrival, vector.stage, raw, faa)
    runway = say.runway(arrival.runway)
    if vector.stage == "intercept":
        parts.append(f"{say.digits(round(position.distance_nm))} miles from touchdown")
        parts.append(f"cleared {arrival.approach} approach runway {runway}")
        if speed:
            parts.append(speed)
        parts.append("report established")
        expect["runway"] = arrival.runway
    else:
        if speed:
            parts.append(speed)
        if first:
            parts.append(f"expect {arrival.approach} approach runway {runway}")
    if not parts:
        return None
    return _reply(engine, f"{callsign}, {lead}{', '.join(parts)}", expect)


def _to_tower(engine, station, callsign):
    arrival = engine.flight.arrival
    arrival.stage = "tower"
    target = engine._handoff(station.airport, "tower")
    if not target:
        return None
    return _reply(engine, f"{callsign}, contact {target[0]} {target[1]}", {"frequency": target[2]})


def _arrival(engine, intent, station, issue, raw, variation, callsign, hello):
    flight = engine.flight
    faa = uses_faa_style(station.airport.ident)
    if "lat" not in raw:
        return None
    if intent.intent == "established":
        if flight.arrival and flight.arrival.stage == "intercept":
            return _to_tower(engine, station, callsign)
        return None
    if intent.intent == "cancel_hold":
        if flight.arrival and flight.arrival.hold:
            return _leave_hold(engine, station, issue, raw, variation, callsign)
        return None
    if intent.intent not in (*CHECK_IN, "going_around", "request_hold"):  # going around: back from the tower
        return None
    if not flight.arrival or flight.arrival.airport != station.airport.ident:
        if _setup_arrival(engine, station, issue, raw, intent) is None:
            return None
    arrival = flight.arrival
    lead = f"{hello}{_contact_word(faa)}, " if hello else ""
    if flight.plan and flight.plan.destination == station.airport.ident:
        _star(engine, station.airport)
    asked = intent.intent == "request_hold"
    if not arrival.hold and arrival.stage in (None, "descend") and (asked or holding.wanted(engine, arrival)):
        found = holding.candidate(engine, station.airport, raw)
        if found:
            minutes = None if asked else engine._rng.choice(holding.DELAYS_MIN)

            def level_text(feet):
                return _level(engine, feet, station.airport.ident, faa)

            return holding.issue(engine, station, raw, callsign, lead, found, minutes, level_text)
        if asked:
            return _reply(engine, f"{callsign}, {lead}unable to hold, expect vectors")
    if asked or arrival.hold:
        return _reply(engine, f"{callsign}, {lead.rstrip(', ')}") if lead else None
    return _vector(engine, station, issue, raw, variation, callsign, lead, first=True)


def _leave_hold(engine, station, issue, raw, variation, callsign):
    """End the hold and go straight to vectors."""
    arrival = engine.flight.arrival
    arrival.hold, arrival.stage = None, None
    reply = _vector(engine, station, issue, raw, variation, callsign, "leave the hold, ", first=False)
    return reply or _reply(engine, f"{callsign}, leave the hold, expect vectors")


def go_around(engine, station, callsign):
    """Tower's reply to an instrument flight going around: climb, back to approach for new vectors."""
    flight, airport = engine.flight, station.airport
    arrival = flight.arrival
    target = engine._handoff(airport, "approach")
    if not arrival or not target:
        return None
    faa = uses_faa_style(airport.ident)
    arrival.stage, arrival.heading, arrival.speed_kt = None, None, None
    flight.cleared_ft, flight.awaiting_landing = arrival.platform_ft, False
    level = _level(engine, arrival.platform_ft, airport.ident, faa)
    verb = "climb and maintain" if faa else "climb"
    text = f"{callsign}, roger, fly runway heading, {verb} {level}, contact {target[0]} {target[1]}"
    return _reply(engine, text, {"altitude": arrival.platform_ft, "frequency": target[2]})


# ---- calls the controller makes unprompted ----------------------------------


def after_takeoff(engine, airport: Airport, raw: dict):
    """Who the tower hands a departing IFR flight to: departure, else approach, else the centre."""
    for kind in ("departure", "approach"):
        target = engine._handoff(airport, kind)
        if target:
            return target
    center = engine.centers.at(raw["lat"], raw["lon"], _feet(raw)) if engine.centers and "lat" in raw else None
    if center:
        station = engine.centers.station_for(center)
        return station.callsign, say.frequency(station.khz), f"{station.khz / 1000:.3f}"
    return None


def _centre_handoff(engine, raw, callsign, exclude=None):
    if not engine.centers:
        return None
    center = engine.centers.at(raw["lat"], raw["lon"], _feet(raw))
    if center is None or center is exclude or engine.flight.told_sector == center.name:
        return None
    engine.flight.told_sector = center.name
    station = engine.centers.station_for(center)
    text = f"{callsign}, contact {station.callsign} {say.frequency(station.khz)}"
    return _reply(engine, text, {"frequency": f"{station.khz / 1000:.3f}"})


def _watch(engine, station, raw, callsign):
    """Things a radar controller notices: a level bust, an assigned heading not being flown."""
    flight, arrival = engine.flight, engine.flight.arrival
    feet, rate, cleared = _feet(raw), raw.get("vs_fpm", 0.0), flight.cleared_ft
    faa = _faa(station)
    ident = station.airport.ident if station.airport else None
    if cleared is not None and flight.bust_called_for != cleared:
        climbing_through = feet > cleared + BUST_FT and rate > 300
        descending_through = feet < cleared - BUST_FT and rate < -300
        if climbing_through or descending_through:
            flight.bust_called_for = cleared
            text = f"{callsign}, check your {'altitude' if faa else 'level'}, cleared {_level(engine, cleared, ident, faa)}"
            return _reply(engine, text)  # a prompt to correct, not an instruction to read back
    height = raw["agl_m"] / 0.3048 if "agl_m" in raw else None
    if height is not None:
        on_approach = arrival is not None and arrival.stage in ("intercept", "tower")
        if height > 2.5 * LOW_AGL_FT:
            flight.low_altitude_called = False
        elif height < LOW_AGL_FT and rate < -500 and not on_approach and not flight.low_altitude_called:
            flight.low_altitude_called = True
            safe = engine.navfix.grid_safe_ft(raw["lat"], raw["lon"]) if engine.navfix else None
            tail = f", minimum safe altitude {say.altitude(safe)} feet" if safe else ""
            return _reply(engine, f"{callsign}, low altitude alert, check your altitude immediately{tail}")
    if (
        arrival
        and arrival.heading is not None
        and arrival.stage in ("downwind", "base")
        and not arrival.heading_asked
        and engine.now() - arrival.heading_at > HEADING_GRACE_S
        and "heading_mag" in raw
        and abs((raw["heading_mag"] - arrival.heading + 180) % 360 - 180) > 25
    ):
        arrival.heading_asked = True
        return _reply(engine, f"{callsign}, fly heading {say.heading(arrival.heading)}", {"heading": arrival.heading})
    return None


def tick(engine, station, issue, raw, variation, callsign):
    """Hand-offs, the next level, the descent clearance and the next vector, as the flight progresses."""
    flight, plan = engine.flight, engine.flight.plan
    if bool(raw.get("on_ground", True)) or "lat" not in raw:
        return None
    part = role(engine, station)
    if station.key not in flight.contacted:
        return None  # the controller speaks first only to an aircraft that has checked in
    noticed = _watch(engine, station, raw, callsign)
    if noticed:
        return noticed
    faa = _faa(station)

    if part == "departure":
        airport = station.airport
        top = _climb_goal(engine, station)
        away = distance_nm(raw["lat"], raw["lon"], airport.lat, airport.lon)
        cleared = flight.cleared_ft or 0
        if cleared < top and _near_cleared(engine, raw) and away <= DEPARTURE_HANDOFF_NM:
            return _climb(engine, _next_level(cleared, _feet(raw), top), airport.ident, faa, callsign, "")
        if away > DEPARTURE_HANDOFF_NM or (cleared >= top and _feet(raw) >= cleared - 1500):
            return _centre_handoff(engine, raw, callsign)
        return None

    if part == "center":
        destination = engine.db.airport(plan.destination) if plan and plan.destination else None
        remaining = distance_nm(raw["lat"], raw["lon"], destination.lat, destination.lon) if destination else None
        if destination and remaining <= APPROACH_HANDOFF_NM and not flight.told_approach:
            flight.told_approach = True
            target = engine._handoff(destination, "approach") or engine._handoff(destination, "tower")
            if target:
                return _reply(engine, f"{callsign}, contact {target[0]} {target[1]}", {"frequency": target[2]})
        # An aircraft that has left the sector is the next controller's to climb or descend.
        if not station.center.contains(raw["lat"], raw["lon"]):
            return _centre_handoff(engine, raw, callsign, exclude=station.center)
        cruise = plan.cruise_ft if plan else None
        cleared = flight.cleared_ft or 0
        if not flight.descent_given and cruise and cleared < cruise and _near_cleared(engine, raw):
            return _climb(engine, _next_level(cleared, _feet(raw), cruise), None, faa, callsign, "")
        if destination and plan:
            _star(engine, destination)
            crossed = restrictions.update_passed(flight.constraints, raw, destination)
            if crossed and restrictions.late(crossed, _feet(raw)):
                level = _level(engine, crossed.altitude_ft, destination.ident, faa)
                word = _fix_word(engine, crossed.fix, crossed.lat, crossed.lon)
                return _reply(engine, f"{callsign}, you were to be level {level} by {word}, expedite descent")
            ahead = restrictions.next_ahead(flight.constraints, _feet(raw))
            if ahead and not ahead.issued and restrictions.due(ahead, raw, _feet(raw)):
                ahead.issued = True
                word = _fix_word(engine, ahead.fix, ahead.lat, ahead.lon)
                tail = f", level by {word}{_announce_star(engine, destination)}"
                return _descend(engine, ahead.altitude_ft, destination.ident, faa, callsign, "", tail)
            if any(not c.passed for c in flight.constraints):
                return None  # the arrival's restrictions set the pace
        if destination:
            goal = _descent_target(engine, destination, raw)
            if not flight.descent_given:
                # Three degrees is about 300 ft per mile; ten miles in hand for slowing down.
                start_nm = (_feet(raw) - goal) / 300 + APPROACH_HANDOFF_NM + 10
                if remaining <= start_nm:
                    return _descent(engine, station, raw, callsign, "", faa)
            elif cleared > goal and _near_cleared(engine, raw):
                return _descend(engine, _next_level(cleared, _feet(raw), goal), destination.ident, faa, callsign, "")
        return None

    arrival = flight.arrival
    if arrival and arrival.airport == station.airport.ident and arrival.hold:
        if holding.progress(engine, raw) == "release":
            return _leave_hold(engine, station, issue, raw, variation, callsign)
        return None
    if not arrival or arrival.airport != station.airport.ident or arrival.stage in (None, "tower"):
        return None
    if arrival.stage == "intercept":
        position = locate(raw["lat"], raw["lon"], arrival.threshold, arrival.course_true)
        if established(position) or 0 < position.along_nm < FINAL_NM - 4:
            return _to_tower(engine, station, callsign)
        return None
    return _vector(engine, station, issue, raw, variation, callsign, "", first=False)


def suggestions(engine, station, raw, add, callsign) -> None:
    """Click-to-transmit calls for radar frequencies."""
    flight, plan = engine.flight, engine.flight.plan
    if bool(raw.get("on_ground", True)):
        return
    feet = _feet(raw)
    faa = _faa(station)
    ident = station.airport.ident if station.airport else None
    passing = _level(engine, int(round(feet / 100) * 100), ident, faa)
    part = role(engine, station)
    cleared = flight.cleared_ft
    if part == "arrival":
        add("Check in", f"descending {_level(engine, cleared, ident, faa)}" if cleared else f"passing {passing}", "check_in")
        if flight.arrival and flight.arrival.hold:
            add("Ready for approach", "ready for the approach", "cancel_hold")
        elif not flight.arrival or flight.arrival.stage in (None, "descend"):
            add("Request hold", "request holding", "request_hold")
        if flight.arrival and flight.arrival.stage == "intercept":
            add(
                "Established",
                f"established {flight.arrival.approach} runway {say.runway(flight.arrival.runway)}",
                "established",
            )
        return
    if cleared and abs(cleared - feet) > 300:
        verb = "climbing" if cleared > feet else "descending"
        add("Check in", f"passing {passing} {verb} {_level(engine, cleared, ident, faa)}", "check_in")
    else:
        add("Check in", f"level {passing}" if cleared else f"passing {passing}", "check_in")
    if part == "center":
        if not flight.descent_given:
            add("Request descent", "request descent", "request_descent")
        add("Request climb", "request higher", "request_climb")
    if flight.deviating:
        add("Clear of weather", "clear of weather", "clear_of_weather")
    else:
        add("Deviate left", "request deviation left of track due weather", "request_deviation", direction="left")
        add("Deviate right", "request deviation right of track due weather", "request_deviation", direction="right")
    if part == "center" and plan and plan.destination:
        add("Destination weather", "request the weather at destination", "request_weather")
    fixes = route_fixes(plan.route) if plan else []
    if fixes and flight.direct_to != fixes[-1]:
        word = _fix_word(engine, fixes[-1], raw.get("lat", 0.0), raw.get("lon", 0.0))
        add(f"Request direct {fixes[-1]}", f"request direct {word}", "request_direct", fix=fixes[-1])
