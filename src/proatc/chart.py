"""The airport map the app draws: pavement, runways, taxiways and stands from the airport's apt.dat rows.

Everything is in metres east and north of the airport's reference point, so the app needs no geography.
The map never decides anything: it shows the taxi route the engine already gave.
"""

from __future__ import annotations

import math

from .navdata import Airport, latlon
from .taxi import TaxiNetwork

HOLD_BACK_M = 75  # the hold-short mark is drawn this far before the runway's centreline
OFF_ROUTE_M = 250  # further than this from the highlighted route, it is worked out again
LABEL_GAP_M = 400  # a taxiway's name is repeated no closer than this
LABELS_PER_TAXIWAY = 3

Point = tuple[float, float]


def project(origin: Point, lat: float, lon: float) -> Point:
    """(metres east, metres north) of `origin`."""
    east = (lon - origin[1]) * math.cos(math.radians(origin[0])) * 111_320
    north = (lat - origin[0]) * 110_574
    return round(east, 1), round(north, 1)


def _pavement(lines: list[str], origin: Point) -> list[list[list[float]]]:
    """Taxiway and apron pavement (rows 110 to 114): a list of polygons, each a list of rings of x, y, x, y...

    The first ring is the outline, the rest are holes. Curves are drawn as straight lines between their points."""
    polygons: list[list[list[float]]] = []
    rings: list[list[float]] | None = None
    ring: list[float] = []
    for line in lines:
        t = line.split()
        if not t:
            continue
        code = t[0]
        if code in ("111", "112", "113", "114") and rings is not None:
            try:
                ring.extend(project(origin, *latlon(t[1], t[2])))
            except (ValueError, IndexError):
                continue
            if code in ("113", "114"):
                if len(ring) >= 6:
                    rings.append(ring)
                ring = []
            continue
        if rings:
            polygons.append(rings)
        rings, ring = ([], []) if code == "110" else (None, [])
    if rings:
        polygons.append(rings)
    return polygons


def _runways(lines: list[str], origin: Point) -> list[dict]:
    found = []
    for line in lines:
        if not line.startswith("100 "):
            continue
        t = line.split()
        try:
            ends = [[t[8], *project(origin, *latlon(t[9], t[10]))], [t[17], *project(origin, *latlon(t[18], t[19]))]]
            found.append({"name": f"{t[8]}/{t[17]}", "width": round(float(t[1]), 1), "ends": ends})
        except (ValueError, IndexError):
            continue
    return found


def build_chart(airport: Airport, lines: list[str], net: TaxiNetwork) -> dict:
    """Everything about an airport that does not change while taxiing."""
    origin = (airport.lat, airport.lon)
    nodes = {node: project(origin, *where) for node, where in net.nodes.items()}
    lines_by_name: dict[str, list[float]] = {}
    longest: dict[str, list[tuple[float, Point]]] = {}
    for a, edges in net.both_ways.items():
        for edge in edges:
            if edge.runway or edge.to < a:  # each taxiway segment once
                continue
            (x1, y1), (x2, y2) = nodes[a], nodes[edge.to]
            lines_by_name.setdefault(edge.name, []).extend((x1, y1, x2, y2))
            if edge.name:
                longest.setdefault(edge.name, []).append((edge.length_m, ((x1 + x2) / 2, (y1 + y2) / 2)))
    labels = []
    for name, candidates in longest.items():
        placed: list[Point] = []
        for _, middle in sorted(candidates, reverse=True):
            if all(math.dist(middle, other) >= LABEL_GAP_M for other in placed):
                placed.append(middle)
                labels.append({"name": name, "at": [round(middle[0], 1), round(middle[1], 1)]})
            if len(placed) == LABELS_PER_TAXIWAY:
                break
    runways = _runways(lines, origin)
    pavement = _pavement(lines, origin)
    stands = [{"name": p.name, "at": list(project(origin, p.lat, p.lon))} for p in net.parking]
    xs: list[float] = []
    ys: list[float] = []
    for runway in runways:
        for _, x, y in runway["ends"]:
            xs.append(x)
            ys.append(y)
    for x, y in nodes.values():
        xs.append(x)
        ys.append(y)
    for stand in stands:
        xs.append(stand["at"][0])
        ys.append(stand["at"][1])
    for polygon in pavement:
        xs.extend(polygon[0][0::2])
        ys.extend(polygon[0][1::2])
    bounds = [min(xs), min(ys), max(xs), max(ys)] if xs else [-500.0, -500.0, 500.0, 500.0]
    return {
        "ident": airport.ident,
        "name": airport.name,
        "runways": runways,
        "pavement": pavement,
        "taxiways": [{"name": name, "lines": flat} for name, flat in sorted(lines_by_name.items())],
        "labels": labels,
        "stands": stands,
        "bounds": bounds,
    }


def _to_segment(p: Point, a: Point, b: Point) -> float:
    ax, ay, bx, by = *a, *b
    length2 = (bx - ax) ** 2 + (by - ay) ** 2
    if length2 == 0:
        return math.dist(p, a)
    along = max(0.0, min(1.0, ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay)) / length2))
    return math.dist(p, (ax + along * (bx - ax), ay + along * (by - ay)))


def next_point(path: list[Point], position: Point) -> tuple[int, float]:
    """(index of the point on `path` the aircraft is heading for, metres the aircraft is off the path)."""
    if len(path) < 2:
        return 0, math.dist(position, path[0]) if path else 0.0
    off, index = min((_to_segment(position, path[i], path[i + 1]), i) for i in range(len(path) - 1))
    return index + 1, off


def split_route(path: list[Point], position: Point, hold_index: int | None) -> tuple[list[Point], list[Point], Point | None]:
    """The route still ahead, in two parts: up to the hold-short mark (cleared), and after it. And the mark."""
    ahead, _ = next_point(path, position)
    rest = [position, *path[ahead:]]
    if hold_index is None or hold_index < ahead:
        return rest, [], None
    k = hold_index - ahead + 1  # the runway's point, in `rest`
    need = HOLD_BACK_M
    while k > 0:
        a, b = rest[k - 1], rest[k]
        length = math.dist(a, b)
        if length >= need:
            part = need / length if length else 0.0
            stop = (round(b[0] + (a[0] - b[0]) * part, 1), round(b[1] + (a[1] - b[1]) * part, 1))
            return [*rest[:k], stop], [stop, *rest[k:]], stop
        need -= length
        k -= 1
    return [rest[0]], rest, rest[0]  # already at the runway
