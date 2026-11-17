"""Level restrictions along the STAR.

A STAR's legs carry altitudes at some of its waypoints (cross INPIP at FL260, INREV at
FL200, TARTN at FL70). The centre uses them to descend the flight: each restriction is
issued as "descend flight level two six zero, level by Inpip" when a three degree path to
it begins, and the controller says so if the waypoint is crossed well above the level.
Waypoint positions come from navfix.py; a STAR whose waypoints cannot be placed falls back
to the plain step descent in ifr.py.
"""

from __future__ import annotations

from dataclasses import dataclass

from .navdata import Airport, distance_nm
from .procedures import Procedure

PASSING_NM = 5  # this close to a waypoint counts as crossing it
LATE_FT = 700  # how far above the restriction at the waypoint before the controller says so
RUN_IN_NM = 10  # level this far before the waypoint


@dataclass
class Constraint:
    fix: str
    lat: float
    lon: float
    descr: str  # "" at, "-" at or below, "+" at or above, "B" between
    altitude_ft: int  # for "B", the upper limit
    issued: bool = False
    passed: bool = False
    floor_ft: int | None = None  # for "B", the lower limit
    speed_kt: int | None = None  # a speed limit at the fix

    @property
    def upper(self) -> bool:
        """Whether the aircraft must be at or below the altitude here (at, at or below, between)."""
        return self.descr in ("", "@", "-", "B")

    @property
    def lower_ft(self) -> int | None:
        """The altitude the aircraft must be at or above here, if any."""
        if self.descr == "+":
            return self.altitude_ft
        if self.descr == "B":
            return self.floor_ft
        return self.altitude_ft if self.descr in ("", "@") else None

    def miles(self, raw: dict) -> float:
        return distance_nm(raw["lat"], raw["lon"], self.lat, self.lon)


def _legs(procedure: Procedure, kind: str, runway: str | None, route: list[str] | None):
    """The legs as flown for this runway and route; every leg of the procedure when that cannot be worked out."""
    flown = procedure.chain(kind, runway, route or [])
    if any(leg.fix for leg in flown):
        return flown, True
    return [leg for legs in procedure.transitions.values() for leg in legs], False


def path(engine, procedure: Procedure, kind: str, airport: Airport, runway: str | None, route: list[str] | None):
    """The procedure's track over the ground: the positions of its fixes in the order flown."""
    return [(lat, lon) for _, lat, lon in named_path(engine, procedure, kind, airport, runway, route)]


def named_path(engine, procedure: Procedure, kind: str, airport: Airport, runway: str | None, route: list[str] | None):
    """The same track as (fix, lat, lon): the waypoints a flight that has left it can be sent back to."""
    if not engine.navfix:
        return []
    flown, ordered = _legs(procedure, kind, runway, route)
    if not ordered:
        return []
    points = []
    for leg in flown:
        position = engine.navfix.position(leg.fix, airport.lat, airport.lon, 300) if leg.fix else None
        if position and (not points or position != points[-1][1:]):
            points.append((leg.fix, *position))
    return points


def build(engine, star: Procedure, destination: Airport, runway: str | None = None, route: list[str] | None = None):
    """The STAR's level and speed restrictions in the order they are flown, for waypoints that can be placed.

    Only the legs for the filed route and the landing runway count, so a restriction that belongs
    to another runway's part of the arrival is not issued."""
    if not engine.navfix:
        return []
    flown, ordered = _legs(star, "STAR", runway, route)
    found: dict[str, Constraint] = {}
    for leg in flown:
        wanted = (leg.altitude_ft and leg.alt_descr != "+") or leg.speed_kt
        if not leg.fix or not wanted or leg.fix in found:
            continue
        position = engine.navfix.position(leg.fix, destination.lat, destination.lon, 300)
        if position:
            # A speed limit with no level, or with only an 'at or above', asks for no descent.
            level = leg.altitude_ft if leg.altitude_ft and leg.alt_descr != "+" else 60000
            found[leg.fix] = Constraint(
                leg.fix, position[0], position[1], leg.alt_descr if level < 60000 else "+", level,
                floor_ft=leg.altitude2_ft, speed_kt=leg.speed_kt,
            )  # fmt: skip
    if ordered:
        return list(found.values())
    # Flown from far to near: the further from the airport, the earlier.
    return sorted(found.values(), key=lambda c: -distance_nm(c.lat, c.lon, destination.lat, destination.lon))


def build_sid(engine, sid: Procedure, airport: Airport, runway: str | None = None, route: list[str] | None = None):
    """The SID's crossing restrictions that can be placed, in the order flown: at or above, at or below, at, between."""
    if not engine.navfix:
        return []
    flown, ordered = _legs(sid, "SID", runway, route)
    found: dict[str, Constraint] = {}
    for leg in flown:
        if not leg.fix or not (leg.altitude_ft or leg.speed_kt) or leg.fix in found:
            continue
        if not ordered and leg.alt_descr != "+":
            continue  # without knowing which legs are flown, only the 'at or above' crossings are safe to judge
        position = engine.navfix.position(leg.fix, airport.lat, airport.lon, 80)
        if position:
            found[leg.fix] = Constraint(
                leg.fix, position[0], position[1], leg.alt_descr if leg.altitude_ft else "+", leg.altitude_ft or 0,
                issued=True, floor_ft=leg.altitude2_ft, speed_kt=leg.speed_kt,
            )  # fmt: skip
    if ordered:
        return list(found.values())
    return sorted(found.values(), key=lambda c: distance_nm(c.lat, c.lon, airport.lat, airport.lon))


def off_track_nm(points: list[tuple[float, float]], lat: float, lon: float) -> float | None:
    """How far the aircraft is from the nearest part of a track, or None beyond its ends."""
    if len(points) < 2:
        return None
    import math

    scale = math.cos(math.radians(lat)) * 60
    px, py = lon * scale, lat * 60
    best = None
    for (lat1, lon1), (lat2, lon2) in zip(points, points[1:]):
        ax, ay, bx, by = lon1 * scale, lat1 * 60, lon2 * scale, lat2 * 60
        length = (bx - ax) ** 2 + (by - ay) ** 2
        along = ((px - ax) * (bx - ax) + (py - ay) * (by - ay)) / length if length else 0.0
        if 0.0 <= along <= 1.0:
            gap = math.hypot(px - (ax + along * (bx - ax)), py - (ay + along * (by - ay)))
            best = gap if best is None else min(best, gap)
    return best


def update_passed(constraints: list[Constraint], raw: dict, destination: Airport) -> Constraint | None:
    """Mark waypoints the aircraft has crossed or gone beyond. Returns one crossed just now, if any."""
    to_go = distance_nm(raw["lat"], raw["lon"], destination.lat, destination.lon)
    crossed = None
    for constraint in constraints:
        if constraint.passed:
            continue
        beyond = to_go < distance_nm(constraint.lat, constraint.lon, destination.lat, destination.lon) - PASSING_NM
        if constraint.miles(raw) <= PASSING_NM:
            constraint.passed, crossed = True, constraint
        elif beyond:
            constraint.passed = True
    return crossed


def next_ahead(constraints: list[Constraint], feet: float) -> Constraint | None:
    """The next restriction that asks for a descent; ones at or above the aircraft need nothing."""
    for constraint in constraints:
        if constraint.passed:
            continue
        if constraint.altitude_ft >= feet - 500 and not constraint.issued and not constraint.speed_kt:
            continue
        return constraint
    return None


def due(constraint: Constraint, raw: dict, feet: float) -> bool:
    """Time to start down: a three degree path (300 ft a mile) plus a level run-in."""
    return constraint.miles(raw) <= (feet - constraint.altitude_ft) / 300 + RUN_IN_NM


def late(constraint: Constraint, feet: float) -> bool:
    return constraint.issued and constraint.upper and feet > constraint.altitude_ft + LATE_FT
