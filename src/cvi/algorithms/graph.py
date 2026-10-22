"""Graph algorithms (Dijkstra, A*) and dynamic programming (0/1 knapsack for maintenance scheduling)."""

from __future__ import annotations

import heapq
from collections.abc import Callable

Graph = dict[str, list[tuple[str, float]]]


def dijkstra(graph: Graph, source: str, target: str | None = None) -> tuple[dict[str, float], dict[str, str]]:
    """Single-source shortest paths with a binary heap: O((V+E) log V) time, O(V) space."""
    dist = {source: 0.0}
    prev: dict[str, str] = {}
    heap = [(0.0, source)]
    done: set[str] = set()
    while heap:
        d, u = heapq.heappop(heap)
        if u in done:
            continue
        done.add(u)
        if u == target:
            break
        for v, w in graph.get(u, ()):
            nd = d + w
            if nd < dist.get(v, float("inf")):
                dist[v], prev[v] = nd, u
                heapq.heappush(heap, (nd, v))
    return dist, prev


def astar(graph: Graph, source: str, target: str, heuristic: Callable[[str], float]) -> tuple[float, list[str]]:
    """A* with an admissible heuristic; expands far fewer nodes than Dijkstra on geographic graphs."""
    g = {source: 0.0}
    prev: dict[str, str] = {}
    heap = [(heuristic(source), source)]
    closed: set[str] = set()
    while heap:
        _, u = heapq.heappop(heap)
        if u == target:
            return g[u], reconstruct(prev, source, target)
        if u in closed:
            continue
        closed.add(u)
        for v, w in graph.get(u, ()):
            ng = g[u] + w
            if ng < g.get(v, float("inf")):
                g[v], prev[v] = ng, u
                heapq.heappush(heap, (ng + heuristic(v), v))
    return float("inf"), []


def reconstruct(prev: dict[str, str], source: str, target: str) -> list[str]:
    if target != source and target not in prev:
        return []
    path = [target]
    while path[-1] != source:
        path.append(prev[path[-1]])
    return path[::-1]


def grid_road_graph(n: int, lat0: float, lon0: float, step_deg: float) -> tuple[Graph, dict[str, tuple[float, float]]]:
    """Synthetic city street grid (n x n intersections) with haversine edge weights in km."""
    from cvi.algorithms.geo import haversine_km

    coords = {f"{i}:{j}": (lat0 + i * step_deg, lon0 + j * step_deg) for i in range(n) for j in range(n)}
    g: Graph = {k: [] for k in coords}
    for i in range(n):
        for j in range(n):
            for di, dj in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                a, b = i + di, j + dj
                if 0 <= a < n and 0 <= b < n:
                    g[f"{i}:{j}"].append((f"{a}:{b}", haversine_km(*coords[f"{i}:{j}"], *coords[f"{a}:{b}"])))
    return g, coords


def knapsack_schedule(items: list[tuple[str, int, float]], capacity: int) -> tuple[float, list[str]]:
    """0/1 knapsack DP. items = (id, cost_hours, value=risk_reduction). O(n * capacity) time/space.

    Used to pick which vehicles to service this week given limited workshop hours.
    """
    n = len(items)
    dp = [[0.0] * (capacity + 1) for _ in range(n + 1)]
    for i, (_, cost, value) in enumerate(items, 1):
        for c in range(capacity + 1):
            dp[i][c] = dp[i - 1][c]
            if cost <= c and dp[i - 1][c - cost] + value > dp[i][c]:
                dp[i][c] = dp[i - 1][c - cost] + value
    chosen, c = [], capacity
    for i in range(n, 0, -1):
        if dp[i][c] != dp[i - 1][c]:
            chosen.append(items[i - 1][0])
            c -= items[i - 1][1]
    return dp[n][capacity], chosen[::-1]
