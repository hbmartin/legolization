"""Step chunking: bands, spatial grouping, and mirror-pair detection.

A band is all bricks sharing a base layer (two same-band bricks can never
support or vertically block each other). Within a band, chunks grow by
spatial proximity so consecutive bricks sit near each other (the assembly
paper's spatial-continuity rule), and detected mirror partners are pulled
into the same step so symmetric halves are built together.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from legolization.instructions.sequencer import InstructionsConfig
    from legolization.layout import Layout, PlacedBrick


def mirror_pairs(layout: Layout) -> dict[int, int]:
    """Detect exact mirror symmetry about a bounding-box mid-plane.

    Returns a brick -> partner map (self-paired bricks map to themselves)
    for the first axis (x then y) under which *every* brick has a
    same-shape, same-colour partner; empty when the layout is asymmetric.
    """
    cells_of = {brick.brick_id: frozenset(layout.cells_of(brick)) for brick in layout}
    if not cells_of:
        return {}
    xs = [x for cells in cells_of.values() for x, _, _ in cells]
    ys = [y for cells in cells_of.values() for _, y, _ in cells]
    for axis, mirror_sum in ((0, min(xs) + max(xs)), (1, min(ys) + max(ys))):
        index = {
            (cells, layout.bricks[bid].part_key, layout.bricks[bid].colour_code): bid
            for bid, cells in cells_of.items()
        }
        pairs: dict[int, int] = {}
        for brick in layout:
            mirrored = frozenset(
                (mirror_sum - x, y, z) if axis == 0 else (x, mirror_sum - y, z)
                for x, y, z in cells_of[brick.brick_id]
            )
            partner = index.get((mirrored, brick.part_key, brick.colour_code))
            if partner is None:
                pairs = {}
                break
            pairs[brick.brick_id] = partner
        if pairs:
            return pairs
    return {}


def chunk_bands(
    layout: Layout,
    *,
    config: InstructionsConfig,
    pairs: dict[int, int],
) -> list[tuple[int, tuple[int, ...]]]:
    """Split each band into spatially coherent chunks of ~target size."""
    bands: dict[int, list[PlacedBrick]] = {}
    for brick in layout:
        bands.setdefault(brick.layer, []).append(brick)
    chunks: list[tuple[int, tuple[int, ...]]] = []
    for band_layer in sorted(bands):
        chunks.extend(
            (band_layer, chunk)
            for chunk in _chunk_band(layout, bands[band_layer], config, pairs)
        )
    return chunks


def _chunk_band(
    layout: Layout,
    bricks: list[PlacedBrick],
    config: InstructionsConfig,
    pairs: dict[int, int],
) -> list[tuple[int, ...]]:
    centroids = {brick.brick_id: _centroid(layout, brick) for brick in bricks}
    band_ids = {brick.brick_id for brick in bricks}
    unassigned = [
        brick.brick_id for brick in sorted(bricks, key=lambda b: (b.y, b.x, b.brick_id))
    ]
    chunks: list[list[int]] = []
    while unassigned:
        chunk = [unassigned.pop(0)]
        _pull_partner(
            chunk,
            unassigned,
            pairs,
            band_ids,
            max_size=config.max_step_size,
        )
        _grow_chunk(
            chunk,
            unassigned,
            centroids,
            pairs,
            band_ids,
            config,
        )
        chunks.append(chunk)
    _merge_undersized_tail(chunks, config)
    ordered = sorted(
        chunks,
        key=lambda chunk: (
            sum(centroids[bid][1] for bid in chunk) / len(chunk),
            sum(centroids[bid][0] for bid in chunk) / len(chunk),
        ),
    )
    return [tuple(chunk) for chunk in ordered]


def _grow_chunk(  # noqa: PLR0913 - explicit chunk-growth state
    chunk: list[int],
    unassigned: list[int],
    centroids: dict[int, tuple[float, float]],
    pairs: dict[int, int],
    band_ids: set[int],
    config: InstructionsConfig,
) -> None:
    """Grow one spatial chunk without splitting a mirror pair or overflowing."""
    while unassigned and len(chunk) < config.target_step_size:
        remaining = config.max_step_size - len(chunk)
        eligible = [
            brick_id
            for brick_id in unassigned
            if _addition_size(brick_id, unassigned, pairs, band_ids) <= remaining
        ]
        if not eligible:
            return
        cx = sum(centroids[brick_id][0] for brick_id in chunk) / len(chunk)
        cy = sum(centroids[brick_id][1] for brick_id in chunk) / len(chunk)
        nearest = min(
            eligible,
            key=lambda brick_id: (
                (centroids[brick_id][0] - cx) ** 2 + (centroids[brick_id][1] - cy) ** 2,
                brick_id,
            ),
        )
        chunk.append(nearest)
        unassigned.remove(nearest)
        _pull_partner(
            chunk,
            unassigned,
            pairs,
            band_ids,
            max_size=config.max_step_size,
        )


def _addition_size(
    brick_id: int,
    unassigned: list[int],
    pairs: dict[int, int],
    band_ids: set[int],
) -> int:
    partner = pairs.get(brick_id)
    return 2 if partner in band_ids and partner in unassigned else 1


def _merge_undersized_tail(chunks: list[list[int]], config: InstructionsConfig) -> None:
    if (
        len(chunks) > 1
        and len(chunks[-1]) < config.min_step_size
        and len(chunks[-2]) + len(chunks[-1]) <= config.max_step_size
    ):
        chunks[-2].extend(chunks.pop())


def _pull_partner(
    chunk: list[int],
    unassigned: list[int],
    pairs: dict[int, int],
    band_ids: set[int],
    *,
    max_size: int,
) -> None:
    """Keep mirror partners in the same step when they share the band."""
    for brick_id in list(chunk):
        partner = pairs.get(brick_id)
        if (
            len(chunk) < max_size
            and partner is not None
            and partner in band_ids
            and partner in unassigned
        ):
            chunk.append(partner)
            unassigned.remove(partner)


def _centroid(layout: Layout, brick: PlacedBrick) -> tuple[float, float]:
    columns = {(x, y) for x, y, _ in layout.cells_of(brick)}
    return (
        sum(x for x, _ in columns) / len(columns),
        sum(y for _, y in columns) / len(columns),
    )
