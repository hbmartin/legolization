"""Loaders for the StableLego release format (Liu et al., RA-L 2024).

The release (github.com/intelligent-control-lab/StableLego, MIT) describes
an assembly as ``{step: {x, y, z, ori, brick_id}}`` where ``brick_id``
resolves through ``lego_library.json`` to a ``height x width`` stud
footprint (``ori`` swaps the axes) and a mass in kilograms; ``z`` counts
brick heights with the lowest layer resting on the baseplate. The full
dataset ships one directory per object holding ``task_graph.json`` and a
per-brick ``stability_score.npy``.

Two consumers share these loaders: the vendored-fixture cross-validation
(``tests/test_stablelego_cross.py``) and the dataset sweep
(``scripts/stablelego_sweep.py``).
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from legolization.catalog import Catalog, default_catalog
from legolization.layout import Layout

if TYPE_CHECKING:
    from pathlib import Path

    from legolization.catalog import Part

_COLOUR = 4
_PLATES_PER_BRICK = 3
_MASS_TOLERANCE_G = 1e-6

Library = dict[str, dict[str, float]]
TaskGraph = dict[str, dict[str, int]]


def load_library(path: Path) -> Library:
    """Read a StableLego ``lego_library.json`` (masses in kilograms)."""
    return json.loads(path.read_text())


def load_task_graph(path: Path) -> TaskGraph:
    """Read one assembly's ``task_graph.json`` step table."""
    return json.loads(path.read_text())


def _extents(part: Part) -> tuple[int, int]:
    xs = [dx for dx, _ in part.footprint]
    ys = [dy for _, dy in part.footprint]
    return max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


def _custom_key(brick_id: str) -> str:
    return f"stablelego_{brick_id}"


def stablelego_catalog(library: Library, *, base: Catalog | None = None) -> Catalog:
    """Extend the default catalog with custom-mass parts for the release.

    Library entries whose mass differs from the resolved rect part (the
    release's 200 g payload block is one) get a dedicated part carrying
    the release mass, so fixture and dataset totals reproduce exactly.
    """
    base = base or default_catalog()
    parts = dict(base.parts)
    for brick_id, spec in library.items():
        key = base.rect_key(int(spec["height"]), int(spec["width"]), _PLATES_PER_BRICK)
        if key is None:
            continue
        mass_g = float(spec["mass"]) * 1000.0
        if abs(base[key].mass_g - mass_g) > _MASS_TOLERANCE_G:
            parts[_custom_key(brick_id)] = replace(
                base[key],
                key=_custom_key(brick_id),
                mass_g=mass_g,
            )
    return Catalog(parts=parts)


def layout_from_task_graph(
    entries: TaskGraph,
    *,
    catalog: Catalog,
    library: Library,
) -> Layout:
    """Build a :class:`Layout` from one release-format step table.

    Raises ``KeyError`` for a brick id missing from the library and
    ``ValueError`` for a footprint the catalog cannot supply.
    """
    layout = Layout(catalog=catalog)
    for step, entry in entries.items():
        spec = library[str(entry["brick_id"])]
        x_extent, y_extent = int(spec["height"]), int(spec["width"])
        if entry["ori"]:
            x_extent, y_extent = y_extent, x_extent
        key = _custom_key(str(entry["brick_id"]))
        if key not in catalog.parts:
            key = catalog.rect_key(x_extent, y_extent, _PLATES_PER_BRICK)
        if key is None:
            msg = f"step {step}: no catalog part for {x_extent}x{y_extent}"
            raise ValueError(msg)
        layer = _PLATES_PER_BRICK * int(entry["z"])
        if _extents(catalog[key]) == (x_extent, y_extent):
            layout.add(key, entry["x"], entry["y"], layer, 0, _COLOUR)
        else:
            # Yaw 90 rotates (dx, dy) to (-dy, dx): anchor at the max-x cell.
            layout.add(key, entry["x"] + x_extent - 1, entry["y"], layer, 90, _COLOUR)
    return layout
