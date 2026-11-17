"""En-route control centres from X-Plane's atc.dat: names, frequencies and airspace boundaries.

CONTROLLER
NAME LONDON
FACILITY_ID EGTT
ROLE ctr
FREQ 12752                      (10 kHz units)
AIRSPACE_POLYGON_BEGIN 0 60000  (floor and ceiling, feet)
POINT 51.5 -0.1
AIRSPACE_POLYGON_END
CONTROLLER_END
"""

from __future__ import annotations

import pickle
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_DIR
from .navdata import FREQ_TOLERANCE_KHZ

ATC_FILES = (
    "Custom Data/1200 atc data/Earth nav data/atc.dat",
    "Resources/default scenery/1200 atc data/Earth nav data/atc.dat",
)
CACHE = PROJECT_DIR / "data" / "centers-v1.pickle"


@dataclass
class Polygon:
    floor_ft: float
    ceiling_ft: float
    points: list[tuple[float, float]]
    box: tuple[float, float, float, float] = (0, 0, 0, 0)  # min lat, max lat, min lon, max lon

    def contains(self, lat: float, lon: float) -> bool:
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


@dataclass
class Center:
    name: str
    facility: str
    freqs_khz: list[int] = field(default_factory=list)
    polygons: list[Polygon] = field(default_factory=list)

    @property
    def callsign(self) -> str:
        name = self.name.title()
        if any(word in name.split() for word in ("Control", "Center", "Centre", "Radar", "Radio", "Information")):
            return name
        # United States and Canada say "Center"; the rest of the world says "Control".
        return f"{name} {'Center' if self.facility[:1] in ('K', 'C', 'P') else 'Control'}"

    @property
    def area(self) -> float:
        return sum((p.box[1] - p.box[0]) * (p.box[3] - p.box[2]) for p in self.polygons)

    def contains(self, lat: float, lon: float, altitude_ft: float | None = None) -> bool:
        for polygon in self.polygons:
            if altitude_ft is not None and not polygon.floor_ft <= altitude_ft <= polygon.ceiling_ft:
                continue
            if polygon.contains(lat, lon):
                return True
        return False

    def has_frequency(self, khz: int) -> bool:
        return any(abs(freq - khz) <= FREQ_TOLERANCE_KHZ for freq in self.freqs_khz)


@dataclass
class CenterStation:
    """A centre on a frequency, shaped like navdata.Station where the two are used alike."""

    center: Center
    khz: int
    kind: str = "center"
    airport = None
    label: str = ""
    distance_nm: float = 0.0

    @property
    def callsign(self) -> str:
        return self.center.callsign

    @property
    def key(self) -> str:
        return f"CTR:{self.center.facility}:{self.center.name}"


def parse_atc(lines) -> list[Center]:
    centers: list[Center] = []
    current: Center | None = None
    role = ""
    polygon: Polygon | None = None
    for line in lines:
        try:
            word, _, rest = line.strip().partition(" ")
            rest = rest.strip()
            if word == "CONTROLLER":
                current, role = Center("", ""), ""
            elif current is None:
                continue
            elif word == "NAME":
                current.name = rest
            elif word == "FACILITY_ID":
                current.facility = rest
            elif word == "ROLE":
                role = rest.lower()
            elif word == "FREQ" and rest.isdigit():
                current.freqs_khz.append(int(rest) * 10)
            elif word == "AIRSPACE_POLYGON_BEGIN":
                limits = rest.split()
                polygon = Polygon(float(limits[0]) if limits else 0, float(limits[1]) if len(limits) > 1 else 99999, [])
            elif word == "POINT" and polygon is not None:
                lat, lon = rest.split()[:2]
                polygon.points.append((float(lat), float(lon)))
            elif word == "AIRSPACE_POLYGON_END" and polygon is not None:
                if len(polygon.points) >= 3:
                    lats, lons = [p[0] for p in polygon.points], [p[1] for p in polygon.points]
                    polygon.box = (min(lats), max(lats), min(lons), max(lons))
                    current.polygons.append(polygon)
                polygon = None
            elif word == "CONTROLLER_END":
                if role == "ctr" and current.name and current.freqs_khz and current.polygons:
                    centers.append(current)
                current = None
        except (ValueError, IndexError, KeyError, OverflowError):
            continue  # a malformed row is skipped, not fatal
    return centers


class CenterIndex:
    def __init__(self, centers: list[Center]):
        self.centers = centers

    @classmethod
    def open(cls, xplane_root: Path | None, cache: Path = CACHE) -> CenterIndex:
        """Load the centres, parsing atc.dat only when it has changed since the last run."""
        path = next((xplane_root / name for name in ATC_FILES if (xplane_root / name).exists()), None) if xplane_root else None
        if path is None:
            return cls([])
        stat = path.stat()
        stamp = f"{path}:{stat.st_size}:{int(stat.st_mtime)}"
        if cache.exists():
            try:
                saved_stamp, centers = pickle.loads(cache.read_bytes())
                if saved_stamp == stamp:
                    return cls(centers)
            except Exception:
                pass  # unreadable cache: rebuild it
        centers = parse_atc(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_bytes(pickle.dumps((stamp, centers)))
        return cls(centers)

    def at(self, lat: float, lon: float, altitude_ft: float | None = None) -> Center | None:
        """The centre responsible for this position: the smallest airspace that contains it."""
        inside = [c for c in self.centers if c.contains(lat, lon, altitude_ft)]
        if not inside and altitude_ft is not None:
            inside = [c for c in self.centers if c.contains(lat, lon)]
        return min(inside, key=lambda c: c.area, default=None)

    def on_frequency(self, khz: int, lat: float, lon: float) -> CenterStation | None:
        """A centre that covers this position and works this frequency."""
        inside = [c for c in self.centers if c.has_frequency(khz) and c.contains(lat, lon)]
        best = min(inside, key=lambda c: c.area, default=None)
        return CenterStation(best, khz) if best else None

    def station_for(self, center: Center) -> CenterStation:
        return CenterStation(center, center.freqs_khz[0])
