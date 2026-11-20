"""Radar vectors to final approach: where the aircraft is relative to the runway, and what to say next.

The pattern depends on where the aircraft comes from. From behind or abeam the airport it is a
downwind leg, a base leg and a 30 degree intercept. An aircraft already well out along the
approach is not turned through 90 degrees: it closes the centreline on a 45 degree dogleg. One
nearly lined up goes straight in. How far out final is joined differs from arrival to arrival
(`final_nm`), and a downwind leg is extended while other traffic is on final.
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
DOGLEG_DEG = 45
DOGLEG_BEYOND_NM = 4  # this much further out than the joining point, a dogleg replaces the base leg
LATEST_JOIN_NM = 2  # an intercept joins final no closer in than this inside where it is usually joined
BASE_DRIFT_NM = 1  # on a base leg that drifted in, one more mile: the heading is rounded, the turn takes room
# A 30 degree intercept from CLOSE_NM off the centreline meets it this far further in: base is turned that much
# further out, so that the intercept joins final where it should, not a mile from the runway far above the path.
BASE_BEYOND_NM = CLOSE_NM / math.tan(math.radians(INTERCEPT_DEG)) - LATEST_JOIN_NM
GLIDE_FT_PER_NM = 318.4  # a three degree glide path
DESCENT_FT_PER_NM = 400  # a little over three degrees: the steepest descent counted on to reach the glide path


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
    again: bool = False  # turned out from a base leg for a new one


def _round_up(feet: float, step: int = 1000) -> int:
    return int(math.ceil(feet / step) * step)


def next_vector(
    position: Position, course_true: float, platform_ft: int, current_ft: float, safe_ft: int = 0,
    final_nm: float = FINAL_NM, extend: bool = False, inbound: bool = False, on_base: bool = False, vectored: bool = False,
) -> Vector:  # fmt: skip
    """The instruction that fits where the aircraft is now.

    `safe_ft` is the minimum safe altitude around the airport: nothing above the published
    approach's own joining altitude is given below it. `final_nm` is where final is joined.
    `extend` keeps the aircraft on its downwind leg (traffic on final). `inbound` says it has not
    flown a downwind leg: it is coming from outside, and may close the centreline on a dogleg. `on_base` says it
    is flying the base leg it was given, `vectored` that it is on a downwind or base leg already.
    """
    pattern = pattern_ft(platform_ft, safe_ft)
    reciprocal = (course_true + 180) % 360
    toward = 1 if position.side == "left" else -1  # sign of the turn, from the course, that points at the centreline

    if position.distance_nm > VECTOR_RANGE_NM and not vectored:
        # Not yet vectored: only a descent this far out. One already on a downwind leg is turned onto base however
        # wide the leg is, or it would fly on with nothing more said.
        # Roughly three degrees: 300 ft for every mile beyond the point where final is joined.
        target = platform_ft + 300 * (position.distance_nm - final_nm - 5)
        return Vector("descend", None, max(pattern, min(_round_up(target), _round_up(current_ft))))

    far_enough = position.along_nm >= final_nm + BASE_BEYOND_NM
    latest = final_nm - LATEST_JOIN_NM - (BASE_DRIFT_NM if on_base else 0)
    if position.cross_nm <= CLOSE_NM and joins_at(position) >= latest:
        turn = 0 if position.cross_nm < 0.7 else INTERCEPT_DEG
        return Vector("intercept", (course_true + toward * turn) % 360, platform_ft)
    if on_base and position.along_nm < latest:
        # Drifted in so far that no intercept from here would join final in time: out again, for a new base.
        return turn_out(position, course_true, pattern, again=True)
    if far_enough and not extend:
        # At 45 degrees the aircraft comes in as far as it is from the centreline: far off to the side, that would
        # take it past the joining point, so it gets a base leg instead.
        room = position.along_nm - position.cross_nm >= final_nm
        dogleg = inbound and position.along_nm >= final_nm + BASE_BEYOND_NM + DOGLEG_BEYOND_NM and room
        return Vector("base", (course_true + toward * (DOGLEG_DEG if dogleg else 90)) % 360, platform_ft)
    if far_enough:
        return Vector("downwind", reciprocal, pattern)
    # Not far enough out: fly away from the airport, parallel to the runway, drifting off the
    # centreline first if the aircraft is close to it so there is room for the base turn.
    return turn_out(position, course_true, pattern)


def turn_out(position: Position, course_true: float, altitude_ft: int, again: bool = False) -> Vector:
    """Away from the airport, parallel to final, drifting off the centreline first when close to it: a downwind leg."""
    toward = 1 if position.side == "left" else -1
    drift = 20 if position.cross_nm < 4 else 0
    return Vector("downwind", ((course_true + 180) % 360 + toward * drift) % 360, altitude_ft, again=again)


def pattern_ft(platform_ft: int, safe_ft: int = 0) -> int:
    """The altitude of a downwind leg: above the joining altitude, and no lower than the minimum safe altitude."""
    return max(platform_ft + 1000, int(math.ceil(safe_ft / 100) * 100))


def too_high_ft(position: Position, current_ft: float, joining_ft: int, elevation_ft: float) -> float | None:
    """How far above what an intercept from here can get down from, in feet; negative when it can make it.

    Cleared now, the aircraft descends to `joining_ft` and meets the glide path from below where the path comes
    down to that altitude. It has the miles to the joining point, and along final to where the path meets the
    joining altitude, at no more than DESCENT_FT_PER_NM. None when the joining altitude is itself above the path
    where final is joined: no aircraft could meet the path from below there, whatever its height now."""
    join = joins_at(position)
    meets = (joining_ft - elevation_ft) / GLIDE_FT_PER_NM  # how far out the glide path is at the joining altitude
    if meets > join:
        return None
    miles = math.hypot(position.along_nm - join, position.cross_nm) + (join - meets)
    return current_ft - joining_ft - miles * DESCENT_FT_PER_NM


def joins_at(position: Position) -> float:
    """How far out a 30 degree intercept given now meets the final approach course (straight in when nearly on it)."""
    if position.cross_nm < 0.7:
        return position.along_nm
    return position.along_nm - position.cross_nm / math.tan(math.radians(INTERCEPT_DEG))


def turn_direction(current: float, target: float) -> str:
    return "right" if (target - current) % 360 <= 180 else "left"


def established(position: Position, final_nm: float = FINAL_NM) -> bool:
    return position.cross_nm < 0.6 and 0 < position.along_nm < final_nm + 3
