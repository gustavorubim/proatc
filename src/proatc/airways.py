"""Airways, so that a filed route is known waypoint by waypoint.

A flight plan says "ALFAX UL9 BRAVO": two waypoints and the airway between them. The waypoints
along UL9 between the two are in X-Plane's `earth_awy.dat` (Custom Data first, so Navigraph wins):

    BIG EG  3 DIKVU EG 11 F 2 250 460 UL9
    from, region, type, to, region, type, direction, class, base, top, airway name(s, joined with -)

With them the engine knows the whole track: it can clear a direct to any waypoint ahead, name the
next waypoint in an oceanic position report, and notice a flight that has left its route.
Parsed once and cached in data/airways-v1.pickle until the file changes.
"""

from __future__ import annotations

import pickle
import re
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_DIR
from .navdata import distance_nm

FOLDERS = ("Custom Data", "Resources/default data")
CACHE = PROJECT_DIR / "data" / "airways-v1.pickle"
MAX_LEG_NM = 1500  # a waypoint further than this from the one before is another waypoint of the same name


@dataclass
class Airways:
    segments: dict[str, dict[str, set[str]]] = field(default_factory=dict)  # airway -> waypoint -> its neighbours

    def __contains__(self, name: str) -> bool:
        return name in self.segments

    def between(self, airway: str, start: str, end: str) -> list[str] | None:
        """The waypoints along an airway after `start`, up to and including `end`; None if it does not join them."""
        graph = self.segments.get(airway)
        if not graph or start not in graph or end not in graph:
            return None
        came_from: dict[str, str | None] = {start: None}
        queue = deque([start])
        while queue:
            here = queue.popleft()
            if here == end:
                break
            for there in sorted(graph.get(here, ())):
                if there not in came_from:
                    came_from[there] = here
                    queue.append(there)
        if end not in came_from:
            return None
        path = []
        here: str | None = end
        while here is not None and here != start:
            path.append(here)
            here = came_from[here]
        return list(reversed(path))


def parse_airways(lines) -> Airways:
    found = Airways()
    for line in lines:
        try:
            t = line.split()
            if len(t) < 11 or not t[2].isdigit() or not t[5].isdigit():
                continue
            start, end = t[0], t[3]
            for name in t[10].split("-"):
                graph = found.segments.setdefault(name, {})
                # Direction (N both ways, F forward, B backward) is not enforced: a route is filed the legal way round.
                graph.setdefault(start, set()).add(end)
                graph.setdefault(end, set()).add(start)
        except (ValueError, IndexError):
            continue  # a malformed row is skipped, not fatal
    return found


COORDINATE = re.compile(r"(\d{2})(\d{2})?([NS])(\d{3})(\d{2})?([EW])")


def coordinate(token: str) -> tuple[float, float] | None:
    """A latitude and longitude filed as a waypoint, as on oceanic routes: 53N020W, 5230N02000W."""
    match = COORDINATE.fullmatch(token)
    if not match:
        return None
    lat = int(match.group(1)) + int(match.group(2) or 0) / 60
    lon = int(match.group(4)) + int(match.group(5) or 0) / 60
    if lat > 90 or lon > 180:
        return None
    return (lat if match.group(3) == "N" else -lat, lon if match.group(6) == "E" else -lon)


def spoken_coordinate(name: str) -> str | None:
    """'53N020W' -> 'five three north zero two zero west', as it is said in a position report."""
    from . import phraseology as say

    match = COORDINATE.fullmatch(name)
    if not match:
        return None
    words = {"N": "north", "S": "south", "E": "east", "W": "west"}
    lat = say.digits(match.group(1)) + (f" {say.digits(match.group(2))}" if match.group(2) and match.group(2) != "00" else "")
    lon = say.digits(match.group(4)) + (f" {say.digits(match.group(5))}" if match.group(5) and match.group(5) != "00" else "")
    return f"{lat} {words[match.group(3)]} {lon} {words[match.group(6)]}"


def expand(route: list[str], airways: Airways | None, navfix, start: tuple[float, float]) -> list[tuple[str, float, float]]:
    """A filed route as every waypoint it passes, with positions, in order.

    Tokens that are not waypoints or airways (DCT, SID and STAR names, speed and level groups) are
    dropped. A waypoint that cannot be placed is dropped too: the track then runs past it."""
    if not navfix:
        return []
    tokens = [token.upper().split("/")[0] for token in route]
    points: list[tuple[str, float, float]] = []
    here = start

    def add(name: str) -> bool:
        nonlocal here
        if points and points[-1][0] == name:
            return True
        where = coordinate(name) or navfix.position(name, *here, MAX_LEG_NM)
        if not where:
            return False
        points.append((name, where[0], where[1]))
        here = where
        return True

    previous: str | None = None
    for index, token in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if airways and token in airways and previous and following:
            for name in (airways.between(token, previous, following) or [])[:-1]:
                add(name)
            continue
        if token in ("DCT", "SID", "STAR") or not token.isalnum() or not (2 <= len(token) <= 5 or coordinate(token)):
            continue
        if add(token):
            previous = token
    return points


def next_ahead(points: list[tuple[str, float, float]], lat: float, lon: float) -> str | None:
    """The route's next waypoint in front of the aircraft."""
    if not points:
        return None
    nearest = min(range(len(points)), key=lambda i: distance_nm(lat, lon, points[i][1], points[i][2]))
    if nearest + 1 < len(points):
        leg = distance_nm(points[nearest][1], points[nearest][2], points[nearest + 1][1], points[nearest + 1][2])
        # Closer to the next waypoint than the nearest one is: the nearest is behind.
        if distance_nm(lat, lon, points[nearest + 1][1], points[nearest + 1][2]) < leg:
            return points[nearest + 1][0]
    return points[nearest][0]


def open_airways(xplane_root: Path | None, cache: Path = CACHE) -> Airways:
    if xplane_root is None:
        return Airways()
    path = next((xplane_root / f / "earth_awy.dat" for f in FOLDERS if (xplane_root / f / "earth_awy.dat").exists()), None)
    if path is None:
        return Airways()
    stamp = f"{path}:{path.stat().st_size}:{int(path.stat().st_mtime)}"
    if cache.exists():
        try:
            saved_stamp, data = pickle.loads(cache.read_bytes())
            if saved_stamp == stamp:
                return data
        except Exception:
            pass  # unreadable cache: rebuild it
    with open(path, encoding="utf-8", errors="ignore") as handle:
        data = parse_airways(handle)
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(pickle.dumps((stamp, data)))
    return data
