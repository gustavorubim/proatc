"""Taxi routing over the airport's own taxiway network from apt.dat (rows 1201/1202/1204/1300)."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field

from .navdata import distance_nm

CROSSING_PENALTY_M = 3000  # prefer routes that do not cross another runway
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
    kind: str


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


@dataclass
class Route:
    taxiways: list[str]  # names in order, consecutive repeats collapsed
    crossings: list[str]  # other runways crossed on the way
    distance_m: float


def parse_taxi(lines: list[str]) -> TaxiNetwork:
    net = TaxiNetwork()
    pending: list[tuple[int, int, bool, bool, str]] = []
    for line in lines:
        if line.startswith("1201 "):
            t = line.split()
            if len(t) >= 5:
                net.nodes[int(t[4])] = (float(t[1]), float(t[2]))
        elif line.startswith("1202 "):
            t = line.split(None, 5)
            if len(t) >= 5:
                pending.append((int(t[1]), int(t[2]), t[3] == "twoway", t[4] == "runway", t[5].strip() if len(t) > 5 else ""))
        elif line.startswith("1300 "):
            t = line.split(None, 6)
            if len(t) >= 6:
                net.parking.append(Parking(t[6].strip() if len(t) > 6 else "", float(t[1]), float(t[2]), t[4]))
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


def _shortest(net: TaxiNetwork, graph: dict[int, list[Edge]], start: int, target_runway: str | None):
    """Dijkstra over taxiways only. Returns (cost, previous-step) maps."""
    cost = {start: 0.0}
    previous: dict[int, tuple[int, Edge]] = {}
    queue = [(0.0, start)]
    while queue:
        dist, node = heapq.heappop(queue)
        if dist > cost.get(node, float("inf")):
            continue
        for edge in graph.get(node, []):
            if edge.runway:
                continue
            step = edge.length_m
            if net.runway_of(edge.to, excluding=target_runway):
                step += CROSSING_PENALTY_M
            if dist + step < cost.get(edge.to, float("inf")):
                cost[edge.to] = dist + step
                previous[edge.to] = (node, edge)
                heapq.heappush(queue, (dist + step, edge.to))
    return cost, previous


def _build(net: TaxiNetwork, previous, start: int, goal: int, target_runway: str | None) -> Route:
    names: list[str] = []
    crossings: list[str] = []
    distance = 0.0
    node = goal
    while node != start:
        prior, edge = previous[node]
        distance += edge.length_m
        if edge.name and (not names or names[-1] != edge.name):
            names.append(edge.name)
        crossed = net.runway_of(node, excluding=target_runway) if node != goal else None
        if crossed and crossed not in crossings:
            crossings.append(crossed)
        node = prior
    names.reverse()
    crossings.reverse()
    return Route(names, crossings, distance)


def _route(net: TaxiNetwork, start: int, goals: dict[int, float], target_runway: str | None) -> Route | None:
    """Best route to any goal; `goals` maps node -> extra cost. One-way rules are dropped only if they block every route."""
    for graph in (net.edges, net.both_ways):
        cost, previous = _shortest(net, graph, start, target_runway)
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


def route_to_point(net: TaxiNetwork, lat: float, lon: float, target: tuple[float, float]) -> Route | None:
    """Taxi route from the aircraft to the taxiway node nearest `target` (a stand or apron)."""
    if not net.usable:
        return None
    start = net.nearest_node(lat, lon)
    goal = net.nearest_node(*target)
    if start is None or goal is None:
        return None
    if start == goal:
        return Route([], [], 0.0)
    return _route(net, start, {goal: 0.0}, None)
