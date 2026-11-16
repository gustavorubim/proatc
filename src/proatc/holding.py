"""Holding before the approach.

There is no other traffic to wait for, so a hold happens for two reasons: the pilot asks
for one, or the "holding" setting has approach delay some arrivals ("sometimes" is about one
in four) the way a busy airport would. The hold is the published one at the end of the STAR
(earth_hold.dat); an airport without one never holds you.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import phraseology as say
from .navdata import distance_nm

ENTRY_NM = 6  # the hold's clock starts when the aircraft gets this close to the fix
DELAYS_MIN = (5, 8, 10)
CHANCE = 0.25  # of a hold on arrival, with holding = "sometimes"


@dataclass
class HoldState:
    fix: str
    lat: float
    lon: float
    level_ft: int
    minutes: int | None  # None: until the pilot is ready
    entered_at: float | None = None


def candidate(engine, airport, raw):
    """(fix, position, published hold) for the hold at the end of the arrival, if the aircraft has not passed it."""
    flight, navfix = engine.flight, engine.navfix
    if not navfix or "lat" not in raw:
        return None
    star = flight.star_procedure
    names = [c.fix for c in reversed(flight.constraints or [])] + (list(reversed(star.fixes())) if star else [])
    here = distance_nm(raw["lat"], raw["lon"], airport.lat, airport.lon)
    for name in dict.fromkeys(names):
        published = navfix.hold(name, airport.ident, flight.cleared_ft)
        position = navfix.position(name, airport.lat, airport.lon, 100)
        if published and position and here > distance_nm(*position, airport.lat, airport.lon) + 3:
            return name, position, published
    return None


def wanted(engine, arrival) -> bool:
    """Whether approach delays this arrival. Decided once per arrival."""
    if arrival.delay_decided:
        return False
    arrival.delay_decided = True
    if engine.holding == "always":
        return True
    return engine.holding == "sometimes" and engine._rng.random() < CHANCE


def issue(engine, station, raw, callsign, lead: str, found, minutes: int | None, level_text):
    """The holding instruction. `level_text(feet)` renders a level for this airport."""
    from .engine import Reply

    flight, arrival = engine.flight, engine.flight.arrival
    name, position, published = found
    feet = raw.get("elevation_m", 0.0) / 0.3048
    wanted_ft = round((flight.cleared_ft if flight.cleared_ft is not None else feet) / 1000) * 1000
    level = int(min(max(wanted_ft, published.min_ft), published.max_ft or 60000))
    level = int(-(-level // 1000) * 1000) if level % 1000 else level
    verb = "descend" if level < feet - 500 else "climb" if level > feet + 500 else "maintain"
    arrival.hold = HoldState(name, position[0], position[1], level, minutes)
    arrival.stage = None
    flight.cleared_ft = level
    if level < feet - 500:
        flight.descent_given = True
    word = name.title() if len(name) > 3 else " ".join(say.PHONETIC[c] for c in name)
    after = f"expect approach in {say.digits(minutes)} minutes" if minutes else "advise when ready for the approach"
    return Reply(f"{callsign}, {lead}hold at {word} as published, {verb} {level_text(level)}, {after}", {"altitude": level})


def progress(engine, raw) -> str:
    """'holding' while the hold continues, 'release' when its time is up."""
    hold = engine.flight.arrival.hold
    if hold.entered_at is None and distance_nm(raw["lat"], raw["lon"], hold.lat, hold.lon) <= ENTRY_NM:
        hold.entered_at = engine.now()
    if hold.minutes and hold.entered_at is not None and engine.now() - hold.entered_at >= hold.minutes * 60:
        return "release"
    return "holding"
