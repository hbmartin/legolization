"""Exact repeated voxel-component detection under translation and yaw."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

import numpy as np

from legolization.catalog import Cell, rotate_offset

if TYPE_CHECKING:
    from legolization.grid import VoxelGrid


@dataclass(frozen=True, slots=True, kw_only=True)
class ComponentInstance:
    """One disconnected component mapped into its canonical yaw frame."""

    origin: Cell
    canonical_yaw: int
    colour_mapping: tuple[tuple[int, int], ...]
    cells: frozenset[Cell]


@dataclass(frozen=True, slots=True, kw_only=True)
class RepeatedComponent:
    """A canonical component shared by at least two exact instances."""

    signature: str
    canonical_cells: tuple[tuple[int, int, int, int], ...]
    instances: tuple[ComponentInstance, ...]


@dataclass(frozen=True, slots=True)
class _Canonical:
    cells: tuple[tuple[int, int, int, int], ...]
    yaw: int
    origin: Cell
    colour_mapping: tuple[tuple[int, int], ...]


def repeated_components(grid: VoxelGrid) -> tuple[RepeatedComponent, ...]:
    """Group disconnected components equal up to yaw, translation, and colour."""
    groups: dict[str, list[tuple[_Canonical, frozenset[Cell]]]] = {}
    canonical_by_signature: dict[str, tuple[tuple[int, int, int, int], ...]] = {}
    for cells in _connected_components(grid):
        canonical = _canonicalize(grid, cells)
        encoded = json.dumps(canonical.cells, separators=(",", ":")).encode()
        signature = hashlib.sha256(encoded).hexdigest()
        canonical_by_signature[signature] = canonical.cells
        groups.setdefault(signature, []).append((canonical, cells))
    return tuple(
        RepeatedComponent(
            signature=signature,
            canonical_cells=canonical_by_signature[signature],
            instances=tuple(
                ComponentInstance(
                    origin=canonical.origin,
                    canonical_yaw=canonical.yaw,
                    colour_mapping=canonical.colour_mapping,
                    cells=cells,
                )
                for canonical, cells in instances
            ),
        )
        for signature, instances in sorted(groups.items())
        if len(instances) > 1
    )


def _connected_components(grid: VoxelGrid) -> tuple[frozenset[Cell], ...]:
    remaining = {
        cast("Cell", tuple(int(value) for value in row))
        for row in np.argwhere(grid.filled_mask)
    }
    components: list[frozenset[Cell]] = []
    while remaining:
        start = min(remaining)
        pending = [start]
        found: set[Cell] = set()
        remaining.remove(start)
        while pending:
            cell = pending.pop()
            found.add(cell)
            for neighbour in _neighbours(cell):
                if neighbour in remaining:
                    remaining.remove(neighbour)
                    pending.append(neighbour)
        components.append(frozenset(found))
    return tuple(components)


def _canonicalize(grid: VoxelGrid, cells: frozenset[Cell]) -> _Canonical:
    alternatives = tuple(_at_yaw(grid, cells, yaw=yaw) for yaw in (0, 90, 180, 270))
    return min(alternatives, key=lambda item: (item.cells, item.yaw))


def _at_yaw(grid: VoxelGrid, cells: frozenset[Cell], *, yaw: int) -> _Canonical:
    rotated = [(rotate_offset(cell, yaw), grid.code_at(*cell)) for cell in cells]
    minimum = tuple(min(cell[axis] for cell, _ in rotated) for axis in range(3))
    origin = cast("Cell", minimum)
    normalized = sorted(
        (
            cell[0] - origin[0],
            cell[1] - origin[1],
            cell[2] - origin[2],
            colour,
        )
        for cell, colour in rotated
    )
    colour_labels: dict[int, int] = {}
    canonical_cells: list[tuple[int, int, int, int]] = []
    for x, y, z, colour in normalized:
        label = colour_labels.setdefault(colour, len(colour_labels))
        canonical_cells.append((x, y, z, label))
    colour_mapping = tuple(
        sorted((label, colour) for colour, label in colour_labels.items())
    )
    return _Canonical(
        cells=tuple(canonical_cells),
        yaw=yaw,
        origin=origin,
        colour_mapping=colour_mapping,
    )


def _neighbours(cell: Cell) -> tuple[Cell, ...]:
    x, y, z = cell
    return (
        (x - 1, y, z),
        (x + 1, y, z),
        (x, y - 1, z),
        (x, y + 1, z),
        (x, y, z - 1),
        (x, y, z + 1),
    )
