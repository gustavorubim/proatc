"""Controlled and restricted airspace from X-Plane's own airspace file (OpenAir format).

`Custom Data/airspaces/airspace.txt` when present, else `Resources/default data/airspaces/airspace.txt`:

    AC D                 class: A B C D, CTR (control zone), R (restricted), P (prohibited), Q (danger)
    AN LONDON CTR        name
    AL GND               lower limit: GND, SFC, 1500 MSL, 1500 AGL, FL65
    AH 2500 MSL          upper limit
    DP 51:36:11 N 000:41:33 W   boundary points

Parsed once and cached in data/airspace-v1.pickle until the file changes. Lookups go through a
one-degree grid, so asking "what am I in?" every second is cheap.
"""

from __future__ import annotations

import math
import pickle
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_DIR
from .navdata import bearing, distance_nm

FOLDERS = ("Custom Data", "Resources/default data")
CACHE = PROJECT_DIR / "data" / "airspace-v1.pickle"
CONTROLLED = ("B", "C", "D", "CTR")  # needs a clearance, or at least two-way radio, to enter VFR
SPECIAL = {"R": "restricted area", "P": "prohibited area", "Q": "danger area"}
ZONE_RADIUS_NM = 5.0  # a towered airport with no zone in the data gets a circle this big
ZONE_HEIGHT_FT = 2500
UNLIMITED_FT = 99999.0
POINT = re.compile(r"(\d+):(\d+):(\d+(?:\.\d+)?)\s*([NS])\s+(\d+):(\d+):(\d+(?:\.\d+)?)\s*([EW])")


@dataclass
class Airspace:
    kind: str  # B, C, D, CTR, R, P, Q (class A is not kept: it is no business of a VFR flight below it)
    name: str
    floor_ft: float
    ceiling_ft: float
    floor_agl: bool  # the floor is a height above the ground, not an altitude
    points: list[tuple[float, float]]
    box: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)  # min lat, max lat, min lon, max lon

    def __post_init__(self):
        if self.points and self.box == (0.0, 0.0, 0.0, 0.0):
            lats, lons = [p[0] for p in self.points], [p[1] for p in self.points]
            self.box = (min(lats), max(lats), min(lons), max(lons))

    @property
    def surface(self) -> bool:
        return self.floor_ft <= 0

    @property
    def area(self) -> float:
        return (self.box[1] - self.box[0]) * (self.box[3] - self.box[2])

    def covers(self, lat: float, lon: float) -> bool:
        """Whether the point is inside the boundary, at any height."""
        if not (self.box[0] <= lat <= self.box[1] and self.box[2] <= lon <= self.box[3]):
            return False
        inside = False
        previous = self.points[-1]
        for point in self.points:
            (lat1, lon1), (lat2, lon2) = previous, point
            if (lon1 > lon) != (lon2 > lon) and lat < (lat2 - lat1) * (lon - lon1) / (lon2 - lon1) + lat1:
                inside = not inside
            previous = point
        return inside

    def contains(self, lat: float, lon: float, altitude_ft: float, ground_ft: float = 0.0) -> bool:
        floor = self.floor_ft + ground_ft if self.floor_agl else self.floor_ft
        return floor <= altitude_ft <= self.ceiling_ft and self.covers(lat, lon)

    def describe(self) -> dict:
        return {"kind": self.kind, "name": self.name.title(), "floor_ft": int(self.floor_ft),
                "ceiling_ft": None if self.ceiling_ft >= UNLIMITED_FT else int(self.ceiling_ft)}  # fmt: skip


def _limit(text: str) -> tuple[float, bool]:
    """(feet, is a height above ground) for an AL or AH value."""
    text = text.upper().strip()
    if text.startswith(("GND", "SFC")) or text in ("0", ""):
        return 0.0, False
    if text.startswith("UNL"):
        return UNLIMITED_FT, False
    number = re.search(r"\d+", text)
    if not number:
        return 0.0, False
    value = float(number.group(0))
    if text.startswith("FL"):
        return value * 100, False
    return value, "AGL" in text or "AGND" in text


def parse_openair(lines) -> list[Airspace]:
    found: list[Airspace] = []
    kind = name = None
    floor, ceiling, points = (0.0, False), (UNLIMITED_FT, False), []

    def finish() -> None:
        if kind in (*CONTROLLED, *SPECIAL) and len(points) >= 3:
            found.append(Airspace(kind, name or "", floor[0], ceiling[0], floor[1], list(points)))

    for line in lines:
        tag, _, rest = line.strip().partition(" ")
        if tag == "AC":
            finish()
            kind, name, floor, ceiling, points = rest.strip().upper(), None, (0.0, False), (UNLIMITED_FT, False), []
        elif tag == "AN":
            name = " ".join(rest.split())
        elif tag == "AL":
            floor = _limit(rest)
        elif tag == "AH":
            ceiling = _limit(rest)
        elif tag == "DP":
            match = POINT.search(rest)
            if match:
                d1, m1, s1, ns, d2, m2, s2, ew = match.groups()
                lat = (int(d1) + int(m1) / 60 + float(s1) / 3600) * (1 if ns == "N" else -1)
                lon = (int(d2) + int(m2) / 60 + float(s2) / 3600) * (1 if ew == "E" else -1)
                points.append((lat, lon))
    finish()
    return found


def circle(lat: float, lon: float, radius_nm: float, steps: int = 36) -> list[tuple[float, float]]:
    scale = max(math.cos(math.radians(lat)), 0.05)
    return [
        (lat + radius_nm / 60 * math.cos(math.radians(a)), lon + radius_nm / 60 * math.sin(math.radians(a)) / scale)
        for a in range(0, 360, 360 // steps)
    ]


@dataclass
class AirspaceIndex:
    spaces: list[Airspace] = field(default_factory=list)
    grid: dict[tuple[int, int], list[int]] = field(default_factory=dict)

    def __post_init__(self):
        if self.spaces and not self.grid:
            for number, space in enumerate(self.spaces):
                south, north, west, east = space.box
                if north - south > 40 or east - west > 60:
                    continue  # a boundary that wraps the date line or covers an ocean: not a VFR matter
                for lat in range(math.floor(south), math.floor(north) + 1):
                    for lon in range(math.floor(west), math.floor(east) + 1):
                        self.grid.setdefault((lat, lon), []).append(number)

    def near(self, lat: float, lon: float) -> list[Airspace]:
        return [self.spaces[number] for number in self.grid.get((math.floor(lat), math.floor(lon)), [])]

    def at(self, lat: float, lon: float, altitude_ft: float, ground_ft: float = 0.0) -> list[Airspace]:
        """The airspaces the aircraft is inside, smallest first."""
        inside = [space for space in self.near(lat, lon) if space.contains(lat, lon, altitude_ft, ground_ft)]
        return sorted(inside, key=lambda space: space.area)

    def zone(self, lat: float, lon: float, elevation_ft: float) -> Airspace | None:
        """The control zone (or class B, C or D surface area) around an airport, if the data has one."""
        zones = [
            space
            for space in self.near(lat, lon)
            if space.kind in CONTROLLED and space.floor_ft <= elevation_ft + 200 and space.covers(lat, lon)
        ]
        return min(zones, key=lambda space: space.area, default=None)

    def ahead(self, lat: float, lon: float, track: float, altitude_ft: float, within_nm: float, kinds, skip=()) -> tuple:
        """(airspace, miles) for the first airspace of these kinds on the present track that the aircraft
        is not already in, or (None, None)."""
        scale = max(math.cos(math.radians(lat)), 0.05)
        here = {id(space) for space in self.at(lat, lon, altitude_ft)}
        for step in range(1, int(within_nm) + 1):
            there_lat = lat + step / 60 * math.cos(math.radians(track))
            there_lon = lon + step / 60 * math.sin(math.radians(track)) / scale
            for space in self.near(there_lat, there_lon):
                if space.kind in kinds and id(space) not in here and space.name not in skip:
                    if space.contains(there_lat, there_lon, altitude_ft):
                        return space, step
        return None, None


def fallback_zone(name: str, lat: float, lon: float, elevation_ft: float) -> Airspace:
    """A plain circle for a towered airport the airspace data says nothing about."""
    ceiling = math.ceil((elevation_ft + ZONE_HEIGHT_FT) / 500) * 500
    return Airspace("CTR", f"{name} control zone", 0.0, float(ceiling), False, circle(lat, lon, ZONE_RADIUS_NM))


def exit_bearing(space: Airspace, lat: float, lon: float) -> float:
    """The direction from the middle of an airspace to the aircraft: which way it is leaving."""
    middle = ((space.box[0] + space.box[1]) / 2, (space.box[2] + space.box[3]) / 2)
    return bearing(*middle, lat, lon)


def edge_distance_nm(space: Airspace, lat: float, lon: float) -> float:
    """Roughly how far the nearest boundary point is."""
    return min(distance_nm(lat, lon, *point) for point in space.points)


def open_airspace(xplane_root: Path | None, cache: Path = CACHE) -> AirspaceIndex:
    if xplane_root is None:
        return AirspaceIndex()
    path = next(
        (
            xplane_root / f / "airspaces" / "airspace.txt"
            for f in FOLDERS
            if (xplane_root / f / "airspaces" / "airspace.txt").exists()
        ),
        None,
    )
    if path is None:
        return AirspaceIndex()
    stamp = f"{path}:{path.stat().st_size}:{int(path.stat().st_mtime)}"
    if cache.exists():
        try:
            saved_stamp, spaces = pickle.loads(cache.read_bytes())
            if saved_stamp == stamp:
                return AirspaceIndex(spaces)
        except Exception:
            pass  # unreadable cache: rebuild it
    with open(path, encoding="utf-8", errors="ignore") as handle:
        spaces = parse_openair(handle)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(pickle.dumps((stamp, spaces)))
    return AirspaceIndex(spaces)
