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
    altitude_ft: int
    issued: bool = False
    passed: bool = False

    def miles(self, raw: dict) -> float:
        return distance_nm(raw["lat"], raw["lon"], self.lat, self.lon)


def build(engine, star: Procedure, destination: Airport) -> list[Constraint]:
    """The STAR's level restrictions, in the order they are flown, for waypoints that can be placed."""
    if not engine.navfix:
        return []
    found: dict[str, Constraint] = {}
    for legs in star.transitions.values():
        for leg in legs:
            if not leg.fix or not leg.altitude_ft or leg.fix in found or leg.alt_descr == "+":
                continue
            position = engine.navfix.position(leg.fix, destination.lat, destination.lon, 300)
            if position:
                found[leg.fix] = Constraint(leg.fix, position[0], position[1], leg.alt_descr, leg.altitude_ft)
    # Flown from far to near: the further from the airport, the earlier.
    return sorted(found.values(), key=lambda c: -distance_nm(c.lat, c.lon, destination.lat, destination.lon))


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
        if constraint.altitude_ft >= feet - 500 and not constraint.issued:
            continue
        return constraint
    return None


def due(constraint: Constraint, raw: dict, feet: float) -> bool:
    """Time to start down: a three degree path (300 ft a mile) plus a level run-in."""
    return constraint.miles(raw) <= (feet - constraint.altitude_ft) / 300 + RUN_IN_NM


def late(constraint: Constraint, feet: float) -> bool:
    return constraint.issued and constraint.descr in ("", "-", "B") and feet > constraint.altitude_ft + LATE_FT
