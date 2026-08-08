"""Loader for the StableText2Brick release (Pun et al., ICCV 2025).

The release (huggingface.co/datasets/AvaLovelace/StableText2Lego, MIT) is seven
parquet shards, 47,389 rows. Each row is one brick structure on a 20x20x20 grid:

- ``bricks`` - one line per brick, ``"{h}x{w} ({x},{y},{z})"``. Verified against
  the data: the brick occupies ``x .. x+h-1`` by ``y .. y+w-1`` at brick-height
  ``z``, so ``h`` is the x extent and ``w`` the y extent, matching
  :mod:`legolization.stablelego`'s height/width. Every brick is one brick tall.
- ``stability_scores`` - a dense 20x20x20 array.
- ``captions`` - five geometric descriptions, ``object_id`` - the source
  ShapeNetCore object, ``category_id`` - one of 21 categories.

**The score convention is the inverse of StableLego's, and that inversion is
load-bearing.** Measured over the test shard: unoccupied voxels are *exactly*
1.0 (all 355,660 of them across 50 structures), occupied voxels lie in
[0.7176, 1.0]. So this is a stability *margin* - higher is better, 1.0 is
effortless - and the paper's rule is "a structure is stable if all bricks have
stability scores greater than 0". Our ``max_score`` and StableLego's
``stability_score.npy`` are *stress*: higher is worse, >= 1.0 collapses. Read
:func:`release_margin` for the worst occupied margin; never compare the two
scales without saying which way each points.

Every row in the release passed Liu et al.'s analysis during construction (the
paper generated 62,000+ structures and kept the ~47,000 stable ones), so the
whole set is a positive-only regression: it bounds false negatives, not false
positives. StableLego's mixed release covers the other direction.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

import numpy as np

from legolization.catalog import default_catalog
from legolization.layout import Layout

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path

    from legolization.catalog import Catalog, Part

GRID = 20
_COLOUR = 4
_PLATES_PER_BRICK = 3

_BRICK = re.compile(
    r"^\s*(\d+)\s*x\s*(\d+)\s*\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\)\s*$"
)


class BrickParseError(ValueError):
    """A ``bricks`` payload contained a line this reader cannot decode."""


@dataclass(frozen=True, slots=True)
class BrickRow:
    """One parsed brick: an ``x_extent`` by ``y_extent`` footprint at ``(x, y, z)``."""

    x_extent: int
    y_extent: int
    x: int
    y: int
    z: int

    @property
    def cells(self) -> Iterator[tuple[int, int, int]]:
        """Yield every grid cell this brick occupies."""
        for dx in range(self.x_extent):
            for dy in range(self.y_extent):
                yield self.x + dx, self.y + dy, self.z


@dataclass(frozen=True, slots=True, kw_only=True)
class Structure:
    """One release row, with its bricks parsed and its scores as an array."""

    structure_id: str
    object_id: str
    category_id: str
    captions: tuple[str, ...]
    bricks: tuple[BrickRow, ...]
    scores: np.ndarray

    @classmethod
    def from_row(cls, row: Mapping[str, object]) -> Self:
        """Build a structure from one parquet row, parsing its brick text."""
        return cls(
            structure_id=str(row["structure_id"]),
            object_id=str(row["object_id"]),
            category_id=str(row["category_id"]),
            captions=tuple(str(item) for item in row["captions"]),  # ty: ignore[not-iterable]
            bricks=parse_bricks(str(row["bricks"])),
            scores=np.asarray(row["stability_scores"], dtype=float),
        )

    @property
    def occupancy(self) -> np.ndarray:
        """Boolean 20x20x20 mask of the cells this structure's bricks fill."""
        mask = np.zeros((GRID, GRID, GRID), dtype=bool)
        for brick in self.bricks:
            mask[
                brick.x : brick.x + brick.x_extent,
                brick.y : brick.y + brick.y_extent,
                brick.z,
            ] = True
        return mask


def parse_bricks(text: str) -> tuple[BrickRow, ...]:
    """Parse a ``bricks`` payload, rejecting any line that does not decode.

    Raises :class:`BrickParseError` rather than skipping: a silently dropped
    brick would change the structure being analysed, which is worse than a
    skipped row.
    """
    rows: list[BrickRow] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not (stripped := line.strip()):
            continue
        if (match := _BRICK.match(stripped)) is None:
            msg = f"line {number}: cannot parse brick {stripped!r}"
            raise BrickParseError(msg)
        x_extent, y_extent, x, y, z = (int(value) for value in match.groups())
        rows.append(BrickRow(x_extent=x_extent, y_extent=y_extent, x=x, y=y, z=z))
    if not rows:
        msg = "structure has no bricks"
        raise BrickParseError(msg)
    return tuple(rows)


def _extents(part: Part) -> tuple[int, int]:
    """Native (x, y) stud extents of a catalog part."""
    xs = [dx for dx, _ in part.footprint]
    ys = [dy for _, dy in part.footprint]
    return max(xs) - min(xs) + 1, max(ys) - min(ys) + 1


def layout_from_bricks(
    bricks: Sequence[BrickRow],
    *,
    catalog: Catalog | None = None,
) -> Layout:
    """Build a :class:`Layout` from parsed release bricks.

    The release's fourteen oriented footprints (1x1 through 2x6 and their
    transposes) are all covered by the default catalog, so no custom library is
    needed - unlike StableLego, whose 200 g payload block needs an injected
    part. Raises ``ValueError`` for a footprint the catalog cannot supply.
    """
    catalog = catalog or default_catalog()
    layout = Layout(catalog=catalog)
    for index, brick in enumerate(bricks):
        key = catalog.rect_key(brick.x_extent, brick.y_extent, _PLATES_PER_BRICK)
        if key is None:
            msg = (
                f"brick {index}: no catalog part for {brick.x_extent}x{brick.y_extent}"
            )
            raise ValueError(msg)
        layer = _PLATES_PER_BRICK * brick.z
        if _extents(catalog[key]) == (brick.x_extent, brick.y_extent):
            layout.add(key, brick.x, brick.y, layer, 0, _COLOUR)
        else:
            # Yaw 90 rotates (dx, dy) to (-dy, dx): anchor at the max-x cell,
            # exactly as legolization.stablelego.layout_from_task_graph does.
            layout.add(key, brick.x + brick.x_extent - 1, brick.y, layer, 90, _COLOUR)
    return layout


def release_margin(structure: Structure) -> float:
    """Worst per-voxel stability margin over the cells this structure occupies.

    Unoccupied voxels are padded with 1.0 in the release, so a naive ``min()``
    over the whole array would be meaningless for a sparse structure - it would
    read 1.0 whenever the structure happens to be dense enough. Masking to the
    occupied cells is the only reading that means anything.

    Higher is better. The release's own stability rule is ``> 0``.
    """
    occupied = structure.scores[structure.occupancy]
    return float(occupied.min()) if occupied.size else float("nan")


def release_verdict(structure: Structure) -> tuple[bool, float]:
    """Return ``(stands, worst_margin)`` under the release's own convention."""
    margin = release_margin(structure)
    return bool(np.isfinite(margin) and margin > 0.0), margin


def shard_paths(root: Path) -> list[Path]:
    """Return the release's parquet shards under ``root``, sorted."""
    return sorted((root / "data").glob("*.parquet")) or sorted(root.glob("*.parquet"))


def iter_structures(paths: Sequence[Path]) -> Iterator[Structure | str]:
    """Stream structures from parquet shards, yielding a reason string on failure.

    Yielding failures rather than raising keeps a 47k-row sweep alive through a
    malformed row, matching ``scripts/stablelego_sweep.py``'s skip convention:
    record what could not be read, never guess at it.
    """
    import pyarrow.parquet as pq  # noqa: PLC0415 - dev-only dependency, import at use

    for path in paths:
        table = pq.read_table(path)
        for index, row in enumerate(table.to_pylist()):
            try:
                yield Structure.from_row(row)
            except (BrickParseError, KeyError, TypeError, ValueError) as error:
                yield f"{path.name}:{index}: {error}"
