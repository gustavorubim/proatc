"""Radar vectors to final approach: where the aircraft is relative to the runway, and what to say next.

Without other traffic there is nobody to sequence behind, so the pattern is the simplest one
that works from any direction: a downwind leg if the aircraft is not yet far enough out, a
base leg toward the extended centreline, then a 30 degree intercept with the approach clearance.
Terrain is not considered.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .navdata import bearing, distance_nm

FINAL_NM = 10  # where the final approach course is joined, from the threshold
BASE_AT_NM = 12  # how far out a downwind leg runs before the base turn
CLOSE_NM = 3.5  # lateral distance at which base becomes the intercept heading
VECTOR_RANGE_NM = 30  # further out than this, only a descent is given
INTERCEPT_DEG = 30


@dataclass
class Position:
    """The aircraft in the runway's frame of reference."""

    distance_nm: float  # straight-line distance from the threshold
    along_nm: float  # distance out along the extended centreline (negative: past the threshold)
    cross_nm: float  # distance from the centreline, always positive
    side: str  # "left" or "right" of the final approach course, looking in the landing direction


def locate(lat: float, lon: float, threshold: tuple[float, float], course_true: float) -> Position:
    distance = distance_nm(*threshold, lat, lon)
    offset = math.radians(bearing(*threshold, lat, lon) - (course_true + 180))
    along, cross = distance * math.cos(offset), distance * math.sin(offset)
    return Position(distance, along, abs(cross), "left" if cross > 0 else "right")


@dataclass
class Vector:
    stage: str  # "descend", "downwind", "base" or "intercept"
    heading_true: float | None
    altitude_ft: int


def _round_up(feet: float, step: int = 1000) -> int:
    return int(math.ceil(feet / step) * step)


def next_vector(position: Position, course_true: float, platform_ft: int, current_ft: float, safe_ft: int = 0) -> Vector:
    """The instruction that fits where the aircraft is now.

    `safe_ft` is the minimum safe altitude around the airport: nothing above the published
    approach's own joining altitude is given below it.
    """
    pattern_ft = max(platform_ft + 1000, int(math.ceil(safe_ft / 100) * 100))
    reciprocal = (course_true + 180) % 360
    toward = 1 if position.side == "left" else -1  # sign of the turn, from the course, that points at the centreline

    if position.distance_nm > VECTOR_RANGE_NM:
        # Roughly three degrees: 300 ft for every mile beyond the point where final is joined.
        target = platform_ft + 300 * (position.distance_nm - FINAL_NM - 5)
        return Vector("descend", None, max(pattern_ft, min(_round_up(target), _round_up(current_ft))))

    far_enough = position.along_nm >= FINAL_NM - 2
    if far_enough and position.cross_nm <= CLOSE_NM:
        turn = 0 if position.cross_nm < 0.7 else INTERCEPT_DEG
        return Vector("intercept", (course_true + toward * turn) % 360, platform_ft)
    if far_enough:
        return Vector("base", (course_true + toward * 90) % 360, platform_ft)
    # Not far enough out: fly away from the airport, parallel to the runway, drifting off the
    # centreline first if the aircraft is close to it so there is room for the base turn.
    drift = 20 if position.cross_nm < 4 else 0
    return Vector("downwind", (reciprocal + toward * drift) % 360, pattern_ft)


def turn_direction(current: float, target: float) -> str:
    return "right" if (target - current) % 360 <= 180 else "left"


def established(position: Position) -> bool:
    return position.cross_nm < 0.6 and 0 < position.along_nm < FINAL_NM + 3
