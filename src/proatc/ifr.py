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

Arrivals: the vectoring pattern depends on where the aircraft comes from (vectors.py), the
approach is the best one published unless the pilot asks for another type or reports the field
in sight, a runway without an approach is reached by circling from another one, and a change
of runway in use re-plans an arrival that is not yet on final. A missed approach goes back to
approach control for another try, or for a diversion to the alternate.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace

from . import atis as atis_text
from . import holding, restrictions, traffic, vfr
from . import phraseology as say
from .atis import uses_faa_style
from .intents import Intent
from .navdata import Airport, bearing, distance_nm
from .procedures import choose_approach, choose_sid, choose_star, circling_approach, route_fixes, spoken_name
from .vectors import CLOSE_NM, FINAL_NM, established, locate, next_vector, turn_direction

RADAR_KINDS = ("departure", "approach", "center")
CHECK_IN = ("check_in", "initial_call", "position_report", "inbound")
# What a centre answers with "contact approach" once the flight is close in and should be with approach.
TO_APPROACH = (*CHECK_IN, "request_descent", "request_approach", "request_visual", "request_vectors", "other", "unknown")
DEPARTURE_TOP_FT = 24000  # the departure controller's highest level
DEPARTURE_HANDOFF_NM = 30
APPROACH_HANDOFF_NM = 45
STEP_FT = 10000  # the size of one climb or descent clearance
STEP_LEAD_NM = 5  # the next step down comes this far before a three degree path leaves the cleared level
BOUNDARY_AHEAD_S = 90  # a centre hands the flight on this long before it leaves the sector
BOUNDARY_AHEAD_NM = 8  # and never closer to the boundary than this
TOWER_HANDOFF_NM = 25  # an airport with a tower and no approach controller: within the tower's range
CLIP_NM = 45  # back in this centre's airspace this far ahead: the next sector's corner is only being clipped
MIN_STAY_NM = 20  # after a hand-off between centres, no other for this distance
BACK_AGAIN_NM = 60  # a flight is not handed back to the centre it came from within this distance of leaving it
APPROACH_SOON_NM = 25  # this close to the hand-off to approach, a centre does not send the flight to another centre
NEXT_STEP_WITHIN_FT = 2000  # the next step is given this close to the cleared level
LOW_AGL_FT = 1000  # descending below this height above the ground, away from the approach, draws an alert
BUST_FT = 400  # how far through a cleared level before the controller says something
HEADING_GRACE_S = 45  # time to roll onto an assigned heading before being asked about it
STAGES = ["descend", "downwind", "base", "intercept", "procedure", "tower"]
ON_APPROACH = ("intercept", "procedure")  # cleared for the approach: by vectors, or by its own initial fix
PROCEDURAL_KINDS = "RHP"  # R NAV, R N P and G P S approaches are flown from an initial fix when one is published
PROCEDURE_WITHIN_NM = 45
JOINED_NM = 2.0  # within this distance of a track, the aircraft has joined it
OFF_TRACK_NM = 4.0
OFF_TRACK_AFTER_S = 45
EN_ROUTE_OFF_TRACK_NM = 10.0  # in the cruise, between waypoints that may be far apart
EN_ROUTE_OFF_TRACK_S = 120
CROSSING_NM = 2.5
SPEED_MARGIN_KT = 15
SPEEDS_KT = {"descend": 250, "downwind": 220, "base": 180, "intercept": 160}
JOIN_NM = (9, 10, 12)  # how far out final is joined; it differs from one arrival to the next
EXTEND_UP_TO_NM = 8  # a downwind leg is extended at most this far beyond the usual base turn
VISUAL_WITHIN_NM = 25
VISUAL_VISIBILITY_SM = 5
VISUAL_CEILING_FT = 3000
CIRCLING_VISIBILITY_SM = 3
CIRCLING_CEILING_FT = 1500
MAX_TAILWIND_KT = 7  # an approach to the other end is used straight in with no more tailwind than this
DIVERSION_TOP_FT = 24000
FAMILY_WORDS = {"ILS": "I L S", "LOC": "localiser", "RNAV": "R NAV", "VOR": "V O R", "NDB": "N D B"}


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
    final_nm: float = FINAL_NM  # where final is joined
    approach_runway: str | None = None  # the runway the approach is flown to, when circling to `runway`
    missed_ft: int | None = None  # the published missed approach altitude
    go_arounds: int = 0
    extended: bool = False  # told the downwind leg is being extended for traffic
    circling_given: bool = False
    procedure: object | None = None  # procedures.Approach: the approach being flown, with its initial fixes
    via: str | None = None  # the initial approach fix an approach flown as a procedure starts at
    unavailable: bool = False  # no instrument approach, and weather that rules out a visual one
    unavailable_told: bool = False

    @property
    def flown(self) -> str:
        """The runway the approach itself leads to."""
        return self.approach_runway or self.runway

    def words(self) -> str:
        """'I L S approach runway two seven right', with the circling runway when there is one."""
        text = f"{self.approach} approach runway {say.runway(self.flown)}"
        return f"{text}, circle to land runway {say.runway(self.runway)}" if self.approach_runway else text


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


def _reply(engine, text: str, expect: dict | None = None, event: str = ""):
    from .engine import Reply

    return Reply(text, expect or {}, event=event)


def safe_floor_ft(engine, raw: dict) -> int:
    """The lowest level that may be given where the aircraft is: the minimum off-route altitude of the terrain below."""
    if not engine.navfix or "lat" not in raw:
        return 0
    return int(engine.navfix.grid_safe_ft(raw["lat"], raw["lon"]) or 0)


def _contact_word(faa: bool) -> str:
    return "radar contact" if faa else "identified"


def _fix_word(engine, fix: str, lat: float, lon: float) -> str:
    """A waypoint as spoken: a five-letter name as a word, a navaid by its name."""
    from .airways import spoken_coordinate

    if spoken_coordinate(fix):
        return spoken_coordinate(fix)
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
    asked = engine.requested_runway(airport)
    if plan.departure_runway and issue and plan.departure_runway in engine.active_runways(airport, issue) and not asked:
        runway = plan.departure_runway
    procedures = engine.procedures(airport.ident)
    sid = choose_sid(procedures, runway, plan.route, plan.sid)
    destination = engine.db.airport(plan.destination)
    where = destination.spoken_name if destination else say.spoken_callsign(plan.destination)

    # The initial climb is to the first level the SID holds the flight down to on the legs it will fly;
    # failing that, the SID's highest such restriction; failing that, a standard initial altitude.
    flown = sid.chain("SID", runway, route_fixes(plan.route)) if sid else []
    held = next((leg.altitude_ft for leg in flown if leg.altitude_ft and leg.alt_descr in ("", "@", "-", "B")), None)
    initial = held or (sid.top_altitude() if sid else None) or (5000 if faa else 6000)
    if plan.cruise_ft:
        initial = min(initial, plan.cruise_ft)
    flight.rules, flight.destination, flight.runway = "IFR", where, runway
    flight.sid, flight.cleared_ft = (sid.name if sid else None), initial
    fixes = route_fixes(plan.route)
    flight.sid_constraints = restrictions.build_sid(engine, sid, airport, runway, fixes) if sid else []
    flight.sid_fixes = restrictions.named_path(engine, sid, "SID", airport, runway, fixes) if sid else []
    track = [(lat, lon) for _, lat, lon in flight.sid_fixes]
    # Only the SID's own waypoints. A departure usually begins on a heading (runway heading, then a turn to
    # intercept): a straight line from the airport to the first waypoint is not where it goes.
    flight.sid_path = track
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
    if plan and plan.destination and plan.destination != ident and not flight.diverting and flight.rules != "VFR":
        # An instrument flight on its way somewhere else: this airport's radar controller is not its
        # approach controller, and does not vector it to land here because it checked in.
        return "passing"
    return "arrival"


def handle(engine, intent, station, issue, raw, variation, callsign, hello):
    """A pilot transmission on a departure, centre or approach frequency."""
    if bool(raw.get("on_ground", True)):
        return None
    part = role(engine, station, intent)
    if intent.intent == "report_leaving":
        from . import answers

        reply = answers.leave_frequency(engine, station, raw, callsign, hello, _faa(station))
    elif intent.intent == "request_direct":
        reply = _direct(engine, intent, station, raw, callsign, part)
    elif intent.intent == "request_weather":
        reply = weather_request(engine, station, issue, raw, variation, callsign, hello)
    elif intent.intent in ("request_deviation", "clear_of_weather"):
        reply = _deviation(engine, intent, callsign, part)
    elif intent.intent in ("request_flight_following", "cancel_flight_following"):
        reply = _flight_following(engine, intent, station, issue, callsign, hello)
    elif intent.intent == "request_transit" and station.airport and engine.flight.rules != "IFR":
        reply = vfr.transit(engine, intent, station, issue, raw, callsign, hello, _faa(station))
    elif intent.intent == "request_diversion":
        reply = _divert(engine, intent, station, raw, variation, callsign, hello)
    elif intent.intent == "request_vectors" and part != "arrival":
        reply = _center(engine, intent, station, raw, callsign, hello) if part == "center" else None
        reply = reply or _vectors_to_track(engine, station, raw, variation, callsign, hello, part)
    elif part == "departure":
        reply = _departure(engine, intent, station, raw, callsign, hello)
    elif part == "center":
        reply = _center(engine, intent, station, raw, callsign, hello)
    elif part == "passing":
        word = _contact_word(_faa(station))
        lead = f"{hello}{word}, " if hello else ""
        if intent.intent in ("request_climb", "request_descent"):
            reply = _level_request(engine, intent, station, raw, callsign, lead, _faa(station))
        elif intent.intent in CHECK_IN:
            reply = _reply(engine, f"{callsign}, {hello}{word}" if hello else f"{callsign}, roger")
        else:
            reply = None
    else:
        reply = _arrival(engine, intent, station, issue, raw, variation, callsign, hello)
    return _with_squawk_check(engine, reply, raw, hello)


def _with_squawk_check(engine, reply, raw, hello):
    """On first contact, a radar controller who sees the wrong code says so."""
    flight = engine.flight
    if not reply or not reply.text or not hello or not flight.squawk or flight.squawk_asked or "squawk" not in raw:
        return reply
    if "squawk" in reply.expect:
        return reply  # the reply gives the code already (a radar service just started)
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


def _level_request(engine, intent, station, raw, callsign, lead: str, faa: bool, floor: int = 0, ceiling: int = 45000):
    """'Request higher', 'request flight level three eight zero': the level asked for (or two thousand feet either way)
    when it is above the terrain and within what this controller works, else 'unable' and the level to keep."""
    flight, feet = engine.flight, _feet(raw)
    ident = station.airport.ident if station.airport else None
    cleared = flight.cleared_ft if flight.cleared_ft is not None else int(round(feet / 1000) * 1000)
    step = 2000 if intent.intent == "request_climb" else -2000
    target = int(intent.altitude) if intent.altitude else cleared + step
    lowest = max(floor, int(math.ceil(safe_floor_ft(engine, raw) / 1000) * 1000), 2000)
    keep = f"maintain {_level(engine, cleared, ident, faa)}"
    if abs(target - cleared) < 100:
        return _reply(engine, f"{callsign}, {lead}{keep}")
    if target > ceiling:
        return _reply(engine, f"{callsign}, {lead}unable higher, {keep}")
    if target < lowest:
        if lowest >= cleared - 100:
            return _reply(engine, f"{callsign}, {lead}unable lower, {keep}")
        target = lowest
    flight.cleared_ft = target
    if target > cleared:
        verb = "climb and maintain" if faa else "climb"
    else:
        verb = "descend and maintain" if faa else "descend"
    return _reply(engine, f"{callsign}, {lead}{verb} {_level(engine, target, ident, faa)}", {"altitude": target})


def _climb(engine, target: int, ident: str | None, faa: bool, callsign: str, lead: str):
    engine.flight.cleared_ft = target
    verb = "climb and maintain" if faa else "climb"
    return _reply(engine, f"{callsign}, {lead}{verb} {_level(engine, target, ident, faa)}", {"altitude": target})


def _destination_pressure(engine, ident: str | None, target: int, faa: bool) -> str:
    """', Q N H one zero zero niner' with the first descent to an altitude, from the destination's own weather report."""
    flight, raw = engine.flight, engine.last_raw
    if not ident or flight.qnh_told or not engine.metars or raw.get("wx_source") != 1:
        return ""
    airport = engine.db.airport(ident)
    if airport is None or target > _transition_ft(engine, ident, faa):
        return ""
    reported = engine.metars.weather(ident, airport.elev_ft)
    if not reported:
        return ""
    flight.qnh_told = True
    return f", {_sentences(atis_text._pressure(reported, faa))}"


def _descend(engine, target: int, ident: str | None, faa: bool, callsign: str, lead: str, tail: str = ""):
    flight = engine.flight
    flight.cleared_ft, flight.descent_given = target, True
    tail = f"{_destination_pressure(engine, ident, target, faa)}{tail}"
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
    top = _climb_goal(engine, station)
    lead = f"{hello}{_contact_word(faa)}, " if hello else ""
    if intent.intent == "request_descent" or (intent.intent == "request_climb" and intent.altitude and intent.altitude <= top):
        return _level_request(engine, intent, station, raw, callsign, lead, faa, ceiling=top)
    if intent.intent not in (*CHECK_IN, "request_climb"):
        return None
    if flight.cleared_ft and flight.cleared_ft >= top:
        if intent.intent == "request_climb":
            return _reply(engine, f"{callsign}, {lead}unable higher, maintain {_level(engine, flight.cleared_ft, None, faa)}")
        # A check-in with nothing new to give is still answered: silence here went to the free-form fallback.
        return _reply(engine, f"{callsign}, {hello}{_contact_word(faa)}" if hello else f"{callsign}, roger")
    target = _next_level(flight.cleared_ft or 0, _feet(raw), top)
    return _climb(engine, target, station.airport.ident, faa, callsign, lead)


def _descent_target(engine, destination: Airport | None, raw: dict | None = None) -> int:
    """The level to be at when handed to approach: about 9000 ft above where the approach is
    joined, and never below the minimum off-route altitude of the terrain being crossed."""
    platform = 3000 + (destination.elev_ft if destination else 0)
    # To approach at 45 miles: 9000 ft above the platform. To a tower at 25: lower, or it cannot get down.
    target = platform + (9000 if not destination or engine._handoff(destination, "approach") else 4000)
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
        star = choose_star(engine.procedures(destination.ident), plan.route, plan.star) if plan else None
        flight.star_procedure = star
        fixes = route_fixes(plan.route) if plan else []
        runway = plan.arrival_runway if plan else None
        flight.constraints = restrictions.build(engine, star, destination, runway, fixes) if star else []
        flight.star_fixes = restrictions.named_path(engine, star, "STAR", destination, runway, fixes) if star else []
        flight.star_path = [(lat, lon) for _, lat, lon in flight.star_fixes]
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


def _oceanic(engine, intent, station, raw, callsign, hello):
    """Oceanic airspace has no radar: the controller takes position reports and clears levels, nothing else."""
    from .airways import next_ahead

    flight = engine.flight
    feet = _feet(raw)
    ahead = next_ahead(engine.route(), raw["lat"], raw["lon"]) if "lat" in raw else None
    report = f", report {_fix_word(engine, ahead, raw['lat'], raw['lon'])}" if ahead else ""
    if intent.intent in ("request_climb", "request_descent"):
        step = 2000 if intent.intent == "request_climb" else -2000
        target = int(intent.altitude) if intent.altitude else int(flight.cleared_ft or round(feet / 1000) * 1000) + step
        climbing = target > feet
        if not 20000 <= target <= 45000 or abs(target - feet) < 500:
            return _reply(engine, f"{callsign}, {hello}unable, maintain {say.level(flight.cleared_ft or feet, 0)}")
        flight.cleared_ft = target
        verb = "climb" if climbing else "descend"
        return _reply(engine, f"{callsign}, {hello}{verb} {say.level(target, 0)}, report reaching", {"altitude": target})
    if intent.intent in CHECK_IN:
        level = say.level(flight.cleared_ft or round(feet / 1000) * 1000, 0)
        return _reply(engine, f"{callsign}, {hello}roger, maintain {level}{report}")
    return None


def _center(engine, intent, station, raw, callsign, hello):
    flight, plan = engine.flight, engine.flight.plan
    if station.center.oceanic:
        return _oceanic(engine, intent, station, raw, callsign, hello)
    faa = _centre_faa(station)
    cruise = plan.cruise_ft if plan else None
    lead = f"{hello}{_contact_word(faa)}, " if hello else ""
    destination = engine.db.airport(plan.destination) if plan and plan.destination else None
    near = (
        destination
        and "lat" in raw
        and distance_nm(raw["lat"], raw["lon"], destination.lat, destination.lon) <= _handoff_nm(engine, destination)
    )
    if near and flight.arrival is None and (flight.told_approach or intent.intent in CHECK_IN) and intent.intent in TO_APPROACH:
        # Close in and still on a centre: whatever is asked, the answer is approach's to give.
        return _to_approach(engine, station, destination, raw, callsign, lead)
    if intent.intent == "request_approach":
        from . import answers

        return answers.approach_preference(engine, intent, station, raw, callsign, lead)
    if intent.intent == "request_descent":
        if intent.altitude and intent.altitude < _feet(raw) - 500:
            # A level the pilot asks for is checked against the terrain like any other.
            wanted, floor = int(intent.altitude), int(math.ceil(safe_floor_ft(engine, raw) / 1000) * 1000)
            if wanted >= floor:
                return _descend(engine, wanted, None, faa, callsign, lead)
            low = _level(engine, floor, None, faa)
            if floor >= _feet(raw) - 500:
                return _reply(engine, f"{callsign}, {lead}unable lower, the minimum safe altitude in your area is {low}")
            return _descend(engine, floor, None, faa, callsign, f"{lead}unable {_level(engine, wanted, None, faa)} due terrain, ")
        return _descent(engine, station, raw, callsign, lead, faa) or _level_request(
            engine, intent, station, raw, callsign, lead, faa
        )
    if intent.intent == "request_climb":
        target = int(intent.altitude) if intent.altitude else cruise
        if target and target <= 45000 and target > _feet(raw) and target > (flight.cleared_ft or 0):
            flight.descent_given = False
            return _climb(engine, target, None, faa, callsign, lead)
        return _level_request(engine, intent, station, raw, callsign, lead, faa)
    if intent.intent not in CHECK_IN:
        return None
    if not flight.descent_given and cruise and (flight.cleared_ft or 0) < cruise:
        return _climb(engine, _next_level(flight.cleared_ft or 0, _feet(raw), cruise), None, faa, callsign, lead)
    # A check-in with nothing new to give is still answered: silence here went to the free-form fallback.
    return _reply(engine, f"{callsign}, {hello}{_contact_word(faa)}" if hello else f"{callsign}, roger")


def _direct(engine, intent, station, raw, callsign, part: str):
    """'Request direct BIBAX': granted for a waypoint on the filed route, outside radar vectors."""
    flight, plan = engine.flight, engine.flight.plan
    fix = (intent.fix or "").upper()
    known = set(route_fixes(plan.route)) | {name for name, _, _ in engine.route()} if plan else set()
    if not plan or not fix or fix not in known:
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
    from .answers import weather_at

    reported = weather_at(engine, destination, raw) if destination else None
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


def _flight_following(engine, intent, station, issue, callsign, hello):
    """Radar service for a VFR flight: a code, identification, and traffic calls from then on."""

    flight = engine.flight
    faa = _faa(station)
    if intent.intent == "cancel_flight_following":
        if not flight.following:
            return None
        flight.following = False
        code = "1200" if faa else "7000"
        flight.squawk = code
        return _reply(engine, f"{callsign}, radar service terminated, squawk {say.digits(code)}, frequency change approved")
    if flight.rules == "IFR":
        return None
    ident = station.airport.ident if station.airport else station.center.facility
    return vfr.radar_service(engine, intent, station, issue, callsign, hello, faa, ident)


# ---- arrival: vectors and the approach clearance ----------------------------


def _setup_arrival(engine, station, issue, raw, intent) -> Arrival | None:
    flight, airport, plan = engine.flight, station.airport, engine.flight.plan
    active = engine.active_runways(airport, issue)
    if not active:
        return None
    if engine.requested_runway(airport):
        runway = engine.requested_runway(airport)
    elif plan and plan.arrival_runway in active and plan.destination == airport.ident:
        runway = plan.arrival_runway
    else:
        runway = engine._landing_runway(airport, issue, raw, intent)
    procedures = engine.procedures(airport.ident)
    wanted = flight.requested_approach
    approach = (wanted and choose_approach(procedures, runway, wanted)) or choose_approach(procedures, runway)
    flown = runway
    unavailable = False
    if approach is None:
        # No approach to the runway in use. In order: circle from another runway's approach if the weather
        # allows circling; land straight in from that approach if the tailwind is acceptable; a visual
        # approach if the weather allows one; otherwise there is no way in, and the pilot is told so.
        others = [ident for rw in airport.runways for ident, _ in rw.ends() if ident != runway]
        other = circling_approach(procedures, others)
        if other and _weather_allows(issue, CIRCLING_VISIBILITY_SM, CIRCLING_CEILING_FT):
            approach, flown = other, other.runway
        elif other and _tailwind_kt(airport, other.runway, issue) <= MAX_TAILWIND_KT:
            approach, flown, runway = other, other.runway, other.runway
        elif issue is not None and not _weather_allows(issue, VISUAL_VISIBILITY_SM, VISUAL_CEILING_FT):
            unavailable = True
    platform = approach.platform_ft if approach and approach.platform_ft else int(math.ceil((airport.elev_ft + 3000) / 500) * 500)
    course = next(heading for rw in airport.runways for ident, heading in rw.ends() if ident == flown)
    # Final is joined no closer than a three degree path from the joining altitude allows.
    join = max(engine._rng.choice(JOIN_NM), math.ceil((platform - airport.elev_ft) / 320))
    flight.runway, flight.diverting = runway, False
    flight.arrival = Arrival(
        airport.ident, runway, approach.spoken if approach else "visual", platform, course, engine._threshold(airport, flown),
        final_nm=min(join, 16), approach_runway=flown if flown != runway else None,
        missed_ft=approach.missed_ft if approach else None, go_arounds=flight.go_arounds, unavailable=unavailable,
        procedure=approach,
    )  # fmt: skip
    return flight.arrival


def _tailwind_kt(airport: Airport, runway: str, issue) -> float:
    if issue is None:
        return 0.0
    course = next(heading for rw in airport.runways for ident, heading in rw.ends() if ident == runway)
    return -issue.weather.wind_kt * math.cos(math.radians(issue.weather.wind_dir_true - course))


def _weather_allows(issue, visibility_sm: float, ceiling_ft: float) -> bool:
    """Whether the reported weather is at least this good: for a visual approach, or for circling."""
    if issue is None:
        return False
    weather = issue.weather
    ceiling = min((cloud.base_ft_agl for cloud in weather.clouds if cloud.coverage >= 0.5), default=None)
    return weather.visibility_sm >= visibility_sm and (ceiling is None or ceiling >= ceiling_ft) and not weather.fog


def _speed(arrival: Arrival, stage: str, raw: dict, faa: bool) -> str | None:
    """Speed control as the aircraft is brought onto final. Not said if already that slow."""
    target = SPEEDS_KT.get(stage)
    if not target or (arrival.speed_kt and arrival.speed_kt <= target):
        return None
    if stage == "descend" and not (raw.get("ias_kt", 0.0) > target + 10 and _feet(raw) < 14000):
        return None  # still high, or already at the usual speed below flight level one hundred
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
    if arrival.unavailable:
        # Nothing can be cleared: no vectors toward an approach that does not exist.
        if arrival.unavailable_told:
            return None
        arrival.unavailable_told = True
        runway = say.runway(arrival.runway)
        text = f"no instrument approach is available for runway {runway} and the weather is below visual minima, say intentions"
        return _reply(engine, f"{callsign}, {lead}{text}", event="no_approach")
    safe = (engine.navfix.airport_safe_ft(airport.ident) if engine.navfix else None) or 0
    if arrival.stage in (None, "descend") and position.distance_nm <= PROCEDURE_WITHIN_NM:
        procedural = _via_initial_fix(engine, station, issue, raw, callsign, lead, position, safe, faa)
        if procedural:
            return procedural
    # Another aircraft on final where this one would turn in: the downwind leg runs on.
    extend = (
        engine.traffic_enabled
        and position.along_nm < arrival.final_nm + EXTEND_UP_TO_NM
        and traffic.on_final(raw, airport, arrival.flown, arrival.final_nm + 4) is not None
    )
    inbound = arrival.stage in (None, "descend")
    vector = next_vector(position, arrival.course_true, arrival.platform_ft, _feet(raw), safe, arrival.final_nm, extend, inbound)
    if (
        extend
        and arrival.stage == "downwind"
        and not arrival.extended
        and position.along_nm >= arrival.final_nm - 2
        and position.cross_nm > CLOSE_NM
    ):
        arrival.extended = True
        return _reply(engine, f"{callsign}, extending your downwind for traffic on final, I will call your base turn")
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
    if vector.stage == "intercept":
        parts.append(f"{say.digits(round(position.distance_nm))} miles from touchdown")
        parts.append(f"cleared {arrival.words()}")
        if speed:
            parts.append(speed)
        parts.append("report established")
        if not arrival.approach_runway:  # with a circling runway two runways are said: neither is checked
            expect["runway"] = arrival.runway
        else:
            arrival.circling_given = True
    else:
        if speed:
            parts.append(speed)
        if first:
            parts.append(f"expect {arrival.words()}")
    if not parts:
        return None
    return _reply(engine, f"{callsign}, {lead}{', '.join(parts)}", expect)


def _initial_fix(engine, airport, arrival, raw, position):
    """(fix, crossing altitude, position) of the published initial approach fix that suits where the aircraft is."""
    approach = arrival.procedure
    if not approach or approach.kind not in PROCEDURAL_KINDS or not engine.navfix:
        return None
    best = None
    for fix, altitude in approach.initial_fixes():
        where = engine.navfix.position(fix, airport.lat, airport.lon, 80)
        if not where:
            continue
        out = distance_nm(*where, *arrival.threshold)
        if out < 5 or position.distance_nm < out - 3:
            continue  # too close in to be a way in, or already behind the aircraft
        miles = distance_nm(raw["lat"], raw["lon"], *where)
        if best is None or miles < best[0]:
            best = (miles, fix, altitude, where)
    return best[1:] if best else None


def _via_initial_fix(engine, station, issue, raw, callsign, lead, position, safe, faa):
    """An R NAV approach is not vectored: direct to its initial approach fix, and cleared for the procedure."""
    flight, arrival, airport = engine.flight, engine.flight.arrival, station.airport
    found = _initial_fix(engine, airport, arrival, raw, position)
    if not found:
        return None
    fix, crossing_ft, where = found
    word = _fix_word(engine, fix, *where)
    arrival.stage, arrival.via, arrival.heading, flight.direct_to = "procedure", fix, None, fix
    altitude = max(crossing_ft or arrival.platform_ft, int(math.ceil(safe / 100) * 100))
    parts = [f"{'proceed' if faa else 'route'} direct {word}"]
    expect: dict = {}
    cleared = flight.cleared_ft if flight.cleared_ft is not None else _feet(raw)
    if altitude < min(cleared, _feet(raw) + 200):
        flight.cleared_ft, flight.descent_given = altitude, True
        transition = _transition_ft(engine, airport.ident, faa)
        text = f"descend {'and maintain ' if faa else ''}{say.level(altitude, transition, faa)}"
        if altitude <= transition and not arrival.qnh_given and issue:
            arrival.qnh_given = True
            text += f", {engine._pressure(issue, faa)[0]}"
        parts.append(text)
        expect["altitude"] = altitude
    parts.append(f"cleared {arrival.words()} via {word}")
    parts.append("report established")
    if not arrival.approach_runway:
        expect["runway"] = arrival.runway
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
        if flight.arrival and flight.arrival.stage in ON_APPROACH:
            return _to_tower(engine, station, callsign)
        if flight.arrival and flight.arrival.airport == station.airport.ident:
            # Not cleared for the approach yet: the next vector if one is due, else an acknowledgement.
            return _vector(engine, station, issue, raw, variation, callsign, "", first=False) or _reply(
                engine, f"{callsign}, roger"
            )
        return None
    if intent.intent == "cancel_hold":
        if flight.arrival and flight.arrival.hold:
            return _leave_hold(engine, station, issue, raw, variation, callsign)
        return None
    # Going around: back from the tower. "Request lower" on first contact is a check-in: the descent comes with it.
    wanted = (*CHECK_IN, "going_around", "request_hold", "request_visual", "request_approach", "request_climb", "request_descent",
              "request_vectors")  # fmt: skip
    if intent.intent not in wanted:
        return None
    if not flight.arrival or flight.arrival.airport != station.airport.ident:
        if _setup_arrival(engine, station, issue, raw, intent) is None:
            return None
    arrival = flight.arrival
    lead = f"{hello}{_contact_word(faa)}, " if hello else ""
    if flight.plan and flight.plan.destination == station.airport.ident:
        _star(engine, station.airport)
    if intent.intent == "request_visual" or (intent.intent == "request_approach" and intent.approach == "visual"):
        return _visual(engine, station, issue, raw, callsign, lead)
    if intent.intent == "request_approach":
        return _other_approach(engine, intent, station, callsign, lead)
    if intent.intent == "request_climb" or (intent.intent == "request_descent" and not hello):
        return _arrival_level(engine, intent, station, raw, callsign, hello, faa)
    if intent.intent == "request_vectors" and not hello:
        return vectors_again(engine, station, issue, raw, variation, callsign, "")
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
    if asked and not arrival.hold:
        return _reply(engine, f"{callsign}, {lead}unable to hold" + (", continue present heading" if arrival.heading else ""))
    if arrival.hold:
        return _reply(engine, f"{callsign}, {lead.rstrip(', ')}" if lead else f"{callsign}, roger")
    # A call with nothing new to give is still answered: "roger" rather than the free-form fallback.
    return _vector(engine, station, issue, raw, variation, callsign, lead, first=True) or _reply(engine, f"{callsign}, roger")


def _arrival_level(engine, intent, station, raw, callsign, hello, faa: bool):
    """A level asked for while being vectored: lower down to where the approach is joined, never higher."""
    flight, arrival = engine.flight, engine.flight.arrival
    lead = f"{hello}{_contact_word(faa)}, " if hello else ""
    if arrival.stage in ON_APPROACH or arrival.stage == "tower":
        level = _level(engine, flight.cleared_ft or arrival.platform_ft, station.airport.ident, faa)
        return _reply(engine, f"{callsign}, {lead}unable, maintain {level} until established")
    ceiling = flight.cleared_ft if flight.cleared_ft is not None else int(_feet(raw))
    return _level_request(engine, intent, station, raw, callsign, lead, faa, floor=arrival.platform_ft, ceiling=ceiling)


def _visual(engine, station, issue, raw, callsign, lead: str):
    """'Field in sight': a visual approach when the weather and the distance allow it, and over to the tower."""
    arrival, airport = engine.flight.arrival, station.airport
    if arrival.stage == "tower":
        return None
    miles = distance_nm(raw["lat"], raw["lon"], *engine._threshold(airport, arrival.runway))
    if miles > VISUAL_WITHIN_NM or not _weather_allows(issue, VISUAL_VISIBILITY_SM, VISUAL_CEILING_FT):
        if arrival.approach == "visual":
            return _reply(engine, f"{callsign}, {lead}roger, continue present heading")
        return _reply(engine, f"{callsign}, {lead}unable visual approach, expect {arrival.words()}")
    arrival.approach, arrival.approach_runway, arrival.hold, arrival.heading = "visual", None, None, None
    arrival.procedure = None
    arrival.course_true = next(heading for rw in airport.runways for ident, heading in rw.ends() if ident == arrival.runway)
    arrival.threshold, arrival.stage = engine._threshold(airport, arrival.runway), "tower"
    text = f"{callsign}, {lead}cleared visual approach runway {say.runway(arrival.runway)}"
    expect = {"runway": arrival.runway}
    target = engine._handoff(airport, "tower")
    if target:
        text += f", contact {target[0]} {target[1]}"
        expect["frequency"] = target[2]
    return _reply(engine, text, expect)


def _other_approach(engine, intent, station, callsign, lead: str):
    """'Request the R NAV approach': given when one is published for the runway and final has not been joined."""
    arrival = engine.flight.arrival
    family = intent.approach
    if family not in FAMILY_WORDS:
        return None
    if arrival.stage in ("intercept", "tower"):
        return _reply(engine, f"{callsign}, {lead}unable, continue the {arrival.approach} approach")
    found = choose_approach(engine.procedures(station.airport.ident), arrival.flown, family)
    if not found:
        runway = say.runway(arrival.flown)
        return _reply(
            engine, f"{callsign}, {lead}unable, no {FAMILY_WORDS[family]} approach runway {runway}, expect {arrival.words()}"
        )
    arrival.approach, arrival.missed_ft, arrival.procedure = found.spoken, found.missed_ft, found
    # The next tick starts again for this approach: by its initial fix if it has one, otherwise by vectors.
    arrival.stage, arrival.heading = ("descend" if arrival.stage else None), None
    if found.platform_ft:
        arrival.platform_ft = found.platform_ft
    return _reply(engine, f"{callsign}, {lead}roger, expect {arrival.words()}")


def replan(engine, station, issue, raw):
    """The arrival planned again from where it is, for the runway now wanted. None when nothing changes."""
    flight = engine.flight
    old = flight.arrival
    flight.runway = None
    new = _setup_arrival(engine, station, issue, raw, Intent())
    if new is None or new.runway == old.runway:
        flight.arrival, flight.runway = old, old.runway
        return None
    new.qnh_given, new.delay_decided = old.qnh_given, True
    new.stage = "descend" if old.stage else None  # the next tick gives the vectors for the new runway
    return new


def _runway_change(engine, station, issue, raw, callsign):
    """The runway in use has changed under an arrival that is not yet on final: a new approach, new vectors."""
    flight = engine.flight
    old = flight.arrival
    flight.runway = None
    new = _setup_arrival(engine, station, issue, raw, Intent())
    if new is None or new.runway == old.runway:
        flight.arrival, flight.runway = old, old.runway
        return None
    new.qnh_given, new.delay_decided = old.qnh_given, True
    new.stage = "descend"  # already being worked by this controller: the next tick gives the new vectors
    return _reply(engine, f"{callsign}, runway change, expect {new.words()}", {"runway": new.runway})


def _divert(engine, intent, station, raw, variation, callsign, hello):
    """A diversion: cleared to the alternate (or the airport asked for) on a heading and a level, then handed on."""
    flight, plan = engine.flight, engine.flight.plan
    asked = (intent.destination or "").strip().upper()
    target = engine.db.airport(asked) if len(asked) == 4 and asked.isalnum() else None
    if target is None and plan and plan.alternate:
        target = engine.db.airport(plan.alternate)
    if target is None or "lat" not in raw:
        return _reply(engine, f"{callsign}, {hello}roger, say the four letter code of the airport you are diverting to")
    faa = _faa(station)
    feet = _feet(raw)
    miles = distance_nm(raw["lat"], raw["lon"], target.lat, target.lon)
    safe = (engine.navfix.grid_safe_ft(raw["lat"], raw["lon"]) if engine.navfix else None) or 0
    level = max(miles * 150 + target.elev_ft + 3000, safe + 1000)
    level = int(min(math.ceil(level / 1000) * 1000, DIVERSION_TOP_FT))
    left_from = station.airport.ident if station.airport else (plan.destination if plan else None)
    base = plan or _blank_plan()
    flight.plan = replace(
        base, origin=left_from, destination=target.ident, route=[], sid=None, star=None, departure_runway=None,
        arrival_runway=None, cruise_ft=level, alternate=None,
    )  # fmt: skip
    flight.rules, flight.destination = "IFR", target.spoken_name
    flight.arrival, flight.star, flight.star_procedure, flight.constraints = None, None, None, None
    flight.told_approach, flight.told_sector, flight.descent_given, flight.direct_to = False, None, False, None
    flight.sector_ahead = None
    flight.runway, flight.awaiting_landing, flight.diverting = None, False, True
    if station.airport:
        flight.origin_ident = station.airport.ident
    flight.cleared_ft = level
    heading = round(((bearing(raw["lat"], raw["lon"], target.lat, target.lon) + variation) % 360) / 5) * 5 % 360 or 360
    current = raw.get("heading_mag", heading)
    turn = "fly" if abs((heading - current + 180) % 360 - 180) < 8 else f"turn {turn_direction(current, heading)}"
    ident = station.airport.ident if station.airport else None
    spoken = _level(engine, level, ident, faa)
    if level > feet + 500:
        vertical = f"{'climb and maintain' if faa else 'climb'} {spoken}"
    elif level < feet - 500:
        vertical = f"{'descend and maintain' if faa else 'descend'} {spoken}"
    else:
        vertical = f"maintain {spoken}"
    text = f"{callsign}, {hello}roger, cleared to {target.spoken_name}, {turn} heading {say.heading(heading)}, {vertical}"
    return _reply(engine, text, {"heading": heading, "altitude": level})


def vectors_again(engine, station, issue, raw, variation, callsign, hello):
    """'Request vectors' while on the arrival: out of a hold or off the approach, and onto vectors for final again."""
    arrival = engine.flight.arrival
    if arrival is None or arrival.airport != station.airport.ident or arrival.stage == "tower":
        return None
    if arrival.hold:
        return _leave_hold(engine, station, issue, raw, variation, callsign)
    if arrival.stage in ON_APPROACH:
        # Not able to join it as cleared: the approach is flown from vectors instead, not from an initial fix.
        arrival.stage, arrival.via, engine.flight.direct_to = "descend", None, None
        arrival.procedure = None if arrival.procedure and arrival.procedure.kind in PROCEDURAL_KINDS else arrival.procedure
    given = _vector(engine, station, issue, raw, variation, callsign, f"{hello}roger, ", first=False)
    if given:
        return given
    if arrival.heading is not None:
        heading = arrival.heading
        return _reply(engine, f"{callsign}, {hello}roger, fly heading {say.heading(heading)}", {"heading": heading})
    return _reply(engine, f"{callsign}, {hello}roger, expect vectors for the {arrival.words()}")


def _vectors_to_track(engine, station, raw, variation, callsign, hello, part: str):
    """'Request vectors' off the arrival: a heading back to the departure or to the filed route."""
    if station.kind == "center" and station.center.oceanic:
        return None  # no radar over the ocean: nobody to give a heading
    flight = engine.flight
    track = "departure" if part == "departure" and flight.sid_fixes else "route"
    return rejoin(engine, track, raw, variation, callsign, hello, heading=True, faa=_faa(station))


def rejoin(engine, track: str, raw, variation, callsign, hello, heading: bool, faa: bool):
    """Back to a track the flight has left: direct to its next waypoint, or a heading toward it when vectors
    are asked for. None when the engine knows no waypoint of that track."""
    from . import questions
    from .airways import next_point

    if "lat" not in raw:
        return None
    ahead = next_point(questions.track_points(engine, track), raw["lat"], raw["lon"])
    if ahead is None:
        return None
    name, lat, lon = ahead
    flight = engine.flight
    if heading or not name:
        target = questions.heading_to(raw, lat, lon, variation)
        current = raw.get("heading_mag", target)
        turn = "fly" if abs((target - current + 180) % 360 - 180) < 8 else f"turn {turn_direction(current, target)}"
        what = "your filed route" if track == "route" else f"the {track}"
        if name:
            word = _fix_word(engine, name, lat, lon)
            text = f"roger, {turn} heading {say.heading(target)}, vectors to rejoin {what}, direct {word} when able"
        else:
            text = f"roger, {turn} heading {say.heading(target)}, vectors to rejoin {what}"
        return _reply(engine, f"{callsign}, {hello}{text}", {"heading": target})
    flight.direct_to = name
    word = _fix_word(engine, name, lat, lon)
    return _reply(engine, f"{callsign}, {hello}roger, {'proceed' if faa else 'route'} direct {word}")


def _blank_plan():
    from .flightplan import FlightPlan

    return FlightPlan()


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
    arrival.stage, arrival.heading, arrival.speed_kt, arrival.extended = None, None, None, False
    arrival.via, flight.direct_to = None, None
    arrival.go_arounds += 1
    flight.go_arounds = arrival.go_arounds
    # The published missed approach when the sim's data has one, otherwise straight ahead to the joining altitude.
    published = bool(arrival.missed_ft) and arrival.approach != "visual"
    feet = max(arrival.missed_ft, arrival.platform_ft) if published else arrival.platform_ft
    flight.cleared_ft, flight.awaiting_landing = feet, False
    level = _level(engine, feet, airport.ident, faa)
    verb = "climb and maintain" if faa else "climb"
    if published:
        how = "fly the published missed approach" if faa else "standard missed approach"
    else:
        how = "fly runway heading"
    text = f"{callsign}, roger, {how}, {verb} {level}, contact {target[0]} {target[1]}"
    return _reply(engine, text, {"altitude": feet, "frequency": target[2]})


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


def _ahead(raw: dict, miles: float | None = None) -> tuple[float, float]:
    """Where the aircraft will be in a minute and a half, or `miles` further on, on its present heading."""
    if miles is None:
        miles = max(BOUNDARY_AHEAD_NM, raw.get("groundspeed_ms", 0.0) * 1.944 * BOUNDARY_AHEAD_S / 3600)
    track = math.radians(raw.get("heading_true", 0.0))
    lat = raw["lat"] + miles / 60 * math.cos(track)
    lon = raw["lon"] + miles / 60 * math.sin(track) / max(math.cos(math.radians(raw["lat"])), 0.05)
    return lat, lon


def _handoff_nm(engine, destination) -> float:
    """How far out the centre hands the flight to the destination: to approach at 45 miles; to a tower with
    no approach controller only once it is in the tower's range."""
    return APPROACH_HANDOFF_NM if engine._handoff(destination, "approach") else TOWER_HANDOFF_NM


def _to_approach(engine, station, destination, raw, callsign, lead: str = ""):
    """'Contact approach', from a centre. Said once by each centre: if the pilot turns up on another centre
    without having reached approach, that one says it too."""
    flight = engine.flight
    target = engine._handoff(destination, "approach", raw) or engine._handoff(destination, "tower", raw)
    flight.told_approach, flight.approach_told_by, flight.sector_ahead = True, station.key, None
    if not target:
        # Nobody to hand the flight to: an airfield without a controller. The pilot is released to its frequency.
        return _reply(engine, f"{callsign}, {lead}radar service terminated, frequency change approved")
    return _reply(engine, f"{callsign}, {lead}contact {target[0]} {target[1]}", {"frequency": target[2]})


def _approach_missed(engine, station) -> bool:
    """Told to contact approach, not with approach yet, and now talking to a centre that did not say it."""
    flight = engine.flight
    return flight.told_approach and flight.arrival is None and flight.approach_told_by != station.key


def _centre_handoff(engine, raw, callsign, exclude=None, where=None):
    """'Contact the next centre': the one at `where`, or where the aircraft is."""
    if not engine.centers:
        return None
    lat, lon = where or (raw["lat"], raw["lon"])
    center = engine.centers.at(lat, lon, _feet(raw))
    if center is None or center is exclude or engine.flight.told_sector == center.name:
        return None
    left = engine.flight.left_sector
    if left:
        since = distance_nm(raw["lat"], raw["lon"], left[1], left[2])
        # Along a ragged boundary, or where sectors are split by level: not back to the centre it has just
        # come from, and no second hand-off on the heels of the first.
        if since < MIN_STAY_NM or (left[0] == center.name and since < BACK_AGAIN_NM):
            return None
    if exclude is not None:
        engine.flight.left_sector = (exclude.name, raw["lat"], raw["lon"])
    engine.flight.told_sector = center.name
    engine.flight.sector_ahead = center.name if where else None
    station = engine.centers.station_for(center)
    engine.centers.expect(station.khz, center)
    text = f"{callsign}, contact {station.callsign} {say.frequency(station.khz)}"
    return _reply(engine, text, {"frequency": f"{station.khz / 1000:.3f}"})


def _crossing_missed(engine, crossing, raw, ident: str, faa: bool, callsign: str):
    """What the controller says when a SID crossing is flown outside its restriction: too low, too high or too fast."""
    feet = _feet(raw)
    word = _fix_word(engine, crossing.fix, crossing.lat, crossing.lon)
    lower = crossing.lower_ft
    if crossing.altitude_ft and lower and feet < lower - 400:
        level = _level(engine, lower, ident, faa)
        return _reply(
            engine, f"{callsign}, you were to cross {word} at or above {level}, expedite climb", event="restriction_missed"
        )
    if crossing.altitude_ft and crossing.upper and feet > crossing.altitude_ft + 400:
        level = _level(engine, crossing.altitude_ft, ident, faa)
        return _reply(engine, f"{callsign}, you were to cross {word} at or below {level}", event="restriction_missed")
    if crossing.speed_kt and raw.get("ias_kt", 0.0) > crossing.speed_kt + SPEED_MARGIN_KT:
        text = f"{callsign}, speed restriction {say.digits(crossing.speed_kt)} knots at {word}, reduce speed"
        return _reply(engine, text, event="restriction_missed")
    return None


def _route_watch(
    engine, raw, callsign, points, name: str | None, kind: str, limit: float = OFF_TRACK_NM, after: float = OFF_TRACK_AFTER_S
):
    """Tell a flight that has left the track of its SID, its STAR or its route, once, after it has been off it for a while."""
    flight = engine.flight
    if not points or not name or name in flight.off_route_called or flight.direct_to or flight.deviating or flight.emergency:
        return None
    gap = restrictions.off_track_nm(points, raw["lat"], raw["lon"])
    if name not in flight.on_track:
        # A track is watched from the moment the aircraft is on it. Before that it is on a heading after
        # take-off, or on its way to the first waypoint, and is not "off" a track it has not reached.
        if gap is not None and gap <= JOINED_NM:
            flight.on_track.add(name)
        return None
    if gap is None or gap <= limit:
        flight.off_route_since = None
        return None
    if flight.off_route_since is None:
        flight.off_route_since = engine.now()
        return None
    if engine.now() - flight.off_route_since < after:
        return None
    flight.off_route_called.add(name)
    flight.off_route_since = None
    miles = say.digits(round(gap))
    if kind == "route":
        text = f"{callsign}, you appear to be {miles} miles off your filed route, confirm your routing"
    else:
        text = f"{callsign}, you appear to be {miles} miles off the {kind} track, confirm you are following the {kind}"
    return _reply(engine, text, event="off_route")


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
            return _reply(engine, text, event="level_bust")  # a prompt to correct, not an instruction to read back
    height = raw["agl_m"] / 0.3048 if "agl_m" in raw else None
    if height is not None:
        on_approach = arrival is not None and arrival.stage in ("intercept", "tower")
        if height > 2.5 * LOW_AGL_FT:
            flight.low_altitude_called = False
        elif height < LOW_AGL_FT and rate < -500 and not on_approach and not flight.low_altitude_called:
            flight.low_altitude_called = True
            safe = engine.navfix.grid_safe_ft(raw["lat"], raw["lon"]) if engine.navfix else None
            tail = f", minimum safe altitude {say.altitude(safe)} feet" if safe else ""
            return _reply(engine, f"{callsign}, low altitude alert, check your altitude immediately{tail}", event="low_altitude")
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
        return _reply(
            engine, f"{callsign}, fly heading {say.heading(arrival.heading)}", {"heading": arrival.heading}, "heading_not_flown"
        )
    # A Basic Service gives no traffic information; every other service, and every IFR flight, gets it.
    if engine.traffic_enabled and not (flight.following and flight.service == "basic"):
        call = traffic.advisory(raw, engine._traffic_seen, engine.now())
        if call:
            return _reply(engine, f"{callsign}, {call}")
    return vfr.airspace_watch(engine, station, raw, callsign, faa)


def tick(engine, station, issue, raw, variation, callsign):
    """Hand-offs, the next level, the descent clearance and the next vector, as the flight progresses."""
    flight, plan = engine.flight, engine.flight.plan
    if bool(raw.get("on_ground", True)) or "lat" not in raw:
        return None
    part = role(engine, station)
    if station.key not in flight.contacted:
        return None  # the controller speaks first only to an aircraft that has checked in
    if station.kind == "center" and station.center.oceanic:
        # No radar: nothing is watched. The next sector is given at the boundary; the destination, when
        # the flight comes within its range while still on the oceanic frequency.
        landing = engine.db.airport(plan.destination) if plan and plan.destination else None
        if landing and not flight.arrival and (not flight.told_approach or _approach_missed(engine, station)):
            if distance_nm(raw["lat"], raw["lon"], landing.lat, landing.lon) <= _handoff_nm(engine, landing):
                return _to_approach(engine, station, landing, raw, callsign)
        if not station.center.contains(raw["lat"], raw["lon"]):
            return _centre_handoff(engine, raw, callsign, exclude=station.center)
        return None
    noticed = _watch(engine, station, raw, callsign)
    if noticed:
        return noticed
    faa = _faa(station)

    if part == "departure":
        airport = station.airport
        for crossing in flight.sid_constraints or []:
            if not crossing.passed and crossing.miles(raw) <= CROSSING_NM:
                crossing.passed = True
                missed = _crossing_missed(engine, crossing, raw, airport.ident, faa, callsign)
                if missed:
                    return missed
        strayed = _route_watch(engine, raw, callsign, flight.sid_path, flight.sid, "departure")
        if strayed:
            return strayed
        if flight.diverting and plan and plan.destination and not flight.told_approach:
            # A short diversion never reaches a centre: straight to the other airport's approach.
            other = engine.db.airport(plan.destination)
            if other and distance_nm(raw["lat"], raw["lon"], other.lat, other.lon) <= APPROACH_HANDOFF_NM + 10:
                target = engine._handoff(other, "approach", raw) or engine._handoff(other, "tower")
                if target and distance_nm(raw["lat"], raw["lon"], airport.lat, airport.lon) > 8:
                    flight.told_approach = True
                    return _reply(engine, f"{callsign}, contact {target[0]} {target[1]}", {"frequency": target[2]})
        top = _climb_goal(engine, station)
        away = distance_nm(raw["lat"], raw["lon"], airport.lat, airport.lon)
        cleared = flight.cleared_ft or 0
        if cleared < top and _near_cleared(engine, raw) and away <= DEPARTURE_HANDOFF_NM:
            return _climb(engine, _next_level(cleared, _feet(raw), top), airport.ident, faa, callsign, "")
        if away > DEPARTURE_HANDOFF_NM or (cleared >= top and _feet(raw) >= cleared - 1500):
            return _centre_handoff(engine, raw, callsign)
        return None

    if part == "passing":
        # A radar controller of an airport on the way. Close to the destination, it sends the flight there.
        destination = engine.db.airport(plan.destination) if plan and plan.destination else None
        if destination and not flight.arrival and (not flight.told_approach or flight.approach_told_by != station.key):
            if distance_nm(raw["lat"], raw["lon"], destination.lat, destination.lon) <= _handoff_nm(engine, destination):
                return _to_approach(engine, station, destination, raw, callsign)
        return None

    if part == "center":
        destination = engine.db.airport(plan.destination) if plan and plan.destination else None
        remaining = distance_nm(raw["lat"], raw["lon"], destination.lat, destination.lon) if destination else None
        gate = _handoff_nm(engine, destination) if destination else APPROACH_HANDOFF_NM
        if destination and remaining <= gate and (not flight.told_approach or _approach_missed(engine, station)):
            return _to_approach(engine, station, destination, raw, callsign)
        if flight.told_approach:
            return None  # handed to approach: the flight is no longer this controller's to descend
        # The next centre is given a little before the boundary: once across it, this controller's frequency
        # is out of range. An aircraft that has left the sector is the next controller's to climb or descend.
        if flight.sector_ahead and flight.sector_ahead != station.center.name:
            return None  # sent on to the next centre: this controller has nothing more to say
        inside = station.center.contains(raw["lat"], raw["lon"])
        if inside and flight.sector_ahead == station.center.name:
            flight.sector_ahead = None  # arrived in the sector it was sent to
        # Sent here before the boundary and not across it yet: this controller keeps the flight.
        inside = inside or flight.sector_ahead == station.center.name
        ahead = _ahead(raw)
        # About to be handed to approach: not sent to another centre for the last few miles. Two hand-offs
        # seconds apart leave the pilot on the wrong one.
        soon = bool(destination) and remaining <= gate + APPROACH_SOON_NM
        leaving = not soon and (not inside or not station.center.contains(*ahead))
        if leaving and station.center.contains(*_ahead(raw, CLIP_NM)):
            leaving = False  # only crossing a corner of the next centre's airspace: nobody is called for that
        if leaving:
            handed = _centre_handoff(engine, raw, callsign, exclude=station.center, where=ahead if inside else None)
            if handed or not inside:
                return handed
        cruise = plan.cruise_ft if plan else None
        cleared = flight.cleared_ft or 0
        if not flight.descent_given and cruise and cleared < cruise and _near_cleared(engine, raw):
            return _climb(engine, _next_level(cleared, _feet(raw), cruise), None, faa, callsign, "")
        if plan and not flight.descent_given:
            # In the cruise the whole filed route is the track, airways included.
            track = [(lat, lon) for _, lat, lon in engine.route()]
            strayed = _route_watch(engine, raw, callsign, track, "route", "route", EN_ROUTE_OFF_TRACK_NM, EN_ROUTE_OFF_TRACK_S)
            if strayed:
                return strayed
        if destination and plan:
            _star(engine, destination)
            crossed = restrictions.update_passed(flight.constraints, raw, destination)
            if crossed and restrictions.late(crossed, _feet(raw)):
                level = _level(engine, crossed.altitude_ft, destination.ident, faa)
                word = _fix_word(engine, crossed.fix, crossed.lat, crossed.lon)
                return _reply(
                    engine, f"{callsign}, you were to be level {level} by {word}, expedite descent", event="restriction_missed"
                )
            if crossed and crossed.issued and crossed.speed_kt and raw.get("ias_kt", 0.0) > crossed.speed_kt + SPEED_MARGIN_KT:
                word = _fix_word(engine, crossed.fix, crossed.lat, crossed.lon)
                text = f"{callsign}, speed restriction {say.digits(crossed.speed_kt)} knots at {word}, reduce speed"
                return _reply(engine, text, event="restriction_missed")
            if flight.star and not (flight.arrival and flight.arrival.stage):
                strayed = _route_watch(engine, raw, callsign, flight.star_path, flight.star, "arrival")
                if strayed:
                    return strayed
            ahead = restrictions.next_ahead(flight.constraints, _feet(raw))
            if ahead and not ahead.issued and ahead.altitude_ft >= 60000:
                # A speed limit with no level: said as the waypoint comes up.
                if ahead.miles(raw) <= 25:
                    ahead.issued = True
                    word = _fix_word(engine, ahead.fix, ahead.lat, ahead.lon)
                    speed = say.digits(ahead.speed_kt)
                    star = _announce_star(engine, destination)
                    return _reply(engine, f"{callsign}, cross {word} at {speed} knots or less{star}")
            elif ahead and not ahead.issued and restrictions.due(ahead, raw, _feet(raw)):
                ahead.issued = True
                word = _fix_word(engine, ahead.fix, ahead.lat, ahead.lon)
                speed = f", speed {say.digits(ahead.speed_kt)} knots or less at {word}" if ahead.speed_kt else ""
                tail = f", level by {word}{speed}{_announce_star(engine, destination)}"
                return _descend(engine, ahead.altitude_ft, destination.ident, faa, callsign, "", tail)
            if any(not c.passed for c in flight.constraints):
                return None  # the arrival's restrictions set the pace
        if destination:
            goal = _descent_target(engine, destination, raw)
            if not flight.descent_given:
                # Three degrees is about 300 ft per mile; ten miles in hand for slowing down.
                start_nm = (_feet(raw) - goal) / 300 + gate + 10
                if remaining <= start_nm:
                    return _descent(engine, station, raw, callsign, "", faa)
            elif cleared > goal and _near_cleared(engine, raw):
                # The next step waits for the distance too: an aircraft that came down quickly is held at its
                # level until a three degree path needs the next one, not sent lower a hundred miles out.
                if remaining <= (cleared - goal) / 300 + gate + 10 + STEP_LEAD_NM:
                    return _descend(engine, _next_level(cleared, _feet(raw), goal), destination.ident, faa, callsign, "")
        return None

    arrival = flight.arrival
    if arrival and arrival.airport == station.airport.ident and arrival.hold:
        if holding.progress(engine, raw) == "release":
            return _leave_hold(engine, station, issue, raw, variation, callsign)
        return None
    if not arrival or arrival.airport != station.airport.ident or arrival.stage in (None, "tower"):
        return None
    if arrival.stage in ON_APPROACH:
        position = locate(raw["lat"], raw["lon"], arrival.threshold, arrival.course_true)
        if established(position, arrival.final_nm) or 0 < position.along_nm < arrival.final_nm - 4:
            return _to_tower(engine, station, callsign)
        return None
    asked = engine.requested_runway(station.airport)
    if issue is not None and arrival.runway not in engine.active_runways(station.airport, issue) and arrival.runway != asked:
        changed = _runway_change(engine, station, issue, raw, callsign)
        if changed:
            return changed
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
    if station.kind == "center" and station.center.oceanic:
        from .airways import next_ahead

        ahead = next_ahead(engine.route(), raw["lat"], raw["lon"]) if "lat" in raw else None
        estimate = f", estimating {_fix_word(engine, ahead, raw['lat'], raw['lon'])}" if ahead else ""
        level = say.level(cleared or round(feet / 1000) * 1000, 0)
        add("Position report", f"position, {level}{estimate}", "position_report")
        add("Request climb", "request climb", "request_climb", altitude=int((cleared or feet) + 2000))
        return
    if part == "arrival" and not plan and flight.rules != "IFR" and not flight.arrival:
        part = "passing"  # a VFR flight with no plan is not this controller's arrival: it gets the en-route calls
    if part == "arrival":
        add("Check in", f"descending {_level(engine, cleared, ident, faa)}" if cleared else f"passing {passing}", "check_in")
        if flight.arrival and flight.arrival.hold:
            add("Ready for approach", "ready for the approach", "cancel_hold")
        elif not flight.arrival or flight.arrival.stage in (None, "descend"):
            add("Request hold", "request holding", "request_hold")
        arrival = flight.arrival
        if arrival and arrival.stage in ON_APPROACH:
            add("Established", f"established {arrival.approach} runway {say.runway(arrival.flown)}", "established")
        if arrival and arrival.stage in ("descend", "downwind", "base", "intercept") and arrival.approach != "visual":
            add("Field in sight", "field in sight, request visual approach", "request_visual")
        if arrival and arrival.stage in (None, "descend", "downwind") and not arrival.hold and station.airport:
            other = "RNAV" if arrival.approach in ("I L S", "G L S", "localiser") else "ILS"
            if choose_approach(engine.procedures(station.airport.ident), arrival.flown, other):
                words = FAMILY_WORDS[other]
                add(f"Request {other} approach", f"request the {words} approach", "request_approach", approach=other)
        if (flight.go_arounds or (arrival and arrival.unavailable)) and plan and plan.alternate:
            add(f"Divert to {plan.alternate}", f"request diversion to {plan.alternate}", "request_diversion",
                destination=plan.alternate)  # fmt: skip
        return
    if cleared and abs(cleared - feet) > 300:
        verb = "climbing" if cleared > feet else "descending"
        add("Check in", f"passing {passing} {verb} {_level(engine, cleared, ident, faa)}", "check_in")
    else:
        add("Check in", f"level {passing}" if cleared else f"passing {passing}", "check_in")
    if cleared:
        if faa:
            add("Verify altitude", "verify assigned altitude", "request_information", item="altitude")
        else:
            add("Confirm level", "confirm cleared level", "request_information", item="altitude")
    if part == "center" and flight.descent_given and plan and plan.destination:
        add("Expected approach", "request the expected approach and runway", "request_information", item="approach")
    if part == "center":
        if not flight.descent_given:
            add("Request descent", "request descent", "request_descent")
        add("Request climb", "request higher", "request_climb")
    if flight.following:
        add("Cancel radar service", "cancel radar service" if not faa else "cancel flight following", "cancel_flight_following")
    elif not plan and flight.rules != "IFR":
        if faa:
            add("Request flight following", "request flight following", "request_flight_following")
        elif (station.center.facility if station.kind == "center" else ident or "").startswith("EG"):
            add("Request basic service", "request basic service", "request_flight_following", service="basic")
            add("Request traffic service", "request traffic service", "request_flight_following", service="traffic")
        else:
            add("Request flight information", "request flight information service", "request_flight_following")
        if station.airport:
            add("Request zone transit", "request zone transit", "request_transit")
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
