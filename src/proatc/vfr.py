"""Visual flying: what a VFR flight gets from each kind of station.

- An airfield with no controller (CTAF, UNICOM, "Traffic"): you announce yourself and nobody
  answers. A UNICOM operator gives an airport advisory when asked.
- An information service (AFIS, "Information", "Radio"): information, never a clearance. You
  taxi, take off and land at your own discretion.
- A tower: how to join the circuit from where you are, circuits, a departure in a direction,
  transits of its control zone, special VFR when the weather is below VFR minima, and a
  frequency change once you have left the zone.
- A radar controller: flight following in North America, a Basic or Traffic Service in the
  United Kingdom, a flight information service elsewhere; and a warning before you fly into
  controlled or restricted airspace without a clearance.

Zones come from the sim's airspace file (airspace.py), circuit directions from the airport's
own data (apt.dat row 1101). These are functions over the Engine, like ifr.py.
"""

from __future__ import annotations

import math
import re

from . import phraseology as say
from . import traffic
from .airspace import CONTROLLED, SPECIAL, Airspace, fallback_zone
from .atis import uses_faa_style
from .navdata import Airport, bearing, distance_nm
from .vectors import locate

COMPASS = ["north", "north east", "east", "south east", "south", "south west", "west", "north west"]
CLASS_WORD = {"B": "Bravo", "C": "Charlie", "D": "Delta"}
INFORMATION = re.compile(r"\bAFIS\b|\bInfo(rmation)?\b|\bRadio\b|\bFISO?\b", re.I)
STRAIGHT_IN_BEYOND_NM = 2.0
AHEAD_NM = 6
SPECIAL_MAX_AREA = 1.0  # square degrees: larger restricted and danger areas are not called
SPECIAL_VFR_MIN_SM = 1.0  # about 1500 metres


def service(station) -> str | None:
    """'ctaf' (nobody there), 'unicom' (an operator who gives advisories), 'afis' (information only), or None."""
    kind, label = station.kind, getattr(station, "label", "") or ""
    if kind == "tower":
        return "afis" if INFORMATION.search(label) else None
    if kind != "unicom":
        return None
    if INFORMATION.search(label):
        return "afis"
    return "unicom" if "UNICOM" in label.upper() else "ctaf"


def compass(degrees: float) -> str:
    return COMPASS[round((degrees % 360) / 45) % 8]


def vmc(issue, faa: bool) -> bool:
    """Whether the reported weather allows ordinary VFR in a control zone."""
    if issue is None:
        return True
    weather = issue.weather
    ceiling = min((cloud.base_ft_agl for cloud in weather.clouds if cloud.coverage >= 0.5), default=None)
    return weather.visibility_sm >= 3 and (ceiling is None or ceiling >= (1000 if faa else 1500)) and not weather.fog


def pattern_side(engine, airport: Airport, runway: str) -> str:
    """'left' or 'right': the circuit direction the airport publishes for this runway (left when it says nothing)."""
    sides = engine._patterns.get(airport.ident)
    if sides is None:
        sides = {}
        try:
            for line in engine.db.record_lines(airport):
                if line.startswith("1101 "):
                    parts = line.split()
                    if len(parts) >= 3 and parts[2] in ("left", "right"):
                        sides.setdefault(parts[1], parts[2])
        except Exception:
            pass  # no record to read: every circuit is left-hand
        engine._patterns[airport.ident] = sides
    return sides.get(runway, "left")


def zone(engine, airport: Airport) -> Airspace | None:
    """The control zone around an airport: from the airspace data, else a circle if it has a tower."""
    if airport.ident not in engine._zones:
        found = engine.airspace.zone(airport.lat, airport.lon, airport.elev_ft) if engine.airspace else None
        towered = any(freq.kind == "tower" and not INFORMATION.search(freq.label or "") for freq in airport.freqs)
        if found is None and towered:
            found = fallback_zone(airport.spoken_name, airport.lat, airport.lon, airport.elev_ft)
        engine._zones[airport.ident] = found
    return engine._zones[airport.ident]


def _zone_words(space: Airspace | None, faa: bool) -> str:
    if faa and space is not None and space.kind in CLASS_WORD:
        return f"class {CLASS_WORD[space.kind]} airspace"
    return "control zone"


def _traffic_words(engine, airport: Airport, runway: str | None, raw: dict) -> str:
    """What an information officer can say about other aircraft: 'no reported traffic', or what is there."""
    if not engine.traffic_enabled or not runway:
        return "no reported traffic"
    if traffic.on_runway(raw, airport, runway):
        return "runway occupied"
    final = traffic.on_final(raw, airport, runway)
    return f"traffic on a {traffic.miles_word(final)} mile final" if final is not None else "no reported traffic"


def _position_words(airport: Airport, raw: dict) -> str:
    """'eight miles south' of the airport."""
    miles = distance_nm(raw["lat"], raw["lon"], airport.lat, airport.lon)
    direction = compass(bearing(airport.lat, airport.lon, raw["lat"], raw["lon"]))
    return f"{say.digits(max(1, round(miles)))} miles {direction}"


def _not_above(space: Airspace | None, airport: Airport, feet: float) -> int:
    wanted = math.ceil(max(feet, airport.elev_ft + 1500) / 500) * 500
    return int(min(wanted, space.ceiling_ft) if space is not None else wanted)


# ---- airfields without a controller -------------------------------------------


def ctaf(engine, intent, station, issue, raw, variation, callsign, kind: str):
    """A common traffic frequency: the call is a broadcast. A UNICOM operator answers a request for an advisory."""
    from .engine import Reply

    airport, flight = station.airport, engine.flight
    place = station.place
    flight.contacted.add(station.key)
    active = engine.active_runways(airport, issue)
    if active and flight.runway not in active:
        flight.runway = active[0]
    if not flight.rules:
        flight.rules = "VFR"
    if kind == "unicom" and intent.intent in ("request_weather", "radio_check", "initial_call"):
        if intent.intent == "radio_check":
            return Reply(f"{callsign}, {place} unicom, loud and clear")
        faa = uses_faa_style(airport.ident)
        parts = [engine._wind(issue, variation, faa), engine._pressure(issue, faa)[0]]
        if active:
            parts.append(f"runway {say.runway(active[0])} in use")
        told = ", ".join(part for part in parts if part)
        return Reply(f"{callsign}, {place} unicom, {told or 'no information available'}")
    return Reply("", note=f"No controller at {place}: your call went out to other traffic on the frequency.")


def ctaf_suggestions(engine, station, issue, raw, callsign: str, kind: str) -> list[dict]:
    """Self-announce calls for the app's buttons, built from where the aircraft is."""
    airport, flight = station.airport, engine.flight
    place = station.place
    active = engine.active_runways(airport, issue)
    runway = flight.runway if flight.runway in active else (active[0] if active else None)
    spoken = f" runway {say.runway(runway)}" if runway else ""
    side = pattern_side(engine, airport, runway) if runway else "left"
    on_ground = bool(raw.get("on_ground", True))
    items: list[dict] = []

    def add(label: str, body: str) -> None:
        items.append(
            {"label": label, "text": f"{place} traffic, {callsign}, {body}, {place}", "intent": {"intent": "traffic_call"}}
        )

    if on_ground and not flight.has_been_airborne:
        add("Taxiing", f"taxiing to{spoken}" if runway else "taxiing for departure")
        add("Departing", f"departing{spoken}")
        add("Departing, staying in the pattern", f"departing{spoken}, remaining in the pattern")
    elif on_ground:
        add("Clear of runway", f"clear of{spoken}" if runway else "clear of the runway")
    elif "lat" in raw:
        where = _position_words(airport, raw)
        add("Inbound", f"{where}, inbound for landing{spoken}")
        add("Overhead", f"overhead the field, joining {side} downwind{spoken}")
        add("Downwind", f"{side} downwind{spoken}")
        add("Base", f"{side} base{spoken}")
        add("Final", f"final{spoken}")
        add("Going around", f"going around{spoken}")
        add("Leaving the area", f"{where}, departing the area to the {where.rsplit('miles ', 1)[-1]}")
    if kind == "unicom":
        opening = f"{place} unicom, {callsign}, "
        items.append(
            {"label": "Airport advisory", "text": f"{opening}request airport advisory", "intent": {"intent": "request_weather"}}
        )
        items.append({"label": "Radio check", "text": f"{opening}radio check", "intent": {"intent": "radio_check"}})
    return items


# ---- information services -------------------------------------------------------


def afis(engine, intent, station, issue, raw, variation, callsign, hello: str):
    """An aerodrome flight information service: the runway, the wind, the pressure and the traffic. No clearances."""
    from .engine import Phase, Reply

    airport, flight = station.airport, engine.flight
    on_ground = bool(raw.get("on_ground", True))
    wind = engine._wind(issue, variation, False)
    pressure = engine._pressure(issue, False)[0]
    name = intent.intent
    if name == "radio_check":
        return Reply(f"{callsign}, {hello}reading you five")
    if name == "initial_call":
        return Reply(f"{callsign}, {hello}pass your message")
    if not flight.rules:
        flight.rules = "VFR"
    told = [part for part in (wind, pressure) if part]
    if on_ground:
        arrived = flight.has_been_airborne
        if name == "runway_vacated" or (name == "request_taxi" and arrived):
            flight.phase = Phase.TAXI_IN
            return Reply(f"{callsign}, {hello}roger, taxi to parking at your discretion")
        runway = flight.runway or engine._departure_runway(airport, issue, raw)
        if not runway:
            return None
        flight.runway = runway
        seen = _traffic_words(engine, airport, runway, raw)
        if name in ("request_taxi", "request_clearance", "request_pushback", "request_circuits"):
            flight.phase, flight.vfr_from = Phase.TAXI_OUT, airport.ident
            flight.circuits = name == "request_circuits"
            return Reply(f"{callsign}, {hello}{', '.join([f'runway {say.runway(runway)} in use', *told, seen])}")
        if name == "ready_for_departure":
            flight.phase = Phase.TAKEOFF_CLEARED
            return Reply(f"{callsign}, {hello}{', '.join([seen, *([wind] if wind else []), 'take off at your discretion'])}")
        if name == "request_weather":
            return Reply(f"{callsign}, {hello}{', '.join(told)}") if told else None
        return None
    runway = engine._landing_runway(airport, issue, raw, intent)
    if not runway:
        return None
    flight.runway = runway
    spoken = say.runway(runway)
    seen = _traffic_words(engine, airport, runway, raw)
    if name == "going_around":
        flight.phase = Phase.AIRBORNE
        return Reply(f"{callsign}, roger, report downwind")
    if name in ("request_transit", "report_leaving"):
        tail = "report leaving the area" if name == "request_transit" else "roger"
        return Reply(
            f"{callsign}, {hello}{', '.join([*([pressure] if pressure and name == 'request_transit' else []), seen, tail])}"
        )
    if name == "request_weather":
        return Reply(f"{callsign}, {hello}{', '.join(told)}") if told else None
    final = name in ("established", "request_touch_and_go", "request_low_approach") or intent.position == "final"
    if name in ("position_report", "established", "request_touch_and_go", "request_low_approach") and final:
        flight.phase = Phase.LANDING_CLEARED
        free = "runway free" if seen == "no reported traffic" else seen
        return Reply(f"{callsign}, {hello}{', '.join([free, *([wind] if wind else []), 'land at your discretion'])}")
    if name == "position_report":
        return Reply(f"{callsign}, {hello}roger, {seen}, report final")
    if name in ("inbound", "check_in"):
        side = pattern_side(engine, airport, runway)
        parts = [f"runway {spoken}", f"{side} hand circuit", *([pressure] if pressure else []), seen, "report downwind"]
        return Reply(f"{callsign}, {hello}{', '.join(parts)}")
    return None


def afis_suggestions(engine, station, issue, raw, add) -> None:
    flight = engine.flight
    on_ground = bool(raw.get("on_ground", True))
    runway = f" runway {say.runway(flight.runway)}" if flight.runway else ""
    if on_ground and not flight.has_been_airborne:
        add("Request taxi", "request taxi for a V F R departure", "request_taxi")
        add("Taxi for circuits", "request taxi for circuits", "request_circuits")
        add("Ready for departure", f"ready for departure{runway}", "ready_for_departure")
    elif on_ground:
        add("Runway vacated", "runway vacated", "runway_vacated")
    else:
        where = _position_words(station.airport, raw) if "lat" in raw else ""
        add("Inbound", f"{where}, inbound for landing".lstrip(", "), "inbound")
        add("Downwind", f"downwind{runway}", "position_report", position="downwind")
        add("Final", f"final{runway}", "position_report", position="final")
        add("Going around", "going around", "going_around")
        add("Passing by", f"{where}, transiting your area".lstrip(", "), "request_transit")


# ---- the tower ------------------------------------------------------------------


def below_minima(engine, intent, station, issue, raw, callsign, hello: str, faa: bool, entering: bool):
    """VFR asked for in weather below VFR minima: special VFR if the pilot asks for it, otherwise 'say intentions'.
    Returns None when the weather is fine and the ordinary reply applies."""
    from .engine import Reply

    if vmc(issue, faa):
        return None
    flight, airport = engine.flight, station.airport
    space = zone(engine, airport)
    if intent.intent != "request_special_vfr":
        if faa:
            return Reply(f"{callsign}, {hello}{station.place} is I F R, say intentions")
        return Reply(f"{callsign}, {hello}the weather is below V F R minima, special V F R is available, say intentions")
    if issue.weather.visibility_sm < SPECIAL_VFR_MIN_SM:
        return Reply(f"{callsign}, {hello}unable special V F R, visibility is below minima")
    flight.rules, flight.special_vfr = "VFR", True
    pressure = engine._pressure(issue, faa)[0]
    limit = _not_above(space, airport, airport.elev_ft)
    if faa:
        way = "into" if entering else "out of"
        text = f"cleared {way} the {_zone_words(space, True)}, maintain special V F R at or below {say.altitude(limit)}"
    else:
        way = "enter" if entering else "leave"
        text = f"special V F R clearance, {way} the control zone special V F R, not above altitude {say.altitude(limit)} feet"
    return Reply(f"{callsign}, {hello}{text}, {pressure}", {"altitude": limit})


def departure(engine, intent, station, issue, raw, callsign, hello: str, faa: bool, runway_part: str, tail: str, on_correct: str):
    """The VFR departure clearance: circuits, a direction to leave the zone in, or simply 'approved'."""
    from .engine import Reply

    flight, airport = engine.flight, station.airport
    special = below_minima(engine, intent, station, issue, raw, callsign, hello, faa, entering=False)
    flight.rules, flight.squawk, flight.vfr_from = "VFR", "1200" if faa else "7000", airport.ident
    flight.circuits, flight.told_leaving = intent.intent == "request_circuits", False
    if special is not None:
        return special
    squawk = f"squawk {say.digits(flight.squawk)}"
    runway = flight.runway
    if flight.circuits:
        side = pattern_side(engine, airport, runway) if runway else "left"
        if faa:
            text = f"closed traffic approved, {side} traffic{runway_part.replace(', departure runway', ' runway')}"
        else:
            text = f"V F R circuits approved{runway_part}, {side} hand"
    elif intent.compass:
        space = zone(engine, airport)
        if faa or space is None:
            text = f"V F R departure to the {intent.compass} approved{runway_part}"
        else:
            limit = _not_above(space, airport, airport.elev_ft)
            text = (
                f"V F R departure approved{runway_part}, leave the control zone to the {intent.compass}, "
                f"not above altitude {say.altitude(limit)} feet"
            )
    else:
        text = f"V F R departure approved{runway_part}"
    return Reply(f"{callsign}, {hello}{text}, {squawk}{tail}", {"squawk": flight.squawk}, on_correct)


def join(engine, airport: Airport, runway: str, raw: dict, faa: bool) -> tuple[str, str]:
    """(how to join the circuit, what to report next) from where the aircraft is."""
    side = pattern_side(engine, airport, runway)
    if "lat" not in raw:
        return (f"enter {side} downwind" if faa else f"join {side} hand downwind"), "downwind"
    course = next(heading for rw in airport.runways for ident, heading in rw.ends() if ident == runway)
    position = locate(raw["lat"], raw["lon"], engine._threshold(airport, runway), course)
    if position.along_nm > STRAIGHT_IN_BEYOND_NM and position.cross_nm < position.along_nm * 0.6:
        return ("make straight in" if faa else "make straight in approach"), "three mile final" if faa else "final"
    if position.side == side and position.along_nm > 0.5:
        return (f"enter {side} base" if faa else f"join {side} base"), "final"
    if position.side == side:
        return (f"enter {side} downwind" if faa else f"join {side} hand downwind"), "downwind"
    # On the far side of the runway from the circuit: across the field first.
    return (f"cross midfield, enter {side} downwind" if faa else f"join overhead for {side} hand downwind"), "downwind"


def inbound(engine, intent, station, issue, raw, callsign, hello: str, faa: bool, runway: str, pressure_tail: str):
    """A VFR arrival's first call: clearance into the zone and how to join."""
    from .engine import Reply

    airport, flight = station.airport, engine.flight
    special = below_minima(engine, intent, station, issue, raw, callsign, hello, faa, entering=True)
    if special is not None:
        return special
    entry, report = join(engine, airport, runway, raw, faa)
    space = zone(engine, airport)
    outside = space is not None and "lat" in raw and not space.covers(raw["lat"], raw["lon"])
    enter = "cleared to enter the control zone, " if outside and not faa else ""
    if outside and faa and space.kind == "B":
        enter = "cleared into the class Bravo airspace, "  # the one American airspace a VFR flight needs a clearance for
    flight.runway, flight.rules = runway, flight.rules or "VFR"
    flight.transit = None
    text = f"{callsign}, {hello}{enter}{entry} runway {say.runway(runway)}{pressure_tail}, report {report}"
    return Reply(text, {"runway": runway})


def transit(engine, intent, station, issue, raw, callsign, hello: str, faa: bool):
    """Crossing a control zone (or class B, C or D airspace) without landing."""
    from .engine import Reply, _squawk

    flight, airport = engine.flight, station.airport
    space = zone(engine, airport)
    feet = raw.get("elevation_m", 0.0) / 0.3048
    tracked = station.kind == "tower"
    if not tracked and engine.airspace and "lat" in raw and "heading_true" in raw:
        # A radar controller clears you through whatever controlled airspace lies ahead, not only its own airport's.
        inside = [s for s in engine.airspace.at(raw["lat"], raw["lon"], feet) if s.kind in CONTROLLED]
        ahead = engine.airspace.ahead(raw["lat"], raw["lon"], raw["heading_true"], feet, 15, CONTROLLED)[0]
        space = ahead or (inside[0] if inside else space)
    if space is None or "lat" not in raw:
        return None
    if not vmc(issue, faa) and intent.intent != "request_special_vfr":
        return below_minima(engine, intent, station, issue, raw, callsign, hello, faa, entering=True)
    pressure = engine._pressure(issue, faa)[0]
    tail = f", {pressure}" if pressure else ""
    flight.rules, flight.transit, flight.transit_inside = flight.rules or "VFR", airport.ident if tracked else None, False
    flight.airspace_called.add(space.name)  # cleared through it: no warning about it
    limit = _not_above(space, airport, feet)
    expect: dict = {"altitude": limit}
    if faa:
        if space.kind == "B":
            if not flight.squawk or flight.squawk == "1200":
                flight.squawk = _squawk(engine._rng)
            expect["squawk"] = flight.squawk
            text = (
                f"squawk {say.digits(flight.squawk)}, cleared through the {station.place} class Bravo airspace, "
                f"maintain V F R at or below {say.altitude(limit)}"
            )
        else:
            text = f"transition approved at or below {say.altitude(limit)}"
        text += f"{tail}, report clear of the {_zone_words(space, True)}"
    else:
        text = f"cleared to transit the control zone, V F R, not above altitude {say.altitude(limit)} feet{tail}, report leaving"
    return Reply(f"{callsign}, {hello}{text}", expect)


def leaving(engine, station, raw, callsign, faa: bool, asked: bool = False):
    """'Frequency change approved' for a VFR flight that has left the zone, or says it is leaving."""
    from .engine import Reply

    flight, airport = engine.flight, station.airport
    flight.told_leaving, flight.transit, flight.transit_inside = True, None, False
    if asked and flight.vfr_from != airport.ident and flight.rules != "VFR":
        return None
    if faa:
        return Reply(f"{callsign}, frequency change approved")
    code = ", squawk seven thousand" if flight.squawk not in (None, "7000") else ""
    if code:
        flight.squawk = "7000"
    return Reply(f"{callsign}, {'roger, ' if asked else 'leaving the control zone, '}frequency change approved{code}")


def tick_tower(engine, station, raw, callsign, faa: bool):
    """Unprompted, from the tower: the frequency change once a VFR departure or a transit is clear of the zone."""
    flight, airport = engine.flight, station.airport
    if "lat" not in raw or bool(raw.get("on_ground", True)) or flight.told_leaving:
        return None
    space = zone(engine, airport)
    if space is None:
        return None
    feet = raw.get("elevation_m", 0.0) / 0.3048
    inside = space.covers(raw["lat"], raw["lon"]) and feet <= space.ceiling_ft
    if flight.transit == airport.ident:
        if inside:
            flight.transit_inside = True
        elif flight.transit_inside:
            return leaving(engine, station, raw, callsign, faa)
        return None
    departing = flight.rules == "VFR" and flight.vfr_from == airport.ident and not flight.circuits and flight.has_been_airborne
    if departing and not inside and flight.arrival is None:
        return leaving(engine, station, raw, callsign, faa)
    return None


# ---- radar controllers ----------------------------------------------------------


def radar_service(engine, intent, station, issue, callsign, hello: str, faa: bool, ident: str | None):
    """Flight following, a Basic or Traffic Service, or a flight information service: whatever the country calls it."""
    from .engine import Reply, _squawk

    flight = engine.flight
    united_kingdom = (ident or "").startswith("EG")
    wanted = intent.service
    flight.rules, flight.following = "VFR", True
    if not flight.squawk or flight.squawk in ("1200", "7000"):
        flight.squawk = _squawk(engine._rng)
    pressure = f", {engine._pressure(issue, faa)[0]}" if issue else ""
    code = f"squawk {say.digits(flight.squawk)}"
    if faa:
        flight.service = "flight following"
        text = f"{code}, radar contact, flight following{pressure}"
    elif united_kingdom:
        flight.service = wanted if wanted in ("basic", "traffic", "deconfliction") else "basic"
        seen = "" if flight.service == "basic" else "identified, "
        text = f"{code}, {seen}{flight.service} service{pressure}"
    else:
        flight.service = "information"
        text = f"{code}, identified, flight information service{pressure}"
    return Reply(f"{callsign}, {hello}{text}", {"squawk": flight.squawk})


def airspace_watch(engine, station, raw, callsign, faa: bool):
    """A VFR flight under a radar service is told before it flies into airspace it has no clearance for."""
    from .engine import Reply

    flight, index = engine.flight, engine.airspace
    if not index or not flight.following or "lat" not in raw or "heading_true" not in raw:
        return None
    feet = raw.get("elevation_m", 0.0) / 0.3048
    lat, lon, track = raw["lat"], raw["lon"], raw["heading_true"]
    space, miles = index.ahead(lat, lon, track, feet, AHEAD_NM, CONTROLLED, flight.airspace_called)
    if space is not None:
        flight.airspace_called.add(space.name)
        middle = ((space.box[0] + space.box[1]) / 2, (space.box[2] + space.box[3]) / 2)
        # The tower to call is the one whose own zone this is; class B is entered through the radar controller.
        towers = [
            a
            for _, a in engine.db.nearest(*middle, 8, 6)
            if engine.db.station_of(a, "tower") and index.zone(a.lat, a.lon, a.elev_ft) is space
        ]
        target = engine._handoff(towers[0], "tower") if towers and space.surface and space.kind != "B" else None
        what = _zone_words(space, faa)
        distance = f"{say.digits(miles)} miles ahead"
        if target:
            text = f"{callsign}, {what} {distance}, for entry contact {target[0]} {target[1]}, otherwise remain outside"
            return Reply(text, {"frequency": target[2]})
        if faa and space.kind == "B":
            return Reply(f"{callsign}, {what} {distance}, remain clear of class Bravo unless cleared to enter")
        return Reply(
            f"{callsign}, {what} {distance}, remain outside {'class ' + CLASS_WORD[space.kind] if faa and space.kind in CLASS_WORD else 'controlled airspace'}"
        )
    space, miles = index.ahead(lat, lon, track, feet, AHEAD_NM, tuple(SPECIAL), flight.airspace_called)
    if space is not None and space.area <= SPECIAL_MAX_AREA:
        flight.airspace_called.add(space.name)
        return Reply(f"{callsign}, caution, {SPECIAL[space.kind]} {say.digits(miles)} miles ahead on your present track")
    return None


def tower_suggestions(engine, station, issue, raw, add) -> None:
    """Extra calls for a VFR flight on a tower frequency."""
    flight = engine.flight
    airport = station.airport
    faa = uses_faa_style(airport.ident)
    on_ground = bool(raw.get("on_ground", True))
    if on_ground and not flight.has_been_airborne and not flight.squawk:
        add("Request circuits", "request V F R circuits", "request_circuits", flight_rules="VFR")
        if not vmc(issue, faa):
            add("Request special VFR", "request special V F R departure", "request_special_vfr", flight_rules="VFR")
    elif not on_ground and flight.rules != "IFR":
        if "lat" in raw:
            where = _position_words(airport, raw)
            add("Request zone transit", f"{where}, request zone transit", "request_transit")
        if flight.vfr_from == airport.ident or flight.transit == airport.ident:
            add("Leaving the zone", "leaving the zone", "report_leaving")
        add("Low approach", "request low approach", "request_low_approach")
        if not vmc(issue, faa):
            add("Request special VFR", "request special V F R to enter the zone", "request_special_vfr", flight_rules="VFR")
