"""Taxi routing over the airport's own taxiway network from apt.dat (rows 1201/1202/1204/1300)."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from .navdata import bearing, distance_nm, latlon

CROSSING_PENALTY_M = 3000  # prefer routes that do not cross another runway
BACKTRACK_FACTOR = 3  # a metre along a runway counts as this many on a taxiway
STAND_REACH_M = 200  # a stand this close to a taxiway node can be taxied to
UNNAMED = ("none", "null")  # what some scenery tools write for a taxiway with no name
FULL_LENGTH_M = 400  # runway entries this close to the threshold count as full-length departures


@dataclass
class Edge:
    to: int
    name: str
    length_m: float
    runway: bool


@dataclass
class Parking:
    name: str
    lat: float
    lon: float
    kind: str  # gate, tie_down, hangar or misc
    categories: set[str] = field(default_factory=set)  # heavy, jets, turboprops, props, helos
    operation: str = ""  # airline, cargo, general_aviation or military (row 1301)
    airlines: set[str] = field(default_factory=set)  # ICAO airline codes that use this stand


@dataclass
class TaxiNetwork:
    nodes: dict[int, tuple[float, float]] = field(default_factory=dict)
    edges: dict[int, list[Edge]] = field(default_factory=dict)  # direction as published
    both_ways: dict[int, list[Edge]] = field(default_factory=dict)  # ignoring one-way restrictions
    runway_nodes: dict[str, set[int]] = field(default_factory=dict)  # "09R/27L" -> nodes on that runway
    parking: list[Parking] = field(default_factory=list)

    @property
    def usable(self) -> bool:
        return bool(self.nodes) and any(not e.runway for edges in self.edges.values() for e in edges)

    def taxi_nodes(self) -> set[int]:
        """Nodes that touch at least one taxiway (not only a runway)."""
        return {node for node, edges in self.both_ways.items() if any(not e.runway for e in edges)}

    def nearest_node(self, lat: float, lon: float, candidates: set[int] | None = None) -> int | None:
        pool = candidates if candidates is not None else self.taxi_nodes()
        return min(pool, key=lambda n: distance_nm(lat, lon, *self.nodes[n]), default=None)

    def runway_of(self, node: int, excluding: str | None = None) -> str | None:
        for name, nodes in self.runway_nodes.items():
            if node in nodes and name != excluding:
                return name
        return None

    def runways_of(self, node: int, excluding: str | None = None) -> list[str]:
        return [name for name, nodes in self.runway_nodes.items() if node in nodes and name != excluding]


@dataclass
class Route:
    taxiways: list[str]  # names in order, consecutive repeats collapsed
    crossings: list[str]  # other runways crossed on the way
    distance_m: float
    turns: list[str] = field(default_factory=list)  # "left" / "right" / "" onto each taxiway after the first
    points: list[tuple[float, float]] = field(default_factory=list)  # (lat, lon) along the route, for the map
    crossing_index: dict[str, int] = field(default_factory=dict)  # crossed runway -> where in `points` it is met


def parse_taxi(lines: list[str]) -> TaxiNetwork:
    net = TaxiNetwork()
    pending: list[tuple[int, int, bool, bool, str]] = []
    for line in lines:
        try:
            if line.startswith("1201 "):
                t = line.split()
                if len(t) >= 5:
                    net.nodes[int(t[4])] = latlon(t[1], t[2])
            elif line.startswith("1202 "):
                t = line.split(None, 5)
                if len(t) >= 5:
                    name = t[5].strip() if len(t) > 5 else ""
                    name = "" if name.lower() in UNNAMED else name  # not "via November Oscar November Echo"
                    pending.append((int(t[1]), int(t[2]), t[3] == "twoway", t[4] == "runway", name))
            elif line.startswith("1300 "):
                t = line.split(None, 6)
                if len(t) >= 6:
                    categories = set(t[5].split("|")) if len(t) > 5 else set()
                    net.parking.append(Parking(t[6].strip() if len(t) > 6 else "", *latlon(t[1], t[2]), t[4], categories))
            elif line.startswith("1301 ") and net.parking:
                t = line.split()
                if len(t) >= 3:
                    net.parking[-1].operation = t[2]
                    net.parking[-1].airlines = {code.upper() for code in t[3:]}
        except (ValueError, IndexError, KeyError, OverflowError):
            continue  # a malformed row is skipped, not fatal
    for a, b, twoway, runway, name in pending:
        if a not in net.nodes or b not in net.nodes:
            continue
        length = distance_nm(*net.nodes[a], *net.nodes[b]) * 1852
        net.edges.setdefault(a, []).append(Edge(b, name, length, runway))
        if twoway:
            net.edges.setdefault(b, []).append(Edge(a, name, length, runway))
        net.both_ways.setdefault(a, []).append(Edge(b, name, length, runway))
        net.both_ways.setdefault(b, []).append(Edge(a, name, length, runway))
        if runway:
            net.runway_nodes.setdefault(name, set()).update((a, b))
    return net


def _shortest(net: TaxiNetwork, graph: dict[int, list[Edge]], start: int, target_runway: str | None, backtrack: bool = False):
    """Dijkstra over taxiways only, or with `backtrack` along runways too. Returns (cost, previous-step) maps."""
    cost = {start: 0.0}
    previous: dict[int, tuple[int, Edge]] = {}
    queue = [(0.0, start)]
    while queue:
        dist, node = heapq.heappop(queue)
        if dist > cost.get(node, float("inf")):
            continue
        for edge in graph.get(node, []):
            if edge.runway and not backtrack:
                continue
            step = edge.length_m * (BACKTRACK_FACTOR if edge.runway else 1)
            if net.runway_of(edge.to, excluding=target_runway):
                step += CROSSING_PENALTY_M
            if dist + step < cost.get(edge.to, float("inf")):
                cost[edge.to] = dist + step
                previous[edge.to] = (node, edge)
                heapq.heappush(queue, (dist + step, edge.to))
    return cost, previous


def _build(net: TaxiNetwork, previous, start: int, goal: int, target_runway: str | None) -> Route:
    steps: list[tuple[str, float]] = []  # (taxiway, direction of travel) per edge, goal first
    on_runway: list[bool] = []
    path = [goal]
    distance = 0.0
    node = goal
    while node != start:
        prior, edge = previous[node]
        distance += edge.length_m
        steps.append((edge.name, bearing(*net.nodes[prior], *net.nodes[node])))
        on_runway.append(edge.runway)
        node = prior
        path.append(node)
    steps.reverse()
    on_runway.reverse()
    path.reverse()
    # In the order the runways are met, every runway at each node: where two runways cross, both are crossed.
    crossing_index: dict[str, int] = {}
    for index, node in enumerate(path[1:-1], start=1):
        for crossed in net.runways_of(node, excluding=target_runway):
            crossing_index.setdefault(crossed, index)
    crossings = list(crossing_index)
    names: list[str] = []
    turns: list[str] = []
    heading = None
    for (name, direction), runway in zip(steps, on_runway, strict=True):
        if runway:
            continue  # a stretch along a runway is said as that runway's crossing clearance, not as a taxiway
        if name and (not names or names[-1] != name):
            if names:
                change = (direction - heading + 180) % 360 - 180 if heading is not None else 0.0
                turns.append("right" if change > 25 else "left" if change < -25 else "")
            names.append(name)
        heading = direction
    return Route(names, crossings, distance, turns, [net.nodes[n] for n in path], crossing_index)


def _route(net: TaxiNetwork, start: int, goals: dict[int, float], target_runway: str | None) -> Route | None:
    """Best route to any goal; `goals` maps node -> extra cost. One-way rules are dropped only if they block every route,
    and a stretch along a runway is taken only when the taxiways alone do not connect (scenery whose taxiways meet
    only on the runway, as at many small airports): it is then a runway to be cleared onto, like a crossing."""
    for graph, backtrack in ((net.edges, False), (net.both_ways, False), (net.both_ways, True)):
        cost, previous = _shortest(net, graph, start, target_runway, backtrack)
        reachable = [(cost[g] + extra, g) for g, extra in goals.items() if g in cost]
        if reachable:
            return _build(net, previous, start, min(reachable)[1], target_runway)
    return None


def route_to_runway(net: TaxiNetwork, lat: float, lon: float, runway_end: str, threshold: tuple[float, float]) -> Route | None:
    """Taxi route from the aircraft to the holding point of `runway_end` (e.g. '27L')."""
    if not net.usable:
        return None
    runway_name = next((name for name in net.runway_nodes if runway_end in name.split("/")), None)
    start = net.nearest_node(lat, lon)
    if runway_name is None or start is None:
        return None
    entries = net.runway_nodes[runway_name] & net.taxi_nodes()
    if not entries:
        return None
    gap = {n: distance_nm(*threshold, *net.nodes[n]) * 1852 for n in entries}
    full_length = {n: 0.0 for n in entries if gap[n] <= FULL_LENGTH_M}
    # Without an entry at the threshold, take the one that leaves the most runway ahead.
    goals = full_length or {n: gap[n] * 10 for n in entries}
    return _route(net, start, goals, runway_name)


def nearest_parking(net: TaxiNetwork, lat: float, lon: float) -> Parking | None:
    return min(net.parking, key=lambda p: distance_nm(lat, lon, p.lat, p.lon), default=None)


HEAVY_TYPES = ("B74", "B77", "B78", "B76", "B75", "A33", "A34", "A35", "A38", "A30", "A31", "MD1")
JET_TYPES = ("A2", "A3", "B7", "E1", "E2", "E7", "CR", "MD", "BC", "F7", "F1", "C5", "C6", "C7", "GL", "LJ")
TURBOPROP_TYPES = ("AT", "DH", "SF", "B3", "PC", "TB", "SW")


def aircraft_category(icao_type: str | None, airline: bool = False) -> str:
    """The apt.dat stand category an aircraft belongs to, from its ICAO type."""
    kind = (icao_type or "").upper()
    if kind[:3] in HEAVY_TYPES:
        return "heavy"
    if kind[:2] in JET_TYPES:
        return "jets"
    if kind[:2] in TURBOPROP_TYPES:
        return "turboprops"
    return "jets" if airline else "props"


def assign_stand(net: TaxiNetwork, category: str, airline: str | None, near: tuple[float, float], rng) -> Parking | None:
    """A stand for an arriving aircraft: one that takes its size, its airline's if the scenery says so,
    otherwise one of the nearer suitable ones (not always the same, as at a real airport)."""
    stands = [p for p in net.parking if p.kind in ("gate", "tie_down") and p.name]
    fits = [p for p in stands if category in p.categories]
    if not fits:
        return None
    wanted = "airline" if category in ("heavy", "jets") else "general_aviation"
    for pool in (
        [p for p in fits if airline and airline in p.airlines],
        [p for p in fits if p.operation == wanted and (category == "props" or p.kind == "gate")],
        [p for p in fits if p.operation == wanted],
        fits,
    ):
        if pool:
            nearest = sorted(pool, key=lambda p: distance_nm(*near, p.lat, p.lon))[:6]
            # A stand out on an apron the taxiway network does not come near cannot be taxied to: one that it does.
            served = [p for p in nearest if _reaches(net, p)]
            return rng.choice(served or nearest)
    return None


def _reaches(net: TaxiNetwork, stand: Parking) -> bool:
    node = net.nearest_node(stand.lat, stand.lon)
    return node is not None and distance_nm(stand.lat, stand.lon, *net.nodes[node]) * 1852 <= STAND_REACH_M


def route_to_point(net: TaxiNetwork, lat: float, lon: float, target: tuple[float, float]) -> Route | None:
    """Taxi route from the aircraft to the taxiway node nearest `target` (a stand or apron)."""
    if not net.usable:
        return None
    start = net.nearest_node(lat, lon)
    goal = net.nearest_node(*target)
    if start is None or goal is None:
        return None
    if start == goal:
        return Route([], [], 0.0, points=[net.nodes[start]])
    return _route(net, start, {goal: 0.0}, None)
