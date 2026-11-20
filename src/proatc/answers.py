"""Questions every controller answers, and what a controller says when it has no rule for a call.

A pilot asks to confirm the cleared level, the runway in use, the approach to expect, the frequency of
the last hand-off. Each answer is read from what the engine itself assigned (`Flight`, `ifr.Arrival`) or
from the sim's data: nothing is invented, and "no heading assigned" is said when there is none.

When no rule covers a call, the controller still answers: "unable", and who to call or what this
controller can do. The language model's free-form reply is left for speech nobody foresaw.

Functions over the Engine, like ifr.py and vfr.py; every one runs under the engine's lock.
"""

from __future__ import annotations

import math

from . import ifr
from . import phraseology as say
from .atis import (
    RUNWAY_CROSSWIND_KT,
    RUNWAY_TAILWIND_KT,
    really_closed,
    really_in_use,
    split_runways,
    uses_faa_style,
    wind_on_runway,
)
from .navdata import distance_nm
from .procedures import choose_approach
from .weather import from_snapshot

# What each request is called in "unable ..." and "contact ... for ...".
WORDS = {
    "request_clearance": "clearance", "request_pushback": "push and start", "request_taxi": "taxi",
    "ready_for_departure": "departure", "request_crossing": "crossing", "request_progressive": "progressive taxi",
    "runway_vacated": "taxi", "request_return": "taxi", "inbound": "landing", "position_report": "landing",
    "request_touch_and_go": "touch and go", "request_low_approach": "low approach", "request_circuits": "circuits",
    "request_special_vfr": "special V F R", "request_transit": "transit", "request_climb": "higher",
    "request_descent": "lower", "request_direct": "direct", "request_hold": "holding", "request_deviation": "deviation",
    "request_approach": "approach", "request_visual": "visual approach", "request_diversion": "diversion",
    "request_flight_following": "radar service", "request_runway": "runway change", "report_leaving": "frequency change",
    "request_weather": "weather information", "request_vectors": "vectors",
}  # fmt: skip
# Which controller a request belongs to: the one to send the pilot to when this one cannot help.
BELONGS = {
    "request_clearance": "clearance", "request_pushback": "ground", "request_taxi": "ground", "request_crossing": "ground",
    "request_progressive": "ground", "runway_vacated": "ground", "request_return": "ground",
    "ready_for_departure": "tower", "inbound": "tower", "position_report": "tower", "request_touch_and_go": "tower",
    "request_low_approach": "tower", "request_circuits": "tower", "request_special_vfr": "tower",
    "request_climb": "radar", "request_descent": "radar", "request_direct": "radar", "request_hold": "radar",
    "request_deviation": "radar", "request_approach": "radar", "request_visual": "radar", "request_diversion": "radar",
    "request_flight_following": "radar", "request_vectors": "radar",
}  # fmt: skip
NEAR_NM = 30  # the sim's weather at the aircraft stands for an airport's this close
SHORT_RUNWAY_M = 1200  # shorter than this and well short of the airport's longest: not for an airliner
HANDOFF_AGAIN_S = 60  # after "unable" to a hand-off, how long before it is given again
CANCEL_BELOW_MS = 15  # a takeoff is not cancelled by a plain "unable" faster than this (the tower's own threshold)


def _reply(engine, text: str, expect: dict | None = None):
    return ifr._reply(engine, text, expect)


def faa_for(station) -> bool:
    return ifr._faa(station)


def _airborne(raw: dict) -> bool:
    return not bool(raw.get("on_ground", True))


def destination(engine):
    """The airport the flight is going to: its arrival, else its filed destination."""
    flight = engine.flight
    if flight.arrival:
        return engine.db.airport(flight.arrival.airport)
    plan = flight.plan
    return engine.db.airport(plan.destination) if plan and plan.destination else None


def weather_at(engine, airport, raw: dict):
    """An airport's weather as its ATIS would give it: its own report on real weather, else the sim's."""
    if engine.metars and raw.get("wx_source") == 1:
        reported = engine.metars.weather(airport.ident, airport.elev_ft)
        if reported:
            return reported
    if raw.get("wx_source") == 1 and "lat" in raw and distance_nm(raw["lat"], raw["lon"], airport.lat, airport.lon) > NEAR_NM:
        return None  # real weather with no report for that airport: what is around the aircraft is not its weather
    return from_snapshot(raw, airport.elev_ft)


def runways_at(engine, airport, issue, raw: dict) -> list[str]:
    """The runways in use at an airport, from this controller's ATIS when it is the airport's own."""
    weather = issue.weather if issue else weather_at(engine, airport, raw)
    return engine.active_runways(airport, weather=weather)


def _level_ident(engine, station) -> str | None:
    """The airport whose transition altitude decides 'flight level' or 'altitude'."""
    flight = engine.flight
    if station.airport:
        return station.airport.ident
    plan = flight.plan
    if flight.descent_given and plan and plan.destination:
        return plan.destination
    return flight.origin_ident


def _who(engine, kind: str, airport, raw: dict | None = None) -> str | None:
    """'Eastfield Approach one two four decimal three': who to call, and on what."""
    target = engine._handoff(airport, kind, raw) if airport else None
    return f"{target[0]} {target[1]}" if target else None


# ---- information ------------------------------------------------------------------


def information(engine, intent, station, issue, raw, variation, callsign, hello):
    """'Confirm cleared level', 'runway in use?', 'say again the heading': one item, from what the engine knows."""
    item = intent.item
    answer = {
        "altitude": _altitude, "heading": _heading, "speed": _speed, "squawk": _squawk, "runway": _runway,
        "frequency": _frequency, "qnh": _pressure, "clearance": _clearance, "route": _route, "approach": _approach,
        "atis": _atis, "distance": _distance, "wind": _wind, "transition": _transition,
    }.get(item)  # fmt: skip
    if answer is None:
        # "Confirm?" with nothing named is about the last instruction.
        previous = engine.flight.last_said.get(station.key)
        if previous:
            return _reply(engine, previous)
        answer = _altitude if _airborne(raw) and station.kind in ifr.RADAR_KINDS else _runway
    words = answer(engine, intent, station, issue, raw, variation) or "unable, no information"
    return _reply(engine, f"{callsign}, {hello}{words}")


def _altitude(engine, intent, station, issue, raw, variation) -> str:
    flight, faa = engine.flight, faa_for(station)
    word = "altitude" if faa else "level"
    if flight.cleared_ft is None or (not _airborne(raw) and flight.has_been_airborne):
        return f"no {word} assigned"
    arrival = flight.arrival
    if _airborne(raw) and arrival and arrival.stage in ("intercept", "procedure", "tower") and not flight.diverting:
        return f"cleared {arrival.words()}"  # the approach's own levels apply now
    if _airborne(raw) and flight.rules != "IFR" and not flight.plan:
        return f"no {word} assigned, maintain V F R"
    level = ifr._level(engine, flight.cleared_ft, _level_ident(engine, station), faa)
    feet = ifr._feet(raw)
    if not _airborne(raw):
        verb = "climb and maintain" if faa else "climb"
    elif abs(feet - flight.cleared_ft) < 300:
        verb = "maintain"
    elif flight.cleared_ft > feet:
        verb = "climb and maintain" if faa else "climb"
    else:
        verb = "descend and maintain" if faa else "descend"
    if intent.altitude is None:
        return f"{verb} {level}"
    if abs(intent.altitude - flight.cleared_ft) <= 50:
        return f"{'affirmative' if faa else 'affirm'}, {level}"
    return f"negative, {verb} {level}"


def _heading(engine, intent, station, issue, raw, variation) -> str:
    flight = engine.flight
    arrival = flight.arrival
    if not _airborne(raw):
        return "no heading assigned"
    if arrival and arrival.stage in ("procedure", "tower") and not flight.diverting:
        return f"no heading assigned, cleared {arrival.words()}"
    if flight.heading_assigned is not None:
        return f"fly heading {say.heading(flight.heading_assigned)}"
    if arrival and arrival.go_arounds and arrival.stage is None and not flight.diverting:
        published = bool(arrival.missed_ft) and arrival.approach != "visual"
        return "fly the published missed approach" if published else "fly runway heading"
    if flight.direct_to:
        word = ifr._fix_word(engine, flight.direct_to, raw.get("lat", 0.0), raw.get("lon", 0.0))
        return f"no heading assigned, {'proceed' if faa_for(station) else 'route'} direct {word}"
    return "no heading assigned, own navigation"


def _speed(engine, intent, station, issue, raw, variation) -> str:
    flight = engine.flight
    arrival = flight.arrival
    if arrival and arrival.speed_kt and _airborne(raw) and not flight.diverting:
        knots = say.digits(arrival.speed_kt)
        if arrival.stage in ("intercept", "procedure", "tower") and "lat" in raw:
            if distance_nm(raw["lat"], raw["lon"], *arrival.threshold) <= 4.2:
                return "no speed restrictions"  # the last one held only to four miles
            return (
                f"maintain {knots} knots until four mile final" if faa_for(station) else f"speed {knots} knots until four miles"
            )
        return f"maintain {knots} knots" if faa_for(station) else f"speed {knots} knots"
    return "no speed restrictions"


def _squawk(engine, intent, station, issue, raw, variation) -> str:
    code = engine.flight.squawk
    return f"squawk {say.digits(code)}" if code else "no squawk assigned"


def _own_runway(engine, airport) -> str | None:
    """The runway this flight was given at this airport, if any."""
    flight = engine.flight
    if flight.arrival and flight.arrival.airport == airport.ident:
        return flight.arrival.runway
    ends = {end for runway in airport.runways for end, _ in runway.ends()}
    return flight.runway if flight.runway in ends else None


def _runway(engine, intent, station, issue, raw, variation) -> str:
    airport = station.airport or destination(engine)
    if airport is None:
        return "no runway assigned, say destination"
    own = _own_runway(engine, airport)
    weather = issue.weather if issue and station.airport else weather_at(engine, airport, raw)
    if own and weather is not None and acceptable_runway(airport, own, weather):
        own = None  # given earlier, and the wind has changed since: the runway in use is the answer
    landing = _airborne(raw) or engine.flight.has_been_airborne
    if own:
        return f"{'landing' if landing else 'departure'} runway {say.runway(own)}"
    active = runways_at(engine, airport, issue if station.airport else None, raw)
    if not active:
        who = _who(engine, "approach", airport) or _who(engine, "tower", airport)
        return f"{airport.spoken_name} runway in use not known, {who} will advise" if who else "runway in use not known"
    there = "" if station.airport else f"{airport.spoken_name} "
    arriving, leaving = split_runways(active)
    if arriving == leaving:
        return f"{there}runway {say.runway(arriving[0])} in use"
    return f"{there}landing runway {say.runway(arriving[0])}, departure runway {say.runway(leaving[0])}"


def _frequency(engine, intent, station, issue, raw, variation) -> str:
    """The hand-off this controller gave and the pilot has not made yet; otherwise this is the frequency."""
    flight = engine.flight
    told = flight.next_contact
    if flight.next_frequency and told and told[0] == station.key:
        return f"contact {told[1]}"
    return "remain this frequency"


def _pressure(engine, intent, station, issue, raw, variation) -> str:
    faa = faa_for(station)
    if station.airport and issue:
        return engine._pressure(issue, faa)[0]
    airport = destination(engine) if engine.flight.descent_given else None
    weather = weather_at(engine, airport, raw) if airport else from_snapshot(raw, 0.0)
    if weather is None:
        return "pressure not available"
    if faa:
        return f"altimeter {say.digits(f'{round(weather.qnh_hpa * 0.02953 * 100):04d}')}"
    return say.qnh(weather.qnh_hpa)


def _clearance(engine, intent, station, issue, raw, variation) -> str:
    flight = engine.flight
    if flight.rules == "IFR" and flight.destination:
        airport = " airport" if faa_for(station) else ""
        return f"cleared to {flight.destination}{airport}"
    if flight.rules == "VFR" and flight.squawk:
        return "V F R, no clearance limit"
    return "no clearance issued"


def _route(engine, intent, station, issue, raw, variation) -> str:
    flight, faa = engine.flight, faa_for(station)
    if _airborne(raw):
        if flight.direct_to:
            fix = ifr._fix_word(engine, flight.direct_to, raw.get("lat", 0.0), raw.get("lon", 0.0))
            return f"{'proceed' if faa else 'route'} direct {fix}"
        return _clearance(engine, intent, station, issue, raw, variation) + (" as filed" if flight.rules == "IFR" else "")
    if (
        not flight.taxi_goal
        and flight.phase.value == "taxiing in"
        and flight.taxi_airport == getattr(station.airport, "ident", None)
    ):
        return f"taxi to {'the gate' if faa else 'parking'}"  # no stand known: the clearance was to parking
    if not flight.taxi_goal or not station.airport or "lat" not in raw:
        return "no taxi clearance issued"
    route = engine._route_from(station.airport, raw)
    via, limit = engine._route_words(route), engine._limit()
    goal = flight.taxi_goal
    if goal[0] == "runway" and faa:
        return f"runway {say.runway(goal[1])}, taxi{via}{limit}"
    return f"taxi to {engine._goal_words(faa)}{via}{limit}"


def expected_approach(engine, airport, issue, raw: dict) -> str | None:
    """'I L S approach runway two seven', the approach this flight will be given at the airport."""
    flight = engine.flight
    if flight.arrival and flight.arrival.airport == airport.ident:
        return flight.arrival.words()
    active = runways_at(engine, airport, issue, raw)
    if not active:
        return None
    landing = split_runways(active)[0]
    weather = issue.weather if issue else weather_at(engine, airport, raw)
    runway = engine.requested_runway(airport, weather=weather) or landing[0]
    procedures = engine.procedures(airport.ident)
    found = (flight.requested_approach and choose_approach(procedures, runway, flight.requested_approach)) or choose_approach(
        procedures, runway
    )
    return f"{found.spoken if found else 'visual'} approach runway {say.runway(runway)}"


def _approach(engine, intent, station, issue, raw, variation) -> str:
    flight = engine.flight
    arrival = flight.arrival
    if not _airborne(raw) and flight.has_been_airborne:
        return "no approach assigned"  # landed: the approach is over
    airport = destination(engine)
    if airport is None:
        return "no approach assigned, say destination"
    if arrival and arrival.airport == airport.ident and arrival.stage in ("intercept", "procedure", "tower"):
        return f"cleared {arrival.words()}"
    here = station.airport is not None and station.airport.ident == airport.ident
    if flight.diverting and not here:
        # On the way to another airport: its own approach controller gives the approach there.
        who = _who(engine, "approach", airport) or _who(engine, "tower", airport)
        return f"{who} will advise the approach" if who else "no approach assigned"
    expected = expected_approach(engine, airport, issue if here else None, raw)
    if expected:
        return f"expect {expected}" if here else f"{airport.spoken_name}, expect {expected}"
    who = _who(engine, "approach", airport)
    return f"{who} will advise the approach" if who else "no approach information"


def _transition(engine, intent, station, issue, raw, variation) -> str:
    """'How will I join the approach?': by vectors, or direct to an initial approach fix, from the arrival as planned."""
    if not _airborne(raw) and engine.flight.has_been_airborne:
        return "no approach assigned"  # landed: the approach is over
    arrival = engine.flight.arrival
    airport = station.airport if station.kind in ("approach", "tower") else destination(engine)
    if airport is None:
        return "no approach assigned, say destination"
    if arrival and arrival.airport == airport.ident:
        return _joining(engine, arrival, airport, raw)
    here = station.airport is not None and station.airport.ident == airport.ident
    expected = expected_approach(engine, airport, issue if here else None, raw)
    star = _star_words(engine, airport)
    if expected:
        where = "" if here else f"{airport.spoken_name}, "
        return f"{where}{star}expect vectors for the {expected}"
    who = _who(engine, "approach", airport)
    return f"{star}{who} will advise the approach" if who else "no approach information"


def _joining(engine, arrival, airport, raw) -> str:
    """How this arrival joins its approach, at the stage it is at."""
    words = arrival.words()
    if arrival.via:
        fix = ifr._fix_word(engine, arrival.via, raw.get("lat", 0.0), raw.get("lon", 0.0))
        return f"cleared {words} via {fix}"
    if arrival.stage in ("intercept", "tower"):
        return f"cleared {words}"
    if arrival.hold:
        return f"hold as published, then expect vectors for the {words}"
    if arrival.stage in ("downwind", "base"):
        return f"radar vectors for the {words}"
    found = None
    if "lat" in raw:
        from .vectors import locate

        found = ifr._initial_fix(
            engine, airport, arrival, raw, locate(raw["lat"], raw["lon"], arrival.threshold, arrival.course_true)
        )
    if found:
        return f"expect direct {ifr._fix_word(engine, found[0], *found[2])} for the {words}"
    return f"expect vectors for the {words}"


def _star_words(engine, airport) -> str:
    """'the Inpip one Echo arrival, then ' while that arrival is still to be flown."""
    from .procedures import spoken_name

    flight = engine.flight
    procedure = flight.star_procedure
    if not procedure or not flight.star or flight.arrival:
        return ""
    return f"the {spoken_name(procedure, engine.navaids, airport.lat, airport.lon)} arrival, then "


def _atis(engine, intent, station, issue, raw, variation) -> str:
    if station.airport and issue:
        return f"information {say.letter(issue.letter)} is current"
    airport = destination(engine)
    target = engine.db.station_of(airport, "atis") if airport else None
    if target is None:
        return "no A T I S available"
    return f"{airport.spoken_name} information on {say.frequency(target.khz)}"


def _miles(miles: float) -> str:
    rounded = max(1, round(miles))
    return f"{say.digits(rounded)} {'mile' if rounded == 1 else 'miles'}"


def _distance(engine, intent, station, issue, raw, variation) -> str:
    flight = engine.flight
    if "lat" not in raw or not _airborne(raw):
        return ""
    arrival, airport = flight.arrival, station.airport
    if arrival and airport and arrival.airport == airport.ident:
        return f"{_miles(distance_nm(raw['lat'], raw['lon'], *arrival.threshold))} from touchdown"
    if airport and station.kind == "tower" and flight.runway in {e for r in airport.runways for e, _ in r.ends()}:
        return f"{_miles(distance_nm(raw['lat'], raw['lon'], *engine._threshold(airport, flight.runway)))} from touchdown"
    target = destination(engine) or airport
    if target is None:
        return ""
    return f"{_miles(distance_nm(raw['lat'], raw['lon'], target.lat, target.lon))} from {target.spoken_name}"


def _wind(engine, intent, station, issue, raw, variation) -> str:
    if station.airport and issue:
        return engine._wind(issue, variation, faa_for(station))
    airport = destination(engine)
    weather = weather_at(engine, airport, raw) if airport else None
    if weather is None:
        return ""
    from .atis import Issue

    return f"{airport.spoken_name} {engine._wind(Issue('A', weather, 0.0), variation, uses_faa_style(airport.ident))}"


# ---- requests every controller answers ----------------------------------------------


def acceptable_runway(airport, runway: str, weather, raw: dict | None = None) -> str | None:
    """Why this runway cannot be given ('tailwind eight knots'), or None when it can. Without the weather it
    cannot be given: nobody knows the wind."""
    from .taxi import aircraft_category

    ends = {end: heading for rw in airport.runways for end, heading in rw.ends()}
    if runway not in ends:
        return "no such runway"
    if runway in really_closed(airport.ident):
        return "runway closed"
    if runway in really_in_use(airport.ident):
        return None  # the airport is using it now, whatever the wind: its real ATIS says so
    length = next(rw.length_m for rw in airport.runways if runway in (rw.end1, rw.end2))
    longest = max(rw.length_m for rw in airport.runways)
    if raw is not None and length < min(SHORT_RUNWAY_M, 0.6 * longest) and aircraft_category(raw.get("aircraft_type")) != "props":
        return "runway too short"
    if weather is None:
        return "wind not known"
    if weather.wind_kt < 1:
        return None
    tail, cross = wind_on_runway(ends[runway], weather.wind_dir_true, weather.wind_kt)
    if tail > RUNWAY_TAILWIND_KT:
        return f"tailwind {say.digits(round(tail))} knots"
    if cross > RUNWAY_CROSSWIND_KT:
        return f"crosswind {say.digits(round(cross))} knots"
    return None


def unable(engine, station, raw, callsign, hello, faa: bool):
    """The pilot cannot comply. The controller cancels the instruction or gives what can be flown instead."""
    from .engine import Phase

    flight = engine.flight
    pending = flight.pending if flight.pending and flight.pending_key == station.key else None
    if pending is None:
        # Read back a moment ago: still the instruction this station gave last, if it said nothing since.
        text, expect, given = flight.last_instruction.get(station.key, ("", {}, 0))
        # Stale once another station has given an instruction since (handed on, cleared by someone else).
        since = any(entry[2] > given for key, entry in flight.last_instruction.items() if key != station.key)
        if not text or flight.last_said.get(station.key) != text or since:
            text, expect = "", {}
    else:
        text, expect = pending.text, pending.expect
        flight.pending = None
    tower = station.kind == "tower" and station.airport is not None
    rolling = raw.get("groundspeed_ms", 0.0) >= CANCEL_BELOW_MS
    if not _airborne(raw) and flight.phase == Phase.TAKEOFF_CLEARED and tower:
        if rolling:
            return _reply(engine, f"{callsign}, {hello}roger")  # on the takeoff roll: the pilot's call to stop
        flight.phase, flight.waiting_takeoff = Phase.TAXI_OUT, False
        cancel = "cancel takeoff clearance, " if faa else "cancel takeoff, "
        return _reply(engine, f"{callsign}, {hello}roger, hold position, {cancel}report ready for departure")
    if "altitude" in expect and _airborne(raw):
        feet = ifr._feet(raw)
        # The nearest whole thousand feet the aircraft is moving towards: a level it can hold.
        rising = expect["altitude"] > feet
        level_ft = int((math.ceil if rising else math.floor)(feet / 1000) * 1000)
        flight.cleared_ft = level_ft
        level = ifr._level(engine, level_ft, _level_ident(engine, station), faa)
        reply = _reply(engine, f"{callsign}, {hello}roger, maintain {level}, advise when able", {"altitude": level_ft})
        reply.asks = {"altitude": expect["altitude"]}  # what "able now" will be given
        return reply
    if "heading" in expect and _airborne(raw):
        flight.heading_assigned = None
        if flight.arrival:
            flight.arrival.heading = None  # no longer watched
        reply = _reply(engine, f"{callsign}, {hello}roger, continue present heading, advise when able to turn")
        reply.asks = {"heading": expect["heading"]}
        return reply
    if expect.keys() & {"hold_short", "crossing"} and "cross runway" in text and flight.crossing_given:
        if flight.crossing_given not in flight.crossings:
            flight.crossings.insert(0, flight.crossing_given)  # not crossed after all: still to be cleared
        # Not cleared again by itself while the aircraft waits there: the pilot asks when able.
        flight.crossing_refused, flight.crossing_given = flight.crossing_given, None
    taxiing = not _airborne(raw) and flight.phase in (Phase.TAXI_OUT, Phase.TAXI_IN)
    if station.kind in ("ground", "tower") and (expect.keys() & {"hold_short", "crossing"} or taxiing):
        return _reply(engine, f"{callsign}, {hello}roger, hold position")
    if "frequency" in expect and flight.next_frequency == expect["frequency"]:
        return _unable_handoff(engine, station, callsign, hello)  # a hand-off not made yet
    landing_here = tower and flight.runway and flight.runway in {e for rw in station.airport.runways for e, _ in rw.ends()}
    if flight.phase == Phase.LANDING_CLEARED and _airborne(raw) and landing_here and "runway" in expect:
        flight.phase, flight.awaiting_landing = Phase.AIRBORNE, False
        return _reply(engine, f"{callsign}, {hello}roger, landing clearance cancelled, say intentions")
    if not expect or "frequency" in expect:
        return _reply(engine, f"{callsign}, {hello}say again")  # nothing outstanding from this controller
    return _reply(engine, f"{callsign}, {hello}roger, advise when able")


def _unable_handoff(engine, station, callsign, hello):
    """The pilot cannot take the hand-off: stay here, and the controller gives it again a little later."""
    flight = engine.flight
    flight.next_frequency = None
    arrival = flight.arrival
    if arrival and arrival.stage == "tower" and station.kind == "approach":
        arrival.stage = "procedure" if arrival.via else "intercept"  # approach keeps the flight until it can go
    if station.kind in ifr.RADAR_KINDS:
        flight.told_sector, flight.sector_ahead = None, None
        if not arrival:
            flight.told_approach = False
        flight.handoffs_held_until = engine.now() + HANDOFF_AGAIN_S
    elif station.kind == "tower":
        flight.told_departure = False
    return _reply(engine, f"{callsign}, {hello}roger, remain this frequency")


def delay(engine, station, raw, callsign, hello, faa: bool):
    """'Not ready yet' / 'request a few minutes': the controller waits for the call."""
    from .engine import Phase

    flight = engine.flight
    if flight.phase == Phase.TAKEOFF_CLEARED and station.kind == "tower":
        return unable(engine, station, raw, callsign, hello, faa)
    if flight.phase == Phase.TAKEOFF_CLEARED:
        return _reply(engine, f"{callsign}, {hello}roger")  # the tower's clearance: not this controller's to cancel
    flight.waiting_takeoff = False
    return _reply(engine, f"{callsign}, {hello}roger, {'advise' if faa else 'report'} ready")


def leave_frequency(engine, station, raw, callsign, hello, faa: bool):
    """'Request frequency change': a visual flight is released; an instrument flight may step away and come back.
    Not while this controller is working the flight: cleared for takeoff or to land, being vectored, an instruction
    still to be read back."""
    from .engine import Phase

    flight = engine.flight
    arrival = flight.arrival
    busy = (
        flight.phase in (Phase.TAKEOFF_CLEARED, Phase.LANDING_CLEARED)
        or flight.waiting_takeoff
        or flight.awaiting_landing
        or (flight.pending is not None and flight.pending_key == station.key)
        or (flight.phase == Phase.TAXI_OUT and station.kind == "tower" and not _airborne(raw))
        or (station.kind == "tower" and _airborne(raw) and flight.rules == "IFR")
        or (
            station.kind == "approach" and arrival is not None and station.airport is not None
            and arrival.airport == station.airport.ident and arrival.stage in ("downwind", "base", "intercept", "procedure")
        )
    )  # fmt: skip
    if busy:
        return _reply(engine, f"{callsign}, {hello}unable, remain this frequency")
    if flight.rules == "IFR" or (flight.rules is None and flight.plan and flight.plan.destination):
        flight.away_from = station.key  # quiet until the pilot calls back
        return _reply(engine, f"{callsign}, {hello}approved, report back on frequency")
    if flight.following:
        flight.following = False
        flight.squawk = "1200" if faa else "7000"
        code = say.digits(flight.squawk)
        return _reply(engine, f"{callsign}, {hello}radar service terminated, squawk {code}, frequency change approved")
    return _reply(engine, f"{callsign}, {hello}frequency change approved")


# ---- when no rule covers the call -------------------------------------------------


def _radar_for(engine, raw):
    """The radar controller that works the aircraft where it is: departure, approach or the centre."""
    flight = engine.flight
    origin = engine.db.airport(flight.origin_ident) if flight.origin_ident else None
    if origin and "lat" in raw and distance_nm(raw["lat"], raw["lon"], origin.lat, origin.lon) <= ifr.DEPARTURE_HANDOFF_NM:
        found = engine._handoff(origin, "departure") or engine._handoff(origin, "approach")
        if found:
            return found
    landing = destination(engine)
    if landing and "lat" in raw and distance_nm(raw["lat"], raw["lon"], landing.lat, landing.lon) <= ifr.APPROACH_HANDOFF_NM:
        found = engine._handoff(landing, "approach")
        if found:
            return found
    center = engine.centers.at(raw["lat"], raw["lon"], ifr._feet(raw)) if engine.centers and "lat" in raw else None
    if center:
        target = engine.centers.station_for(center)
        return target.callsign, say.frequency(target.khz), f"{target.khz / 1000:.3f}"
    return None


def _owner(engine, intent, station, raw, gives_clearance: bool):
    """(callsign, spoken frequency, 'nnn.nnn') of the controller this request belongs to, when it is another one."""
    wanted = BELONGS.get(intent.intent)
    airport = station.airport
    if wanted == "radar":
        if not _airborne(raw) or station.kind in ifr.RADAR_KINDS:
            return None
        return _radar_for(engine, raw)
    if airport is None or wanted is None or station.kind == wanted:
        return None
    if _airborne(raw) and wanted in ("clearance", "delivery", "ground"):
        return None  # in the air nobody is sent to a ground frequency: the controller says what it can do instead
    if wanted == "clearance":
        if gives_clearance:
            return None
        return engine._handoff(airport, "delivery") or engine._handoff(airport, "ground")
    found = engine._handoff(airport, wanted)
    return found if found and found[0] != station.callsign else None


# How a controller asks back about a call it is not sure of: "confirm you …".
DECLARING = {"mayday": "you are declaring a mayday", "pan_pan": "you are declaring a pan pan",
             "cancel_emergency": "you are cancelling the emergency", "going_around": "you are going around",
             "cancel_flight_following": "you are cancelling radar service",
             "report_leaving": "you request to leave the frequency"}  # fmt: skip


def confirm_words(engine, intent, station, raw) -> str:
    """What the controller asks the pilot to confirm, in its own words: "you request descent to flight level two
    zero zero". Only values the call carries are said. Empty when the call cannot be put as a request."""
    name = intent.intent
    if name in DECLARING:
        return DECLARING[name]
    faa, ident = faa_for(station), _level_ident(engine, station)
    if name in ("request_climb", "request_descent") and intent.altitude:
        verb = "climb" if name == "request_climb" else "descent"
        return f"you request {verb} to {ifr._level(engine, int(intent.altitude), ident, faa)}"
    if name == "request_runway" and intent.runway:
        return f"you request runway {say.runway(intent.runway)}"
    # Nothing the model wrote is spoken as it came: a waypoint is said only when it is on the flight's route, an
    # airport only by the name the scenery gives it. Otherwise the request is asked about without it.
    if name == "request_direct":
        fix, plan = (intent.fix or "").upper(), engine.flight.plan
        known = (set(ifr.route_fixes(plan.route)) | {name for name, _, _ in engine.route()}) if plan else set()
        if fix in known:
            here = (raw.get("lat", 0.0), raw.get("lon", 0.0))
            return f"you request direct {ifr._fix_word(engine, fix, *here)}"
        return "you request a direct routing"
    if name == "request_diversion":
        asked = (intent.destination or "").strip().upper()
        target = engine.db.airport(asked) if len(asked) == 4 and asked.isalnum() else None
        return f"you request a diversion to {target.spoken_name}" if target else "you request a diversion"
    if name == "request_approach" and intent.approach in ifr.FAMILY_WORDS:
        return f"you request the {ifr.FAMILY_WORDS[intent.approach]} approach"
    if name == "request_deviation" and intent.direction in ("left", "right"):
        return f"you request a deviation {intent.direction} of track"
    if name == "request_return":
        return "you request to return"
    words = WORDS.get(name)
    return f"you request {words}" if words else ""


def way_out(engine, intent, station, raw, callsign, hello):
    """No rule covers this call. The controller says who can help, or 'unable' and what it can do: never nothing."""
    from .vfr import service

    if intent.intent in ("other", "traffic_call"):
        return None  # unforeseen speech, or a broadcast on the wrong frequency: the free-form reply handles it
    if station.airport and service(station) == "afis":
        return _reply(engine, f"{callsign}, {hello}roger")  # information only: it neither grants nor refuses
    words = WORDS.get(intent.intent, "")
    kinds = {freq.kind for freq in station.airport.freqs} if station.airport else set()
    owner = _owner(engine, intent, station, raw, bool(station.airport) and engine._gives_clearance(station, kinds, raw))
    if owner:
        reason = f" for {words}" if words else ""
        return _reply(engine, f"{callsign}, {hello}contact {owner[0]} {owner[1]}{reason}", {"frequency": owner[2]})
    if not words:
        return _reply(engine, f"{callsign}, {hello}roger")  # a report or a cancellation with nothing to act on
    return _reply(engine, f"{callsign}, {hello}unable {words}")


# ---- runways and approaches asked for ------------------------------------------------


def afis_runway(engine, intent, station, issue, raw, variation, callsign, hello):
    """Asked of an information service: it gives no runway, only what it knows. The choice is the pilot's."""
    active = engine.active_runways(station.airport, issue)
    parts = [f"runway {say.runway(active[0])} in use"] if active else []
    wind = engine._wind(issue, variation, False)
    parts += [wind] if wind else []
    return _reply(engine, f"{callsign}, {hello}{', '.join(parts) or 'roger'}, runway at your discretion")


def runway_request(engine, intent, station, issue, raw, callsign, hello, faa: bool):
    """'Request runway two seven': given when the runway exists and the wind allows it, else declined with the reason."""
    from .engine import Phase

    flight = engine.flight
    airport = station.airport or destination(engine)
    if airport is None:
        return None
    wanted = intent.runway
    if not wanted:
        # "Request runway assignment" names none: the runway this flight has is said, and which one it wants is asked.
        own = _own_runway(engine, airport)
        landing = _airborne(raw) or flight.has_been_airborne
        given = f"{'landing' if landing else 'departure'} runway {say.runway(own)}, " if own else ""
        return _reply(engine, f"{callsign}, {hello}{given}say the runway you request")
    here = station.airport is not None
    weather = issue.weather if issue and here else weather_at(engine, airport, raw)
    why = acceptable_runway(airport, wanted, weather, raw)
    spoken = say.runway(wanted)
    if why == "wind not known":
        who = None if here else _who(engine, "approach", airport) or _who(engine, "tower", airport)
        return _reply(
            engine, f"{callsign}, {hello}unable runway {spoken}, wind not known" + (f", {who} will advise" if who else "")
        )
    if why:
        active = runways_at(engine, airport, issue if here else None, raw)
        in_use = f", runway {say.runway(active[0])} in use" if active else ""
        return _reply(engine, f"{callsign}, {hello}unable runway {spoken}, {why}{in_use}")
    arrival, airborne = flight.arrival, _airborne(raw)
    if arrival and arrival.airport == airport.ident and airborne:
        late = arrival.hold or arrival.stage in ("base", "intercept", "procedure", "tower")
        if late or flight.phase == Phase.LANDING_CLEARED:
            return _reply(engine, f"{callsign}, {hello}unable runway {spoken}, continue {arrival.words()}")
        flight.requested_runway = (airport.ident, wanted, "arrival")
        if station.kind == "approach" and here:
            new = ifr.replan(engine, station, issue, raw)
            if new:
                return _reply(engine, f"{callsign}, {hello}roger, expect {new.words()}", {"runway": new.runway})
        return _reply(engine, f"{callsign}, {hello}roger, expect runway {spoken}")
    if flight.phase in (Phase.LANDING_CLEARED, Phase.TAKEOFF_CLEARED) and flight.runway:
        cleared = "cleared to land" if airborne else "cleared for takeoff"
        return _reply(engine, f"{callsign}, {hello}unable runway {spoken}, you are {cleared} runway {say.runway(flight.runway)}")
    flight.requested_runway = (airport.ident, wanted, "arrival" if airborne or flight.has_been_airborne else "departure")
    if airborne:
        if station.kind == "tower" and here and (intent.flight_rules or flight.rules) != "IFR":
            from . import vfr

            flight.runway = wanted
            pressure = engine._pressure(issue, faa)[0]
            tail = f", {pressure}" if pressure else ""
            return vfr.inbound(engine, intent, station, issue, raw, callsign, hello, faa, wanted, tail)
        if station.kind == "tower" and here:
            flight.runway = wanted
        return _reply(engine, f"{callsign}, {hello}roger, expect runway {spoken}")
    flight.runway = wanted
    # Held at the other runway for traffic: that wait is over, the flight now taxis to the new one.
    flight.waiting_takeoff = False
    if flight.taxi_goal and flight.phase == Phase.TAXI_OUT and station.kind in ("ground", "tower"):
        return engine._taxi_out(station, issue, raw, callsign, hello, faa, "")  # a new route to the new runway
    return _reply(engine, f"{callsign}, {hello}roger, expect runway {spoken}")


def approach_preference(engine, intent, station, raw, callsign, hello):
    """'Request the R NAV approach', to a centre: kept for approach, when the destination publishes one."""
    family = intent.approach
    airport = destination(engine)
    if airport is None or family not in ifr.FAMILY_WORDS:
        return None
    words = ifr.FAMILY_WORDS[family]
    active = runways_at(engine, airport, None, raw)
    if not active:
        engine.flight.requested_approach = family
        return _reply(engine, f"{callsign}, {hello}roger, {words} approach requested, approach will advise")
    runway = engine.requested_runway(airport, weather=weather_at(engine, airport, raw)) or split_runways(active)[0][0]
    if not choose_approach(engine.procedures(airport.ident), runway, family):
        current = expected_approach(engine, airport, None, raw)
        return _reply(engine, f"{callsign}, {hello}unable, no {words} approach runway {say.runway(runway)}, expect {current}")
    engine.flight.requested_approach = family
    return _reply(engine, f"{callsign}, {hello}roger, expect {expected_approach(engine, airport, None, raw)}")
