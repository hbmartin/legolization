"""Placement invariants: exact cover, colour fidelity, connectivity."""

import numpy as np
import pytest

from legolization.catalog import default_catalog
from legolization.graph import ConnectionGraph
from legolization.grid import EMPTY, VoxelGrid
from legolization.layout import Layout
from legolization.placement.base import _seam_alignment, evaluate
from legolization.placement.greedy import (
    GreedyStrategy,
    _grid_component_count,
    _h_lookahead,
)
from legolization.placement.luo import LuoStrategy
from legolization.placement.merge import (
    atomize,
    compact_columns,
    final_remerge,
    improve_connectivity,
    maximal_random_merge,
    merged_rect,
    place_rect,
    resolve_ignore_colours,
)
from legolization.placement.slopes import apply_slopes, apply_tiles


def _pyramid_grid() -> VoxelGrid:
    codes = np.full((6, 6, 3), EMPTY, dtype=np.int16)
    for z in range(3):
        lo, hi = z, 6 - z
        codes[lo:hi, lo:hi, z] = 4 if z % 2 == 0 else 14
    return VoxelGrid.from_array(codes, plates_per_voxel=3)


def _random_grid(seed: int) -> VoxelGrid:
    rng = np.random.default_rng(seed)
    codes = np.full((5, 5, 6), EMPTY, dtype=np.int16)
    codes[:, :, 0] = 4  # solid grounded base
    mask = rng.random((5, 5, 5)) < 0.6
    for z in range(1, 6):
        column_ok = mask[:, :, z - 1] & (codes[:, :, z - 1] != EMPTY)
        codes[:, :, z][column_ok] = rng.choice([4, 14, 15])
    return VoxelGrid(codes=codes)


def _assert_exact_cover(layout: Layout, grid: VoxelGrid) -> None:
    covered: dict[tuple[int, int, int], int] = {}
    for brick in layout:
        for cell in layout.filled_cells_of(brick):
            assert cell not in covered, f"cell {cell} covered twice"
            covered[cell] = brick.colour_code
    filled = {
        (int(x), int(y), int(z))
        for x, y, z in zip(*grid.filled_mask.nonzero(), strict=True)
    }
    assert set(covered) == filled
    for cell, colour in covered.items():
        assert colour == grid.code_at(*cell), f"colour mismatch at {cell}"


def _all_strategy_names() -> tuple[str, ...]:
    from legolization.placement.registry import strategy_names

    return strategy_names()


@pytest.mark.parametrize("name", _all_strategy_names())
def test_strategies_cover_exactly(name):
    from legolization.pipeline import PipelineConfig
    from legolization.placement.registry import make_strategy

    grid = _pyramid_grid()
    strategy = make_strategy(
        name, catalog=default_catalog(), config=PipelineConfig(strategy=name)
    )
    layout = strategy.place(grid, rng=np.random.default_rng(7))
    _assert_exact_cover(layout, grid)
    graph = ConnectionGraph.from_layout(layout)
    assert graph.component_count() == 1
    assert not graph.floating_ids()


@pytest.mark.parametrize("name", _all_strategy_names())
def test_strategies_are_seed_deterministic(name):
    from legolization.pipeline import PipelineConfig
    from legolization.placement.registry import make_strategy

    grid = _pyramid_grid()
    config = PipelineConfig(strategy=name, ga_generations=10)

    def snapshot() -> list[tuple[str, int, int, int, int, int]]:
        strategy = make_strategy(name, catalog=default_catalog(), config=config)
        layout = strategy.place(grid, rng=np.random.default_rng(3))
        return sorted(
            (b.part_key, b.x, b.y, b.layer, b.yaw, b.colour_code) for b in layout
        )

    assert snapshot() == snapshot()


def test_make_strategy_rejects_unknown_name():
    from legolization.pipeline import PipelineConfig
    from legolization.placement.registry import make_strategy

    with pytest.raises(ValueError, match="unknown strategy"):
        make_strategy("nope", catalog=default_catalog(), config=PipelineConfig())


@pytest.mark.parametrize("seed", [1, 2])
def test_strategies_on_random_grid(seed):
    grid = _random_grid(seed)
    layout = GreedyStrategy().place(grid, rng=np.random.default_rng(seed))
    _assert_exact_cover(layout, grid)


def test_atomize_slab_alignment():
    codes = np.full((1, 1, 7), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    layout = atomize(grid, default_catalog())
    keys = sorted(b.part_key for b in layout)
    # 7 plates = 2 aligned brick slabs + 1 leftover plate.
    assert keys == ["brick_1x1", "brick_1x1", "plate_1x1"]
    _assert_exact_cover(layout, grid)


def test_maximal_merge_reduces_bricks():
    grid = _pyramid_grid()
    layout = atomize(grid, default_catalog())
    atoms = len(layout)
    maximal_random_merge(layout, np.random.default_rng(3))
    assert len(layout) < atoms
    _assert_exact_cover(layout, grid)


def test_merged_rect_rejects_colour_mismatch():
    layout = Layout(catalog=default_catalog())
    a = layout.add("brick_1x1", 0, 0, 0, 0, 4)
    b = layout.add("brick_1x1", 1, 0, 0, 0, 14)
    assert merged_rect(layout, layout.bricks[a.brick_id], b) is None


def test_place_rect_yaw90_anchor():
    layout = Layout(catalog=default_catalog())
    brick = place_rect(layout, 0, 0, 1, 3, 0, 3, 4)
    assert brick.part_key == "brick_2x4"
    assert brick.yaw == 90
    columns = {(x, y) for x, y, _ in layout.cells_of(brick)}
    assert columns == {(x, y) for x in range(2) for y in range(4)}


def test_apply_slopes_on_step():
    codes = np.full((1, 3, 3), EMPTY, dtype=np.int16)
    codes[0, 0, :] = 4  # one full-height column at y=0
    grid = VoxelGrid(codes=codes)
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    replaced = apply_slopes(layout, grid)
    assert replaced == 1
    (brick,) = list(layout)
    assert brick.part_key == "slope_45_2x1"
    # The stud column stays where the 1x1 brick was.
    assert (0, 0, 0) in layout.filled_cells_of(brick)


def test_apply_tiles_caps_top_plates():
    layout = Layout(catalog=default_catalog())
    layout.add("plate_2x2", 0, 0, 0, 0, 4)
    swapped = apply_tiles(layout)
    assert swapped == 1
    (brick,) = list(layout)
    assert brick.part_key == "tile_2x2"


def test_apply_tiles_skips_supporting_plates():
    layout = Layout(catalog=default_catalog())
    layout.add("plate_2x2", 0, 0, 0, 0, 4)
    layout.add("plate_1x1", 0, 0, 1, 0, 4)
    assert apply_tiles(layout) == 1  # only the top 1x1 becomes a tile
    keys = sorted(b.part_key for b in layout)
    assert keys == ["plate_2x2", "tile_1x1"]


def test_greedy_bridges_straight_seams():
    # A 7-wide, two-brick-tall slab: the longest brick is 6, so largest-first
    # fill repeats a straight vertical seam on both layers and strands the
    # 7th column as its own tower. Reinforcement must remerge with staggered
    # seams into one component.
    codes = np.full((7, 2, 9), EMPTY, dtype=np.int16)
    codes[3, :, :3] = 4  # narrow grounded stem
    codes[:, :, 3:] = 4  # 7-wide slab on top, two brick layers
    grid = VoxelGrid(codes=codes)
    layout = GreedyStrategy().place(grid, rng=np.random.default_rng(5))
    _assert_exact_cover(layout, grid)
    graph = ConnectionGraph.from_layout(layout)
    assert graph.component_count() == 1
    assert not graph.floating_ids()


def test_greedy_rebonds_phase_mismatched_columns():
    # Column x0 runs from the ground; column x1 starts at layer 4, so greedy
    # bricks the two columns at incompatible layer phases (0/3/6 vs 4/7) and
    # no brick merge can ever bridge them. Repair must re-phase via plates.
    codes = np.full((2, 2, 10), EMPTY, dtype=np.int16)
    codes[0, :, :] = 4
    codes[1, :, 4:] = 4
    grid = VoxelGrid(codes=codes)
    layout = GreedyStrategy().place(grid, rng=np.random.default_rng(11))
    _assert_exact_cover(layout, grid)
    graph = ConnectionGraph.from_layout(layout)
    assert graph.component_count() == 1
    assert not graph.floating_ids()


def test_h_lookahead_values():
    # Exact equality-knapsack below rho = 25, 8-stud peeling above.
    assert [_h_lookahead(r) for r in (0, 1, 2, 5, 7, 8, 12, 16)] == [
        0,
        1,
        1,
        2,
        2,
        1,
        2,
        2,
    ]
    assert _h_lookahead(25) == 4  # 8 peeled + exact(17) = 1 + 3
    assert _h_lookahead(64) == 8


def test_seam_alignment_distinguishes_bond_patterns():
    # Stretcher bond: staggered seams never repeat -> 0.0.
    stretcher = Layout(catalog=default_catalog())
    for x in (0, 4):
        stretcher.add("brick_1x4", x, 0, 0, 0, 4)
    stretcher.add("brick_1x2", 0, 0, 3, 0, 4)
    stretcher.add("brick_1x4", 2, 0, 3, 0, 4)
    stretcher.add("brick_1x2", 6, 0, 3, 0, 4)
    assert _seam_alignment(stretcher) == 0.0

    # Stack bond: every course repeats the seam; only the top one doesn't.
    stack = Layout(catalog=default_catalog())
    for course in range(3):
        for x in (0, 4):
            stack.add("brick_1x4", x, 0, 3 * course, 0, 4)
    assert _seam_alignment(stack) == pytest.approx(2 / 3)


def test_greedy_staggers_wall_without_repair():
    # A 7-wide two-course wall: h(r) makes 6+1 and staggered splits tie on
    # part count, and the distance-aware bond term breaks the tie away from
    # the stacked seam — no reinforcement pass needed.
    codes = np.full((7, 1, 6), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    layout = GreedyStrategy(refine=False).place(grid, rng=np.random.default_rng(0))
    _assert_exact_cover(layout, grid)
    assert _seam_alignment(layout) == 0.0


def test_bond_alpha2_distance_decay_matters():
    # Two candidates above a seam: one continues it (d = 0), one keeps two
    # studs of stagger (d = 2). The decay makes the staggered candidate the
    # better bond; killing the decay (alpha2 = 0) flips the ordering, since
    # then every border with any seam in the window is penalized alike.
    from legolization.placement.base import ObjectiveWeights
    from legolization.placement.greedy import _Candidate

    def bond(strategy: GreedyStrategy, layout: Layout, cells) -> float:
        key = default_catalog().rect_key(1, len(cells), 3)
        assert key is not None
        candidate = _Candidate(
            part=default_catalog()[key],
            anchor=cells[0],
            yaw=0,
            cells=tuple(cells),
            colour=4,
        )
        return strategy._bond_score(layout, candidate)  # noqa: SLF001

    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x4", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 4, 0, 0, 0, 4)  # seam below at x = 3|4
    aligned = [(x, 0, 3) for x in range(4)]  # border continues the seam
    staggered = [(x, 0, 3) for x in range(2)]  # border two studs away

    decayed = GreedyStrategy()
    assert bond(decayed, layout, staggered) > bond(decayed, layout, aligned)

    flat = GreedyStrategy(weights=ObjectiveWeights(bond_alpha2=0.0))
    assert bond(flat, layout, staggered) < bond(flat, layout, aligned)


def test_reinforce_rebuilds_vary_with_rng():
    # F5: rebuild fills shuffle seed order within layers, so different rng
    # states must be able to produce different layouts on a tie-rich region.
    codes = np.full((7, 2, 3), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    strategy = GreedyStrategy(refine=False)

    def rebuild(seed: int) -> frozenset[tuple[str, int, int, int, int]]:
        layout = Layout(catalog=default_catalog())
        uncovered = {
            (int(x), int(y), int(z))
            for x, y, z in zip(*grid.filled_mask.nonzero(), strict=True)
        }
        strategy._fill(  # noqa: SLF001
            layout,
            grid,
            uncovered,
            np.random.default_rng(seed),
            shuffle_within_layers=True,
        )
        return frozenset((b.part_key, b.x, b.y, b.layer, b.yaw) for b in layout)

    layouts = {rebuild(seed) for seed in range(4)}
    assert len(layouts) > 1


def test_merged_rect_allows_ignore_wildcard():
    from legolization.grid import IGNORE

    layout = Layout(catalog=default_catalog())
    red = layout.add("plate_1x1", 0, 0, 0, 0, 4)
    interior = layout.add("plate_1x1", 1, 0, 0, 0, IGNORE)
    assert merged_rect(layout, red, interior) == (0, 0, 1, 0)
    maximal_random_merge(layout, np.random.default_rng(0))
    (merged,) = list(layout)
    assert merged.part_key == "plate_1x2"
    assert merged.colour_code == 4  # the specific colour wins over IGNORE


def test_soft_colour_merge_trades_colour_for_parts():
    def build() -> Layout:
        layout = Layout(catalog=default_catalog())
        layout.add("plate_1x1", 0, 0, 0, 0, 4)
        layout.add("plate_1x1", 1, 0, 0, 0, 1)
        return layout

    hard = build()
    maximal_random_merge(hard, np.random.default_rng(0), colour_mode="hard")
    assert len(hard) == 2  # colours never cross in hard mode

    soft = build()
    maximal_random_merge(
        soft, np.random.default_rng(0), colour_mode="soft", colour_weight=0.0
    )
    (merged,) = list(soft)
    assert merged.part_key == "plate_1x2"
    assert merged.colour_code in {4, 1}


@pytest.mark.parametrize("colour_weight", [-1.0, float("inf"), float("nan")])
def test_soft_colour_merge_rejects_invalid_weights(colour_weight):
    layout = Layout(catalog=default_catalog())
    with pytest.raises(ValueError, match="finite and non-negative"):
        maximal_random_merge(
            layout,
            np.random.default_rng(0),
            colour_mode="soft",
            colour_weight=colour_weight,
        )


def test_compact_columns_reforms_bricks_on_voted_phase():
    layout = Layout(catalog=default_catalog())
    ids = {layout.add("plate_1x1", 0, 0, z, 0, 4).brick_id for z in range(7)}
    merged = compact_columns(layout, ids)
    # Phase 0 wins (6 of 7 plates convert); the leftover plate stays.
    assert merged == 2
    kinds = sorted(b.part_key for b in layout)
    assert kinds == ["brick_1x1", "brick_1x1", "plate_1x1"]
    assert {b.layer for b in layout if b.part_key == "brick_1x1"} == {0, 3}


def test_compact_columns_respects_colour_compatibility():
    from legolization.grid import IGNORE

    layout = Layout(catalog=default_catalog())
    colours = (4, IGNORE, 4, 4, 1, 4)  # triple 2 mixes red and blue: skipped
    ids = {
        layout.add("plate_1x1", 0, 0, z, 0, colour).brick_id
        for z, colour in enumerate(colours)
    }
    merged = compact_columns(layout, ids)
    assert merged == 1
    (brick,) = [b for b in layout if b.part_key == "brick_1x1"]
    assert brick.layer == 0
    assert brick.colour_code == 4  # IGNORE resolved by the specific plates


def test_final_remerge_reclaims_plate_rafts():
    # A 4x2 slab tiled as three mismatched plate layers — the raft shape
    # connectivity repairs leave behind. Re-phasing must find brick_2x4.
    codes = np.full((4, 2, 3), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    layout = Layout(catalog=default_catalog())
    for y in (0, 1):
        layout.add("plate_1x4", 0, y, 0, 0, 4)
    for x in (0, 2):
        for y in (0, 1):
            layout.add("plate_1x2", x, y, 1, 0, 4)
    layout.add("plate_2x2", 0, 0, 2, 0, 4)
    layout.add("plate_2x2", 2, 0, 2, 0, 4)
    assert len(layout) == 8

    changed = final_remerge(layout, grid, np.random.default_rng(0))

    assert changed
    assert len(layout) == 1
    assert next(iter(layout)).part_key == "brick_2x4"
    _assert_exact_cover(layout, grid)


def test_resolve_ignore_colours_uses_nearest_neighbour():
    from legolization.grid import IGNORE

    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    layout.add("brick_1x1", 1, 0, 0, 0, IGNORE)  # touches red
    layout.add("brick_1x1", 5, 5, 0, 0, IGNORE)  # isolated
    brick_ids = set(layout.bricks)
    recoloured = resolve_ignore_colours(layout)
    assert recoloured == 2
    assert set(layout.bricks) == brick_ids
    colours = sorted(b.colour_code for b in layout)
    assert colours == [4, 4, 71]  # neighbour red, isolated falls back to gray


def test_reinforce_accepts_disjoint_grid_islands(monkeypatch):
    # Two islands can never share a component, so reinforcement must treat
    # the grid's own island count as done — not chase a single component
    # through futile connectivity repairs and delete-rebuild churn.
    codes = np.full((5, 1, 3), EMPTY, dtype=np.int16)
    codes[:2, 0, :] = 4
    codes[3:, 0, :] = 4
    grid = VoxelGrid(codes=codes)
    assert _grid_component_count(grid) == 2

    def fail_connectivity(*_args: object, **_kwargs: object) -> None:
        msg = "connectivity repair ran on an already island-complete layout"
        raise AssertionError(msg)

    monkeypatch.setattr(
        "legolization.placement.greedy.improve_connectivity",
        fail_connectivity,
    )
    layout = GreedyStrategy().place(grid, rng=np.random.default_rng(0))

    graph = ConnectionGraph.from_layout(layout)
    assert graph.component_count() == 2
    assert not graph.floating_ids()


def test_hollow_sphere_brick_count_regression():
    # The audit's F3 case: repaired hollow shells used to carry ~3x the
    # parts as permanent plate rafts. Guard the reclaimed count.
    radius = 4
    n = 2 * radius + 1
    codes = np.full((n, n, n), EMPTY, dtype=np.int16)
    xs, ys, zs = np.mgrid[0:n, 0:n, 0:n]
    inside = (xs - radius) ** 2 + (ys - radius) ** 2 + (zs - radius) ** 2
    codes[inside <= radius * radius] = 4
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)

    from legolization.pipeline import PipelineConfig, run

    # Thin shell to isolate the raft-reclaim mechanism (the audit's setup);
    # the default 3-plate shell simply carries more material.
    result = run(grid, PipelineConfig(seed=0, shell_studs=1, shell_plates=1))
    assert result.buildable
    assert result.brick_count <= 160


@pytest.mark.parametrize("acceptance", ["maximin", "rbe"])
def test_luo_stabilize_repairs_collapsing_bridge(acceptance, bad_bridge):
    from legolization.stability import analyze

    layout, grid = bad_bridge
    assert not analyze(layout).stable

    strategy = LuoStrategy(acceptance=acceptance)
    strategy._stabilize(layout, grid, np.random.default_rng(0))  # noqa: SLF001

    result = analyze(layout)
    assert result.stable
    _assert_exact_cover(layout, grid)


def test_improve_connectivity_bridges_grounded_towers():
    # Two grounded 1x1 columns touch side-by-side but share no studs: two
    # brick-graph components that only a cross-column remerge can join.
    codes = np.full((2, 1, 6), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    layout = Layout(catalog=default_catalog())
    for x in (0, 1):
        for level in (0, 1):
            layout.add("brick_1x1", x, 0, 3 * level, 0, 4)
    assert ConnectionGraph.from_layout(layout).component_count() == 2

    final = improve_connectivity(layout, grid, np.random.default_rng(0))

    assert final == 1
    assert not ConnectionGraph.from_layout(layout).floating_ids()
    _assert_exact_cover(layout, grid)


def test_greedy_sweeps_layers_bottom_up():
    codes = np.full((2, 1, 6), EMPTY, dtype=np.int16)
    codes[0, 0, 5] = 4
    codes[1, 0, 0] = 4
    grid = VoxelGrid(codes=codes)

    layout = GreedyStrategy(refine=False).place(grid, rng=np.random.default_rng(3))

    assert [brick.layer for brick in layout] == [0, 5]


def test_compact_vertical_reforms_bricks():
    from legolization.placement.merge import compact_vertical

    layout = Layout(catalog=default_catalog())
    for z in range(3):
        layout.add("plate_2x4", 0, 0, z, 0, 4)
    layout.add("plate_1x1", 0, 0, 3, 0, 4)  # lone plate on top stays
    assert compact_vertical(layout) == 1
    keys = sorted(b.part_key for b in layout)
    assert keys == ["brick_2x4", "plate_1x1"]


def test_evaluate_reports_terms():
    grid = _pyramid_grid()
    layout = GreedyStrategy(refine=False).place(grid, rng=np.random.default_rng(0))
    report = evaluate(layout, grid)
    assert 0 < report.cost <= 1
    assert report.colour_error == 0.0
    assert report.total >= 0


# --- shape-preserving slopes ---


def _preserve_fixture(
    codes: np.ndarray,
    adds: list[tuple],
) -> tuple[VoxelGrid, Layout]:
    grid = VoxelGrid(codes=codes)
    layout = Layout(catalog=default_catalog())
    for entry in adds:
        layout.add(*entry)
    return grid, layout


def _filled(layout: Layout) -> list[tuple[int, int, int]]:
    return sorted(c for b in layout for c in layout.filled_cells_of(b))


def test_preserve_swaps_45_profile_without_changing_fill():
    codes = np.full((2, 1, 3), EMPTY, dtype=np.int16)
    codes[0, 0, :] = 4
    codes[1, 0, 0] = 4
    grid, layout = _preserve_fixture(
        codes, [("brick_1x1", 0, 0, 0, 0, 4), ("plate_1x1", 1, 0, 0, 0, 4)]
    )
    before = _filled(layout)
    assert apply_slopes(layout, grid, mode="preserve") == 1
    assert [b.part_key for b in layout] == ["slope_45_2x1"]
    assert _filled(layout) == before  # zero material added or removed


def test_preserve_prefers_larger_slopes():
    # Stud column plus a 2-cell tread run: the 33-degree 3x1 must win over
    # a 45-degree 2x1 matching at an earlier scan position.
    codes = np.full((3, 1, 3), EMPTY, dtype=np.int16)
    codes[0, 0, :] = 4
    codes[1, 0, 0] = 4
    codes[2, 0, 0] = 4
    grid, layout = _preserve_fixture(
        codes,
        [
            ("brick_1x1", 0, 0, 0, 0, 4),
            ("plate_1x1", 1, 0, 0, 0, 4),
            ("plate_1x1", 2, 0, 0, 0, 4),
        ],
    )
    assert apply_slopes(layout, grid, mode="preserve") == 1
    assert [b.part_key for b in layout] == ["slope_33_3x1"]


def test_preserve_places_wide_2x2_slope():
    codes = np.full((2, 2, 3), EMPTY, dtype=np.int16)
    codes[0, :, :] = 4
    codes[1, :, 0] = 4
    grid, layout = _preserve_fixture(
        codes,
        [
            ("brick_1x2", 0, 0, 0, 90, 4),
            ("plate_1x1", 1, 0, 0, 0, 4),
            ("plate_1x1", 1, 1, 0, 0, 4),
        ],
    )
    assert apply_slopes(layout, grid, mode="preserve") == 1
    assert [b.part_key for b in layout] == ["slope_45_2x2"]


def test_preserve_carves_and_refills_overlapping_donors():
    # The tread donor sticks one cell out of the slope profile: it is
    # carved, the slope placed, and the leftover cell refilled — the
    # filled-cell set survives untouched.
    codes = np.full((2, 2, 3), EMPTY, dtype=np.int16)
    codes[0, 0, :] = 4
    codes[1, 0, 0] = 4
    codes[1, 1, 0] = 4
    grid, layout = _preserve_fixture(
        codes, [("brick_1x1", 0, 0, 0, 0, 4), ("plate_1x2", 1, 0, 0, 90, 4)]
    )
    before = _filled(layout)
    assert apply_slopes(layout, grid, mode="preserve") == 1
    parts = sorted(b.part_key for b in layout)
    assert parts == ["plate_1x1", "slope_45_2x1"]
    assert _filled(layout) == before


def test_preserve_rejects_bad_candidates():
    # Void cell still inside the shape (a full brick step): no swap.
    codes = np.full((2, 1, 3), EMPTY, dtype=np.int16)
    codes[0, 0, :] = 4
    codes[1, 0, :] = 4
    grid, layout = _preserve_fixture(
        codes, [("brick_1x1", 0, 0, 0, 0, 4), ("brick_1x1", 1, 0, 0, 0, 4)]
    )
    assert apply_slopes(layout, grid, mode="preserve") == 0

    # Donor colours differ: no swap.
    codes = np.full((2, 1, 3), EMPTY, dtype=np.int16)
    codes[0, 0, :] = 4
    codes[1, 0, 0] = 4
    grid, layout = _preserve_fixture(
        codes, [("brick_1x1", 0, 0, 0, 0, 4), ("plate_1x1", 1, 0, 0, 0, 14)]
    )
    assert apply_slopes(layout, grid, mode="preserve") == 0


def test_smooth_mode_is_the_legacy_default():
    codes = np.full((1, 3, 3), EMPTY, dtype=np.int16)
    codes[0, 0, :] = 4
    grid = VoxelGrid(codes=codes)
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x1", 0, 0, 0, 0, 4)
    assert apply_slopes(layout, grid) == 1  # same behaviour as before
    (brick,) = list(layout)
    assert brick.part_key == "slope_45_2x1"
