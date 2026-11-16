"""Waypoint positions, published holds and minimum safe altitudes from X-Plane's navigation data.

Files, looked for in `Custom Data` first (Navigraph) and then in `Resources/default data`:

    earth_fix.dat   lat lon IDENT area region ...                       waypoints
    earth_nav.dat   type lat lon elev freq range var IDENT area region  navaids
    earth_hold.dat  IDENT region area type inbound legtime legdist L|R min max speed
    earth_msa.dat   type IDENT region airport M (bearing alt radius)... 000 000 0
    earth_mora.dat  +LL +LLL then thirty one-degree cells, hundreds of feet

Everything is parsed once and cached in data/navfix-v1.pickle until a source file changes.
"""

from __future__ import annotations

import math
import pickle
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_DIR
from .navdata import distance_nm

FOLDERS = ("Custom Data", "Resources/default data")
FILES = ("earth_fix.dat", "earth_nav.dat", "earth_hold.dat", "earth_msa.dat", "earth_mora.dat")
CACHE = PROJECT_DIR / "data" / "navfix-v1.pickle"


@dataclass
class Hold:
    fix: str
    area: str  # airport ident for terminal holds, ENRT otherwise
    inbound_course: float  # magnetic
    turn: str  # "L" or "R"
    leg_minutes: float
    min_ft: int
    max_ft: int
    speed_kt: int


@dataclass
class NavFix:
    fixes: dict[str, list[tuple[float, float]]] = field(default_factory=dict)
    holds: dict[str, list[Hold]] = field(default_factory=dict)
    msa: dict[str, int] = field(default_factory=dict)  # airport -> highest sector altitude, feet
    mora: dict[tuple[int, int], int] = field(default_factory=dict)  # (lat, lon) of a cell's south-west corner -> feet

    # ---- lookups ------------------------------------------------------------

    def position(self, ident: str, lat: float, lon: float, within_nm: float = 400) -> tuple[float, float] | None:
        """The waypoint or navaid with this name nearest to a position."""
        options = self.fixes.get(ident.upper())
        if not options:
            return None
        best = min(options, key=lambda p: distance_nm(lat, lon, *p))
        return best if distance_nm(lat, lon, *best) <= within_nm else None

    def hold(self, ident: str, airport: str | None = None, level_ft: float | None = None) -> Hold | None:
        """The published hold at a fix: the airport's own if there is one, and the one that fits the level."""
        options = self.holds.get(ident.upper(), [])
        if not options:
            return None

        def rank(hold: Hold) -> tuple:
            fits = level_ft is None or hold.min_ft <= level_ft <= (hold.max_ft or 99999)
            return (hold.area != airport, not fits, hold.min_ft)

        return min(options, key=rank)

    def airport_safe_ft(self, airport: str) -> int | None:
        """The highest minimum sector altitude within 25 NM of the airport."""
        return self.msa.get(airport.upper())

    def grid_safe_ft(self, lat: float, lon: float) -> int | None:
        """The minimum off-route altitude of the one-degree cell containing a position."""
        return self.mora.get((math.floor(lat), math.floor(lon)))


# ---- parsing ----------------------------------------------------------------


def parse_fixes(fix_lines, nav_lines) -> dict[str, list[tuple[float, float]]]:
    fixes: dict[str, list[tuple[float, float]]] = {}
    for line in fix_lines:
        t = line.split(None, 3)
        if len(t) >= 3:
            try:
                fixes.setdefault(t[2], []).append((float(t[0]), float(t[1])))
            except ValueError:
                continue
    for line in nav_lines:
        t = line.split(None, 8)
        # NDB (2), VOR (3) and DME (12, 13) rows carry the ident in the eighth column.
        if len(t) >= 8 and t[0] in ("2", "3", "12", "13"):
            try:
                point = (float(t[1]), float(t[2]))
            except ValueError:
                continue
            if point not in fixes.setdefault(t[7], []):
                fixes[t[7]].append(point)
    return fixes


def parse_holds(lines) -> dict[str, list[Hold]]:
    holds: dict[str, list[Hold]] = {}
    for line in lines:
        t = line.split()
        if len(t) < 11 or t[7] not in ("L", "R"):
            continue
        try:
            hold = Hold(t[0], t[2], float(t[4]), t[7], float(t[5]) or 1.0, int(float(t[8])), int(float(t[9])), int(float(t[10])))
        except ValueError:
            continue
        holds.setdefault(t[0], []).append(hold)
    return holds


def parse_msa(lines) -> dict[str, int]:
    msa: dict[str, int] = {}
    for line in lines:
        t = line.split()
        if len(t) < 8 or t[4] != "M":
            continue
        sectors = t[5:]
        altitudes = []
        for index in range(0, len(sectors) - 2, 3):
            bearing, altitude, radius = sectors[index : index + 3]
            if bearing == "000" and altitude == "000":
                break
            if altitude.isdigit():
                altitudes.append(int(altitude) * 100)
        if altitudes:
            msa[t[3]] = max(msa.get(t[3], 0), max(altitudes))
    return msa


def parse_mora(lines) -> dict[tuple[int, int], int]:
    mora: dict[tuple[int, int], int] = {}
    for line in lines:
        t = line.split()
        if len(t) < 3 or t[0][0] not in "+-" or t[1][0] not in "+-":
            continue
        try:
            lat, lon = int(t[0]), int(t[1])
        except ValueError:
            continue
        for offset, value in enumerate(t[2:32]):
            if value.isdigit():
                mora[(lat, lon + offset)] = int(value) * 100
    return mora


def _find(root: Path, name: str) -> Path | None:
    return next((root / folder / name for folder in FOLDERS if (root / folder / name).exists()), None)


def _lines(path: Path | None) -> list[str]:
    return path.read_text(encoding="utf-8", errors="ignore").splitlines() if path else []


def open_navfix(xplane_root: Path | None, cache: Path = CACHE) -> NavFix:
    if xplane_root is None:
        return NavFix()
    paths = {name: _find(xplane_root, name) for name in FILES}
    stamp = "|".join(f"{p}:{p.stat().st_size}:{int(p.stat().st_mtime)}" if p else "-" for p in paths.values())
    if cache.exists():
        try:
            saved_stamp, data = pickle.loads(cache.read_bytes())
            if saved_stamp == stamp:
                return data
        except Exception:
            pass  # unreadable cache: rebuild it
    data = NavFix(
        fixes=parse_fixes(_lines(paths["earth_fix.dat"]), _lines(paths["earth_nav.dat"])),
        holds=parse_holds(_lines(paths["earth_hold.dat"])),
        msa=parse_msa(_lines(paths["earth_msa.dat"])),
        mora=parse_mora(_lines(paths["earth_mora.dat"])),
    )
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(pickle.dumps((stamp, data)))
    return data
