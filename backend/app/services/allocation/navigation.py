"""Wayfinding from a gate to an allocated slot.

The layout builder lets an owner draw drivable aisles as polylines. Those
segments form a graph; the driver's route is the shortest path through it,
entered and left at the perpendicular projection of the gate and the slot onto
their nearest segment. When a facility has no aisles drawn we fall back to an
L-shaped route, which is what a driver would do anyway in an open lot.

Output is both machine-readable waypoints (the React canvas animates them) and
turn-by-turn text (the voice agent reads it aloud).
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field
from itertools import pairwise

Point = tuple[float, float]


@dataclass(slots=True)
class Route:
    waypoints: list[Point] = field(default_factory=list)
    distance_m: float = 0.0
    instructions: list[str] = field(default_factory=list)
    level_id: int | None = None

    def as_dict(self) -> dict:
        return {
            "waypoints": [[round(x, 1), round(y, 1)] for x, y in self.waypoints],
            "distance_m": round(self.distance_m, 1),
            "instructions": self.instructions,
            "level_id": self.level_id,
        }


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _project_on_segment(p: Point, a: Point, b: Point) -> tuple[Point, float]:
    """Closest point to `p` on segment a→b, plus the distance to it."""
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    seg_len_sq = dx * dx + dy * dy
    if seg_len_sq < 1e-9:
        return a, _dist(p, a)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / seg_len_sq))
    proj = (ax + t * dx, ay + t * dy)
    return proj, _dist(p, proj)


class AisleGraph:
    """Undirected weighted graph built from the level's aisle polylines."""

    def __init__(self, aisles: list[list[list[float]]]):
        self.nodes: list[Point] = []
        self.adj: dict[int, list[tuple[int, float]]] = {}
        self._index: dict[Point, int] = {}
        self.segments: list[tuple[Point, Point]] = []

        for polyline in aisles or []:
            points = [(float(p[0]), float(p[1])) for p in polyline if len(p) >= 2]
            for a, b in pairwise(points):
                self._add_segment(a, b)

    def _node(self, p: Point) -> int:
        key = (round(p[0], 2), round(p[1], 2))
        if key not in self._index:
            self._index[key] = len(self.nodes)
            self.nodes.append(key)
            self.adj[self._index[key]] = []
        return self._index[key]

    def _add_segment(self, a: Point, b: Point) -> None:
        ia, ib = self._node(a), self._node(b)
        if ia == ib:
            return
        w = _dist(self.nodes[ia], self.nodes[ib])
        self.adj[ia].append((ib, w))
        self.adj[ib].append((ia, w))
        self.segments.append((self.nodes[ia], self.nodes[ib]))

    @property
    def is_empty(self) -> bool:
        return not self.segments

    def attach(self, p: Point) -> int:
        """Splice an off-graph point onto its nearest aisle segment."""
        best_seg, best_proj, best_d = None, None, float("inf")
        for a, b in self.segments:
            proj, d = _project_on_segment(p, a, b)
            if d < best_d:
                best_seg, best_proj, best_d = (a, b), proj, d
        if best_seg is None or best_proj is None:
            return self._node(p)

        node = self._node(p)
        proj_node = self._node(best_proj)
        # Connect the point to its projection, and the projection into the aisle.
        self.adj[node].append((proj_node, best_d))
        self.adj[proj_node].append((node, best_d))
        for endpoint in best_seg:
            end_node = self._node(endpoint)
            w = _dist(best_proj, endpoint)
            self.adj[proj_node].append((end_node, w))
            self.adj[end_node].append((proj_node, w))
        return node

    def shortest_path(self, start: int, goal: int) -> tuple[list[Point], float]:
        dist = {start: 0.0}
        prev: dict[int, int] = {}
        heap: list[tuple[float, int]] = [(0.0, start)]
        visited: set[int] = set()

        while heap:
            d, node = heapq.heappop(heap)
            if node in visited:
                continue
            visited.add(node)
            if node == goal:
                break
            for neighbour, w in self.adj.get(node, ()):
                nd = d + w
                if nd < dist.get(neighbour, float("inf")):
                    dist[neighbour] = nd
                    prev[neighbour] = node
                    heapq.heappush(heap, (nd, neighbour))

        if goal not in dist:
            return [], float("inf")

        path, cursor = [], goal
        while cursor != start:
            path.append(self.nodes[cursor])
            cursor = prev[cursor]
        path.append(self.nodes[start])
        path.reverse()
        return path, dist[goal]


def _bearing(a: Point, b: Point) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def _turn_word(previous: float, current: float) -> str:
    delta = (current - previous + 180) % 360 - 180
    if delta < -110:
        return "make a U-turn"
    if delta < -25:
        return "turn left"
    if delta > 110:
        return "make a U-turn"
    if delta > 25:
        return "turn right"
    return "continue straight"


def build_instructions(waypoints: list[Point], slot_code: str, zone: str) -> list[str]:
    """Human-readable directions; the voice agent speaks these verbatim."""
    if len(waypoints) < 2:
        return [f"Park in slot {slot_code}, zone {zone}."]

    steps = ["Enter the facility and follow the marked aisle."]
    previous_bearing = _bearing(waypoints[0], waypoints[1])
    run = _dist(waypoints[0], waypoints[1])

    for a, b in pairwise(waypoints[1:]):
        bearing = _bearing(a, b)
        turn = _turn_word(previous_bearing, bearing)
        if turn == "continue straight":
            run += _dist(a, b)
            continue
        steps.append(f"Drive {run:.0f} metres, then {turn}.")
        run = _dist(a, b)
        previous_bearing = bearing

    steps.append(f"Drive {run:.0f} metres — slot {slot_code} is in zone {zone}, on your right.")
    return steps


def route_to_slot(
    gate: Point,
    slot: Point,
    aisles: list | None = None,
    *,
    slot_code: str = "",
    zone: str = "",
    level_id: int | None = None,
) -> Route:
    graph = AisleGraph(aisles or [])

    if graph.is_empty:
        # No aisle map: route down the lot then across, the way a driver would.
        corner = (gate[0], slot[1])
        waypoints = [gate, corner, slot]
        distance = _dist(gate, corner) + _dist(corner, slot)
    else:
        start = graph.attach(gate)
        goal = graph.attach(slot)
        waypoints, distance = graph.shortest_path(start, goal)
        if not waypoints or distance == float("inf"):
            corner = (gate[0], slot[1])
            waypoints = [gate, corner, slot]
            distance = _dist(gate, corner) + _dist(corner, slot)

    # Collapse collinear points so the instruction list reads naturally.
    cleaned: list[Point] = []
    for p in waypoints:
        if len(cleaned) >= 2:
            a, b = cleaned[-2], cleaned[-1]
            if abs(_bearing(a, b) - _bearing(b, p)) < 3:
                cleaned[-1] = p
                continue
        cleaned.append(p)

    return Route(
        waypoints=cleaned,
        distance_m=distance,
        instructions=build_instructions(cleaned, slot_code, zone),
        level_id=level_id,
    )
