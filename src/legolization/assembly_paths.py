"""Region classification, selectors, load paths, and connector minimum cuts."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING

from pyldcad import ConnectionStatus

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyldcad import ConnectivityAnalysis

    from legolization.assembly_model import AssemblyModel, AssemblyOccurrence


@dataclass(frozen=True, slots=True)
class AssemblyRegion:
    """A meaningful occurrence subset with classification evidence."""

    name: str
    occurrence_ids: tuple[int, ...]
    evidence: str
    confidence: float


@dataclass(frozen=True, slots=True)
class RegionPath:
    """Confirmed and optimistic connector cuts between two regions."""

    left_selector: str
    right_selector: str
    left_occurrence_ids: tuple[int, ...]
    right_occurrence_ids: tuple[int, ...]
    verdict: str
    confirmed_minimum_cut: int
    optimistic_minimum_cut: int
    confirmed_path: tuple[int, ...]
    capacity_cut_n: float | None
    capacity_complete: bool


def classify_regions(model: AssemblyModel) -> tuple[AssemblyRegion, ...]:
    """Classify vehicle regions from tags first and descriptions second."""
    # The ordered rule table is intentionally explicit for evidence attribution.
    # lizard forgives(cyclomatic_complexity)
    buckets: dict[str, list[int]] = {
        "wheels": [],
        "axle-suspension": [],
        "chassis": [],
        "body": [],
    }
    evidence: dict[str, str] = {}
    for occurrence in model.occurrences:
        labels = _labels(occurrence)
        if labels & {"wheel", "tyre", "tire", "rim"}:
            buckets["wheels"].append(occurrence.occurrence_id)
            evidence["wheels"] = "registry tags or part description"
        elif labels & {"axle", "suspension", "steering", "shock"}:
            buckets["axle-suspension"].append(occurrence.occurrence_id)
            evidence["axle-suspension"] = "registry tags or part description"
        elif labels & {"chassis", "frame", "floor"}:
            buckets["chassis"].append(occurrence.occurrence_id)
            evidence["chassis"] = "registry tags or part description"
        else:
            buckets["body"].append(occurrence.occurrence_id)
            evidence["body"] = "complement of classified running gear"
    if buckets["wheels"] and not buckets["chassis"] and buckets["body"]:
        by_id = {item.occurrence_id: item for item in model.occurrences}
        body_centers = sorted(
            by_id[item].bounds_ldu.center.y for item in buckets["body"]
        )
        threshold = body_centers[max(0, int(len(body_centers) * 0.65) - 1)]
        inferred_chassis = [
            item
            for item in buckets["body"]
            if by_id[item].bounds_ldu.center.y >= threshold
            and _labels(by_id[item])
            & {"beam", "brick", "frame", "plate", "technic", "floor"}
        ]
        if inferred_chassis:
            chassis_ids = set(inferred_chassis)
            buckets["chassis"] = inferred_chassis
            buckets["body"] = [
                item for item in buckets["body"] if item not in chassis_ids
            ]
            evidence["chassis"] = "lower structural-body geometry heuristic"
    return tuple(
        AssemblyRegion(
            name=name,
            occurrence_ids=tuple(ids),
            evidence=evidence.get(name, "no matching occurrences"),
            confidence=(
                0.55
                if evidence.get(name) == "lower structural-body geometry heuristic"
                else 0.9
                if name in evidence and name != "body"
                else 0.6
            ),
        )
        for name, ids in buckets.items()
        if ids
    )


def select_occurrences(selector: str, model: AssemblyModel) -> tuple[int, ...]:
    """Resolve ``pages:`` and ``occurrences:`` region selectors."""
    prefix, separator, value = selector.partition(":")
    if not separator or not value:
        msg = f"invalid region selector {selector!r}"
        raise ValueError(msg)
    match prefix.casefold():
        case "occurrences":
            selected = _parse_ranges(value)
            maximum = model.occurrence_count
            if any(item < 1 or item > maximum for item in selected):
                msg = f"occurrence selector is outside 1..{maximum}"
                raise ValueError(msg)
            return tuple(sorted(selected))
        case "pages":
            pages = _parse_ranges(value)
            return tuple(
                item.occurrence_id
                for item in model.occurrences
                if any(
                    page in pages for page in item.source.page_path if page is not None
                )
            )
        case _:
            msg = f"unsupported region selector {prefix!r}"
            raise ValueError(msg)


def analyze_region_path(
    left_selector: str,
    right_selector: str,
    *,
    model: AssemblyModel,
    connectivity: ConnectivityAnalysis,
) -> RegionPath:
    """Compute confirmed/optimistic connector-instance cuts and one path."""
    # Confirmed/optimistic graph cases share one selector-validation boundary.
    # lizard forgives(cyclomatic_complexity)
    left = select_occurrences(left_selector, model)
    right = select_occurrences(right_selector, model)
    if not left or not right:
        msg = "region selectors must each resolve to at least one occurrence"
        raise ValueError(msg)
    confirmed_edges = [
        edge.occurrence_ids
        for edge in connectivity.connections
        if edge.status is ConnectionStatus.CONFIRMED
    ]
    optimistic_edges = [edge.occurrence_ids for edge in connectivity.connections]
    confirmed_cut, confirmed_cut_edges = _minimum_cut_details(
        connectivity.occurrence_count,
        confirmed_edges,
        left,
        right,
    )
    optimistic_cut, _ = _minimum_cut_details(
        connectivity.occurrence_count,
        optimistic_edges,
        left,
        right,
    )
    path = _shortest_path(confirmed_edges, left, right)
    if confirmed_cut > 0:
        verdict = "connected"
    elif optimistic_cut == 0:
        verdict = "definitely_disconnected"
    else:
        verdict = "possibly_connected"
    capacity_values = [
        edge.capacity.shear_n
        for edge in connectivity.connections
        if edge.status is ConnectionStatus.CONFIRMED
        and edge.occurrence_ids in set(confirmed_cut_edges)
    ]
    capacity_complete = bool(capacity_values) and all(
        value is not None for value in capacity_values
    )
    return RegionPath(
        left_selector=left_selector,
        right_selector=right_selector,
        left_occurrence_ids=left,
        right_occurrence_ids=right,
        verdict=verdict,
        confirmed_minimum_cut=confirmed_cut,
        optimistic_minimum_cut=optimistic_cut,
        confirmed_path=path,
        capacity_cut_n=(
            sum(value for value in capacity_values if value is not None)
            if capacity_complete
            else None
        ),
        capacity_complete=capacity_complete,
    )


def reachable_occurrences(
    edges: Iterable[tuple[int, int]],
    seeds: Iterable[int],
) -> frozenset[int]:
    """Return all nodes reachable through an undirected edge collection."""
    adjacency: dict[int, set[int]] = {}
    for left, right in edges:
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
    found = set(seeds)
    queue = deque(found)
    while queue:
        current = queue.popleft()
        for neighbour in adjacency.get(current, ()):
            if neighbour not in found:
                found.add(neighbour)
                queue.append(neighbour)
    return frozenset(found)


def _labels(occurrence: AssemblyOccurrence) -> set[str]:
    description = occurrence.geometry.description.casefold().replace("-", " ")
    return set(occurrence.tags) | set(description.split())


def _parse_ranges(value: str) -> frozenset[int]:
    selected: set[int] = set()
    for raw_field in value.split(","):
        field = raw_field.strip()
        if not field:
            continue
        if "-" in field:
            first_text, last_text = field.split("-", maxsplit=1)
            first, last = int(first_text), int(last_text)
            if first > last:
                msg = f"invalid descending range {field!r}"
                raise ValueError(msg)
            selected.update(range(first, last + 1))
        else:
            selected.add(int(field))
    if not selected:
        msg = "region selector is empty"
        raise ValueError(msg)
    return frozenset(selected)


def _shortest_path(
    edges: Iterable[tuple[int, int]],
    sources: Iterable[int],
    targets: Iterable[int],
) -> tuple[int, ...]:
    adjacency: dict[int, set[int]] = {}
    for left, right in edges:
        adjacency.setdefault(left, set()).add(right)
        adjacency.setdefault(right, set()).add(left)
    target_set = set(targets)
    parents: dict[int, int | None] = dict.fromkeys(sources)
    queue = deque(sorted(parents))
    found: int | None = None
    while queue and found is None:
        current = queue.popleft()
        if current in target_set:
            found = current
            break
        for neighbour in sorted(adjacency.get(current, ())):
            if neighbour not in parents:
                parents[neighbour] = current
                queue.append(neighbour)
    if found is None:
        return ()
    path: list[int] = []
    cursor: int | None = found
    while cursor is not None:
        path.append(cursor)
        cursor = parents[cursor]
    return tuple(reversed(path))


def _minimum_cut(
    node_count: int,
    edges: Iterable[tuple[int, int]],
    sources: Iterable[int],
    targets: Iterable[int],
) -> int:
    """Return the connector-instance minimum cut between two node sets."""
    return _minimum_cut_details(node_count, edges, sources, targets)[0]


def _minimum_cut_details(
    node_count: int,
    edges: Iterable[tuple[int, int]],
    sources: Iterable[int],
    targets: Iterable[int],
) -> tuple[int, tuple[tuple[int, int], ...]]:
    edge_list = list(edges)
    if not _shortest_path(edge_list, sources, targets):
        return 0, ()
    # Unit-capacity Edmonds-Karp over connector instances. Parallel edges are
    # accumulated, so the result counts connectors rather than part pairs.
    source = 0
    sink = node_count + 1
    capacity: dict[tuple[int, int], int] = {}
    for left, right in edge_list:
        capacity[(left, right)] = capacity.get((left, right), 0) + 1
        capacity[(right, left)] = capacity.get((right, left), 0) + 1
    infinite = len(edge_list) + 1
    for item in sources:
        capacity[(source, item)] = infinite
    for item in targets:
        capacity[(item, sink)] = infinite
    flow: dict[tuple[int, int], int] = {}
    total = 0
    while path := _augmenting_path(capacity, flow, source, sink):
        amount = min(
            capacity.get((left, right), 0) - flow.get((left, right), 0)
            for left, right in pairwise(path)
        )
        total += amount
        for left, right in pairwise(path):
            flow[(left, right)] = flow.get((left, right), 0) + amount
            flow[(right, left)] = flow.get((right, left), 0) - amount
    residual_reachable = _residual_reachable(capacity, flow, source)
    cut_edges = tuple(
        edge
        for edge in edge_list
        if (edge[0] in residual_reachable) != (edge[1] in residual_reachable)
    )
    return total, cut_edges


def _augmenting_path(
    capacity: dict[tuple[int, int], int],
    flow: dict[tuple[int, int], int],
    source: int,
    sink: int,
) -> tuple[int, ...]:
    neighbours: dict[int, set[int]] = {}
    for left, right in capacity:
        neighbours.setdefault(left, set()).add(right)
    parents: dict[int, int | None] = {source: None}
    queue = deque([source])
    while queue and sink not in parents:
        left = queue.popleft()
        for right in sorted(neighbours.get(left, ())):
            residual = capacity.get((left, right), 0) - flow.get((left, right), 0)
            if residual > 0 and right not in parents:
                parents[right] = left
                queue.append(right)
    if sink not in parents:
        return ()
    path: list[int] = []
    cursor: int | None = sink
    while cursor is not None:
        path.append(cursor)
        cursor = parents[cursor]
    return tuple(reversed(path))


def _residual_reachable(
    capacity: dict[tuple[int, int], int],
    flow: dict[tuple[int, int], int],
    source: int,
) -> frozenset[int]:
    neighbours: dict[int, set[int]] = {}
    for left, right in capacity:
        neighbours.setdefault(left, set()).add(right)
    found = {source}
    queue = deque([source])
    while queue:
        left = queue.popleft()
        for right in neighbours.get(left, ()):
            residual = capacity.get((left, right), 0) - flow.get((left, right), 0)
            if residual > 0 and right not in found:
                found.add(right)
                queue.append(right)
    return frozenset(found)
