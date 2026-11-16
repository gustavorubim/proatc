"""Other aircraft: X-Plane's AI traffic, as the sim reports it on its TCAS datarefs.

The app does not control those aircraft. It sees them, so a controller can call traffic,
hold a departure for an aircraft on final, tell an arrival it is number two, and send it
around if the runway is still occupied.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import phraseology as say
from .navdata import Airport, Runway, bearing, distance_nm
from .vectors import locate

ADVISORY_NM = 6
ADVISORY_FT = 1500
REPEAT_AFTER_S = 180
FINAL_NM = 6  # how far out an aircraft on final matters to a departure
FINAL_BLOCKS_NM = 4
CLOCK = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"]


@dataclass
class Target:
    index: int
    lat: float
    lon: float
    altitude_ft: float
    heading_true: float
    vertical_fpm: float
    on_ground: bool
    kind: str = ""  # ICAO type, when the sim gives one


def targets(raw: dict) -> list[Target]:
    """The other aircraft the sim is tracking. Slot 0 is the user's own."""
    lats, lons, eles = raw.get("tcas_lat"), raw.get("tcas_lon"), raw.get("tcas_ele_m")
    if not isinstance(lats, list) or not isinstance(lons, list) or not isinstance(eles, list):
        return []
    headings = raw.get("tcas_heading") if isinstance(raw.get("tcas_heading"), list) else []
    rates = raw.get("tcas_vs_fpm") if isinstance(raw.get("tcas_vs_fpm"), list) else []
    wheels = raw.get("tcas_on_ground") if isinstance(raw.get("tcas_on_ground"), list) else []
    found = []
    for index in range(1, min(len(lats), len(lons), len(eles))):
        if abs(lats[index]) < 1e-6 and abs(lons[index]) < 1e-6:
            continue  # an empty slot
        found.append(
            Target(
                index,
                lats[index],
                lons[index],
                eles[index] / 0.3048,
                headings[index] if index < len(headings) else 0.0,
                rates[index] if index < len(rates) else 0.0,
                bool(wheels[index]) if index < len(wheels) else False,
            )
        )
    return found


def _clock(own_heading: float, bearing_to: float) -> int:
    hour = round(((bearing_to - own_heading) % 360) / 30) % 12
    return hour or 12


def advisory(raw: dict, seen: dict[int, float], now: float) -> str | None:
    """'traffic, two o'clock, five miles, opposite direction, one thousand feet above', for the nearest conflict."""
    if "lat" not in raw or bool(raw.get("on_ground", True)):
        return None
    own_ft = raw.get("elevation_m", 0.0) / 0.3048
    heading = raw.get("heading_true", raw.get("heading_mag", 0.0))
    nearest = None
    for target in targets(raw):
        if target.on_ground or now - seen.get(target.index, -1e9) < REPEAT_AFTER_S:
            continue
        miles = distance_nm(raw["lat"], raw["lon"], target.lat, target.lon)
        if miles <= ADVISORY_NM and abs(target.altitude_ft - own_ft) <= ADVISORY_FT and (nearest is None or miles < nearest[0]):
            nearest = (miles, target)
    if nearest is None:
        return None
    miles, target = nearest
    seen[target.index] = now
    hour = _clock(heading, bearing(raw["lat"], raw["lon"], target.lat, target.lon))
    crossing = abs((target.heading_true - heading + 180) % 360 - 180)
    direction = "same direction" if crossing < 45 else "opposite direction" if crossing > 135 else "crossing"
    gap = round((target.altitude_ft - own_ft) / 100) * 100
    if abs(gap) < 300:
        level = "same altitude"
    else:
        level = f"{say.altitude(abs(gap))} feet {'above' if gap > 0 else 'below'}"
    return f"traffic, {CLOCK[hour]} o'clock, {say.digits(max(1, round(miles)))} miles, {direction}, {level}"


def _runway_for(airport: Airport, ident: str) -> Runway | None:
    return next((r for r in airport.runways if ident in (r.end1, r.end2)), None)


def _threshold_and_course(runway: Runway, ident: str) -> tuple[tuple[float, float], float]:
    (end1, heading1), (end2, heading2) = runway.ends()
    if ident == end1:
        return (runway.lat1, runway.lon1), heading1
    return (runway.lat2, runway.lon2), heading2


def on_final(raw: dict, airport: Airport, ident: str, exclude_beyond_nm: float = FINAL_NM) -> float | None:
    """Distance from the threshold of the nearest other aircraft on final for this runway, if any."""
    runway = _runway_for(airport, ident)
    if not runway:
        return None
    threshold, course = _threshold_and_course(runway, ident)
    nearest = None
    for target in targets(raw):
        if target.on_ground:
            continue
        position = locate(target.lat, target.lon, threshold, course)
        aligned = abs((target.heading_true - course + 180) % 360 - 180) < 40
        low = target.altitude_ft < airport.elev_ft + 400 * max(position.along_nm, 1) + 800
        if 0 < position.along_nm <= exclude_beyond_nm and position.cross_nm < 0.5 and aligned and low:
            nearest = position.along_nm if nearest is None else min(nearest, position.along_nm)
    return nearest


def on_runway(raw: dict, airport: Airport, ident: str) -> bool:
    """Whether another aircraft is on this runway, on the ground."""
    runway = _runway_for(airport, ident)
    if not runway:
        return False
    threshold, course = _threshold_and_course(runway, ident)
    length_nm = runway.length_m / 1852
    for target in targets(raw):
        if not target.on_ground and target.altitude_ft > airport.elev_ft + 60:
            continue
        # Along the runway from its threshold, in the landing direction.
        position = locate(target.lat, target.lon, threshold, (course + 180) % 360)
        if -0.05 < position.along_nm < length_nm + 0.05 and position.cross_nm < 0.04:
            return True
    return False


def miles_word(miles: float) -> str:
    return say.digits(max(1, math.ceil(miles)))
