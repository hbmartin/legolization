"""Layer engine and the four per-layer tiling strategies."""

import math
from dataclasses import fields
from types import SimpleNamespace

import numpy as np
import pytest

from legolization import runtime as runtime_mod
from legolization.catalog import default_catalog
from legolization.graph import ConnectionGraph, TopologyMetrics
from legolization.grid import EMPTY, IGNORE, VoxelGrid
from legolization.layout import Layout
from legolization.placement.aesthetics import (
    colour_speckle_error,
    layer_symmetry_error,
    perpendicularity_error,
    profile_roughness,
    symmetry_error,
)
from legolization.placement.base import (
    ObjectiveReport,
    ObjectiveWeights,
    _seam_alignment,
    evaluate,
)
from legolization.placement.greedy import _h_lookahead
from legolization.placement.layered import (
    BeautyStrategy,
    BeautyWeights,
    BondStrategy,
    FastStrategy,
    SmGaConfig,
    SmGaStrategy,
)
from legolization.placement.layered.engine import (
    LayeredStrategy,
    LayerProblem,
    Rect2D,
    build_context,
    mergeable_union,
    random_fill,
    rects_covering,
    slab_decompose,
)


def _wall_grid(width: int = 8, courses: int = 2) -> VoxelGrid:
    codes = np.full((width, 1, 3 * courses), 4, dtype=np.int16)
    return VoxelGrid(codes=codes)


def _layer_problem(columns: dict[tuple[int, int], int]) -> LayerProblem:
    return LayerProblem(
        layer=0,
        height_plates=3,
        columns=frozenset(columns),
        colour_of=dict(columns),
    )


def test_slab_decompose_brick_and_plate_problems():
    codes = np.full((2, 1, 7), 4, dtype=np.int16)
    codes[1, 0, 3:] = EMPTY  # second column only exists in the first slab
    grid = VoxelGrid(codes=codes)
    problems = slab_decompose(grid)
    kinds = [(p.layer, p.height_plates, len(p.columns)) for p in problems]
    # Slab 0: both columns brick-eligible; slab 3: one column; layer 6: plate.
    assert kinds == [(0, 3, 2), (3, 3, 1), (6, 1, 1)]


def test_slab_decompose_mixed_colours_fall_back_to_plates():
    codes = np.full((1, 1, 3), 4, dtype=np.int16)
    codes[0, 0, 1] = 14  # colour change inside the slab
    problems = slab_decompose(VoxelGrid(codes=codes))
    assert [(p.layer, p.height_plates) for p in problems] == [(0, 1), (1, 1), (2, 1)]


def test_slab_decompose_ignore_is_brick_compatible():
    codes = np.full((1, 1, 3), 4, dtype=np.int16)
    codes[0, 0, 1] = IGNORE
    problems = slab_decompose(VoxelGrid(codes=codes))
    assert [(p.layer, p.height_plates) for p in problems] == [(0, 3)]
    assert problems[0].colour_of[(0, 0)] == 4


def test_rects_covering_respects_colours_and_bounds():
    problem = _layer_problem({(0, 0): 4, (1, 0): 4, (2, 0): 14})
    rects = rects_covering(problem, (0, 0), default_catalog())
    assert all(rect.colour == 4 for rect in rects)
    assert max(rect.area for rect in rects) == 2  # the red run is 2 long


def test_mergeable_union_requires_solid_catalog_rect():
    problem = _layer_problem({(x, 0): 4 for x in range(5)})
    catalog = default_catalog()
    a = Rect2D(x0=0, y0=0, x1=1, y1=0, colour=4)
    b = Rect2D(x0=2, y0=0, x1=2, y1=0, colour=4)
    union = mergeable_union(a, b, problem, catalog)
    assert union is not None
    assert (union.x0, union.x1) == (0, 2)
    gap = Rect2D(x0=4, y0=0, x1=4, y1=0, colour=4)
    assert mergeable_union(a, gap, problem, catalog) is None  # not contiguous


def test_random_fill_is_feasible_exact_cover():
    columns = {(x, y): 4 for x in range(5) for y in range(3)}
    problem = _layer_problem(columns)
    rects = random_fill(problem, np.random.default_rng(0), default_catalog())
    covered: set[tuple[int, int]] = set()
    for rect in rects:
        assert not covered & rect.columns()
        covered |= rect.columns()
    assert covered == problem.columns


def test_build_context_reports_supports_and_seams():
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x2", 0, 0, 0, 0, 4)
    layout.add("brick_1x2", 2, 0, 0, 0, 4)  # seam below at x = 1|2
    problem = LayerProblem(
        layer=3,
        height_plates=3,
        columns=frozenset((x, 0) for x in range(4)),
        colour_of={(x, 0): 4 for x in range(4)},
    )
    context = build_context(layout, problem)
    assert len(set(context.support_of.values())) == 2
    assert (((1, 0), 0)) in context.seams
    assert context.seam_priority[((1, 0), 0)] == 1.0  # disconnected towers


def test_bond_staggers_wall_courses():
    layout = BondStrategy().place(_wall_grid(), rng=np.random.default_rng(0))
    assert _seam_alignment(layout) == 0.0


def test_bond_brick_count_is_competitive():
    codes = np.full((8, 4, 6), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    from legolization.placement.greedy import GreedyStrategy

    bond = BondStrategy().place(grid, rng=np.random.default_rng(0))
    greedy = GreedyStrategy(refine=False).place(grid, rng=np.random.default_rng(0))
    assert len(bond) <= len(greedy) * 1.2


def test_bond_lookahead_counts_every_transverse_row():
    rect = Rect2D(x0=4, y0=0, x1=5, y1=1, colour=4)
    uncovered = (
        {(x, 0) for x in range(4)}
        | {(x, 1) for x in range(2, 4)}
        | {(x, 1) for x in range(6, 8)}
    )
    columns = uncovered | set(rect.columns())
    problem = _layer_problem(dict.fromkeys(columns, 4))

    lookahead = BondStrategy()._lookahead(  # noqa: SLF001
        problem,
        uncovered,
        rect,
        axis=0,
    )

    assert lookahead == _h_lookahead(4) + 2 * _h_lookahead(2)


def test_smga_fitness_prefers_fewer_and_crossing_bricks():
    # Below: four 1x2 bricks laid along y. Above candidates with equal
    # brick and support counts differ only in direction: 1x4s along x cross
    # the below bricks (n_p = 2), square 2x2s carry no direction (n_p = 0).
    layout = Layout(catalog=default_catalog())
    for x in range(4):
        layout.add("brick_1x2", x, 0, 0, 90, 4)
    problem = LayerProblem(
        layer=3,
        height_plates=3,
        columns=frozenset((x, y) for x in range(4) for y in range(2)),
        colour_of={(x, y): 4 for x in range(4) for y in range(2)},
    )
    context = build_context(layout, problem)
    strategy = SmGaStrategy()
    crossing = tuple(Rect2D(x0=0, y0=y, x1=3, y1=y, colour=4) for y in range(2))
    squares = (
        Rect2D(x0=0, y0=0, x1=1, y1=1, colour=4),
        Rect2D(x0=2, y0=0, x1=3, y1=1, colour=4),
    )
    ones = tuple(
        Rect2D(x0=x, y0=y, x1=x, y1=y, colour=4) for x in range(4) for y in range(2)
    )
    fit = strategy._fitness  # noqa: SLF001
    assert fit(context, crossing) > fit(context, ones)  # fewer bricks dominates
    assert fit(context, crossing) > fit(context, squares)  # crossing rewarded


def test_smga_returns_best_chromosome_across_generations(monkeypatch):
    problem = _layer_problem({(x, 0): 4 for x in range(3)})
    context = build_context(Layout(catalog=default_catalog()), problem)
    elite = (Rect2D(x0=0, y0=0, x1=2, y1=0, colour=4),)
    inferior = tuple(Rect2D(x0=x, y0=0, x1=x, y1=0, colour=4) for x in range(3))
    initial = iter((inferior, inferior, elite))

    def fake_random_fill(*args, **kwargs) -> list[Rect2D]:
        del args, kwargs
        return list(next(initial))

    def fake_next_generation(  # noqa: PLR0913, PLR0917 - mirrors the production method
        self, problem, below, rng, population, fitnesses, p_mut
    ) -> tuple[list[tuple[Rect2D, ...]], list[float]]:
        del problem, rng, population, fitnesses, p_mut
        children = [elite, inferior, inferior]
        return children, [self._fitness(below, child) for child in children]

    monkeypatch.setattr(
        "legolization.placement.layered.smga.random_fill", fake_random_fill
    )
    monkeypatch.setattr(SmGaStrategy, "_next_generation", fake_next_generation)
    strategy = SmGaStrategy(
        config=SmGaConfig(
            population=3,
            max_generations=1,
            patience=1,
            p_mut_hi=0.0,
            p_mut_lo=0.0,
        )
    )

    result = strategy.tile(
        problem,
        context,
        rng=np.random.default_rng(0),
        deadline=None,
    )

    assert tuple(result) == elite


def test_smga_operators_preserve_exact_cover():
    columns = {(x, y): 4 for x in range(6) for y in range(2)}
    problem = _layer_problem(columns)
    rng = np.random.default_rng(0)
    strategy = SmGaStrategy(config=SmGaConfig(population=6, max_generations=5))
    catalog = default_catalog()
    parent_a = tuple(random_fill(problem, rng, catalog))
    parent_b = tuple(random_fill(problem, rng, catalog))
    child = strategy._crossover(problem, rng, parent_a, parent_b)  # noqa: SLF001
    mutated = strategy._split_and_merge(problem, rng, child)  # noqa: SLF001
    for chromosome in (child, mutated):
        covered: set[tuple[int, int]] = set()
        for rect in chromosome:
            assert not covered & rect.columns()
            covered |= rect.columns()
        assert covered == problem.columns


def test_smga_weight_discipline_enforced():
    with pytest.raises(ValueError, match="c1"):
        SmGaConfig(c1=2.0, c2=1.0, c3=1.0)


def test_beauty_presets_trade_bricks_for_symmetry():
    # An odd-width box (two brick slabs so single-component is reachable
    # with real bricks): the efficiency preset accepts a lopsided split for
    # fewer parts, the aesthetics preset pays extra parts for mirror pairs.
    codes = np.full((7, 3, 6), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    results = {}
    for preset in ("efficiency", "aesthetics"):
        strategy = BeautyStrategy(beauty=BeautyWeights.preset(preset))
        layout = strategy.place(grid, rng=np.random.default_rng(0))
        results[preset] = (len(layout), symmetry_error(layout))
    assert results["efficiency"][0] < results["aesthetics"][0]
    assert results["aesthetics"][1] < results["efficiency"][1]


def test_beauty_aesthetics_preset_is_symmetric():
    codes = np.full((8, 3, 6), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    strategy = BeautyStrategy(beauty=BeautyWeights.preset("aesthetics"))
    layout = strategy.place(grid, rng=np.random.default_rng(0))
    assert symmetry_error(layout) == 0.0


def test_beauty_scores_balance_from_the_completed_tiling():
    strategy = BeautyStrategy(beauty=BeautyWeights(w_s=0.0, w_a=1.0, w_h=0.0, w_v=0.0))
    mirrored = (
        Rect2D(x0=0, y0=0, x1=1, y1=0, colour=4),
        Rect2D(x0=4, y0=0, x1=5, y1=0, colour=4),
    )

    assert (
        strategy._axis_balance_cost(  # noqa: SLF001
            mirrored,
            mirror_sum=5,
            axis=0,
        )
        == 0.0
    )


def test_stackable_footprints_preserve_colour_and_ignore_disjoint_rects():
    layout = Layout(catalog=default_catalog())
    layout.add("plate_1x2", 0, 0, 0, 0, 4)
    layout.add("plate_1x2", 0, 0, 1, 0, 4)
    problem = LayerProblem(
        layer=2,
        height_plates=1,
        columns=frozenset({(0, 0), (1, 0), (3, 0)}),
        colour_of={(0, 0): 4, (1, 0): 4, (3, 0): 4},
    )
    context = build_context(layout, problem)
    footprint = frozenset({(0, 0), (1, 0)})
    assert context.stackable_footprints[footprint] == 4
    strategy = BeautyStrategy(beauty=BeautyWeights(w_s=0.0, w_a=0.0, w_h=0.0, w_v=1.0))
    compatible = Rect2D(x0=0, y0=0, x1=1, y1=0, colour=4)
    incompatible = Rect2D(x0=0, y0=0, x1=1, y1=0, colour=14)
    disjoint = Rect2D(x0=3, y0=0, x1=3, y1=0, colour=4)

    assert strategy._rect_cost(context, compatible) == 0.0  # noqa: SLF001
    assert strategy._rect_cost(context, incompatible) == 1.0  # noqa: SLF001
    assert strategy._rect_cost(context, disjoint) == 0.0  # noqa: SLF001


def test_fast_prefers_bigger_bricks():
    codes = np.full((8, 2, 3), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    layout = FastStrategy().place(grid, rng=np.random.default_rng(0))
    areas = sorted(len({(x, y) for x, y, _ in layout.cells_of(b)}) for b in layout)
    assert areas[-1] == 16  # a 2x8 emerged from the all-1x1 start


def test_fast_merge_loop_respects_expired_deadline():
    problem = _layer_problem({(x, 0): 4 for x in range(4)})
    context = build_context(Layout(catalog=default_catalog()), problem)
    rects = [Rect2D(x0=x, y0=0, x1=x, y1=0, colour=4) for x in range(4)]

    result = FastStrategy()._merge_to_fixpoint(  # noqa: SLF001
        problem,
        context,
        rects,
        deadline=0.0,
    )

    assert result == rects


def test_beauty_uses_global_mirror_center():
    # An L-shaped model: the upper layer's own bbox centre differs from the
    # whole-model centre. The strategy must balance about the global plane the
    # objective measures, not each layer's private one.
    codes = np.full((8, 2, 6), -1, dtype=np.int16)
    codes[:, :, :3] = 4  # full 8x2 slab
    codes[2:8, :, 3:6] = 4  # offset upper slab
    grid = VoxelGrid(codes=codes)
    strategy = BeautyStrategy(beauty=BeautyWeights.preset("aesthetics"))
    layout = strategy.place(grid, rng=np.random.default_rng(0))
    assert layout is not None
    # The per-run mirror stash must not leak between placements.
    assert strategy._mirror_x is None  # noqa: SLF001
    assert strategy._mirror_y is None  # noqa: SLF001

    # Directly driven tile() still uses the per-problem bbox (the paper's
    # behaviour), so unit tests over bare problems keep meaning.
    problem = _layer_problem({(x, 0): 4 for x in range(4)})
    context = build_context(Layout(catalog=default_catalog()), problem)
    rects = strategy.tile(problem, context, rng=np.random.default_rng(0), deadline=None)
    assert {column for rect in rects for column in rect.columns()} == set(
        problem.columns
    )


def test_beauty_searches_both_global_axes_and_keeps_lower_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grid = VoxelGrid(codes=np.full((2, 1, 3), 4, dtype=np.int16))
    strategy = BeautyStrategy(beauty=BeautyWeights.preset("aesthetics"))
    searched_axes: list[int | None] = []
    finalized_axes: list[int | None] = []

    def fake_tile_layout(
        self: BeautyStrategy,
        *,
        grid: VoxelGrid,
        rng: np.random.Generator,
        deadline: float | None = None,
    ) -> Layout:
        del grid, rng, deadline
        searched_axes.append(self._mirror_axis)
        assert self._mirror_axis is not None
        self._run_cost = 2.0 if self._mirror_axis == 0 else 1.0
        layout = Layout(catalog=default_catalog())
        layout.add(
            part_key="brick_1x1",
            x=self._mirror_axis,
            y=0,
            layer=0,
            yaw=0,
            colour_code=4,
        )
        return layout

    def fake_finalize_layout(  # noqa: PLR0913
        self: BeautyStrategy,
        *,
        layout: Layout,
        grid: VoxelGrid,
        rng: np.random.Generator,
        deadline: float | None,
        return_topology: bool = False,
    ) -> None:
        del layout, grid, rng, deadline, return_topology
        finalized_axes.append(self._mirror_axis)

    monkeypatch.setattr(LayeredStrategy, "_tile_layout", fake_tile_layout)
    monkeypatch.setattr(LayeredStrategy, "_finalize_layout", fake_finalize_layout)

    def fail_candidate_key(*args: object, **kwargs: object) -> None:
        del args, kwargs
        pytest.fail("a singleton finalist must not compute a selection key")

    monkeypatch.setattr(
        "legolization.placement.layered.beauty._candidate_key",
        fail_candidate_key,
    )
    layout = strategy.place(grid, rng=np.random.default_rng(0))

    assert searched_axes == [0, 1]
    assert finalized_axes == [1]
    assert next(iter(layout)).x == 1
    assert strategy._mirror_x is None  # noqa: SLF001
    assert strategy._mirror_y is None  # noqa: SLF001
    assert strategy._mirror_axis is None  # noqa: SLF001


def test_beauty_treats_one_ulp_cost_difference_as_a_tie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grid = VoxelGrid(codes=np.full((2, 1, 3), 4, dtype=np.int16))
    strategy = BeautyStrategy(beauty=BeautyWeights.preset("balanced"))
    finalized_axes: list[int | None] = []

    def fake_tile_layout(
        self: BeautyStrategy,
        *,
        grid: VoxelGrid,
        rng: np.random.Generator,
        deadline: float | None,
    ) -> Layout:
        del grid, rng, deadline
        assert self._mirror_axis is not None
        self._run_cost = (
            1.0 if self._mirror_axis == 0 else math.nextafter(1.0, math.inf)
        )
        layout = Layout(catalog=default_catalog())
        layout.add("brick_1x1", 0, 0, 0, 0, 4)
        if self._mirror_axis == 0:
            layout.add("brick_1x1", 0, 0, 3, 0, 4)
        return layout

    def fake_finalize_layout(  # noqa: PLR0913
        self: BeautyStrategy,
        *,
        layout: Layout,
        grid: VoxelGrid,
        rng: np.random.Generator,
        deadline: float | None,
        return_topology: bool = False,
    ) -> None:
        del layout, grid, rng, deadline, return_topology
        finalized_axes.append(self._mirror_axis)

    monkeypatch.setattr(LayeredStrategy, "_tile_layout", fake_tile_layout)
    monkeypatch.setattr(LayeredStrategy, "_finalize_layout", fake_finalize_layout)

    layout = strategy.place(grid=grid, rng=np.random.default_rng(0))

    assert finalized_axes == [0, 1]
    assert len(layout) == 1


def test_beauty_tie_break_uses_post_finalization_brick_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grid = VoxelGrid(codes=np.full((2, 1, 3), 4, dtype=np.int16))
    strategy = BeautyStrategy(beauty=BeautyWeights.preset("aesthetics"))
    finalized_axes: list[int | None] = []

    def fake_tile_layout(
        self: BeautyStrategy,
        *,
        grid: VoxelGrid,
        rng: np.random.Generator,
        deadline: float | None,
    ) -> Layout:
        del grid, rng, deadline
        self._run_cost = 1.0
        layout = Layout(catalog=default_catalog())
        assert self._mirror_axis is not None
        layout.add(
            part_key="brick_1x1",
            x=self._mirror_axis,
            y=0,
            layer=0,
            yaw=0,
            colour_code=4,
        )
        return layout

    def fake_finalize_layout(  # noqa: PLR0913
        self: BeautyStrategy,
        *,
        layout: Layout,
        grid: VoxelGrid,
        rng: np.random.Generator,
        deadline: float | None,
        return_topology: bool = False,
    ) -> None:
        del grid, rng, deadline, return_topology
        finalized_axes.append(self._mirror_axis)
        if self._mirror_axis == 0:
            # Both raw candidates have one brick and perfect symmetry. Make the
            # x-axis finalist connected but less efficient only during
            # finalization, so feasibility and symmetry remain tied.
            layout.add(
                part_key="brick_1x1",
                x=0,
                y=0,
                layer=3,
                yaw=0,
                colour_code=4,
            )

    monkeypatch.setattr(LayeredStrategy, "_tile_layout", fake_tile_layout)
    monkeypatch.setattr(LayeredStrategy, "_finalize_layout", fake_finalize_layout)

    layout = strategy.place(grid, rng=np.random.default_rng(0))

    assert finalized_axes == [0, 1]
    assert len(layout) == 1
    assert next(iter(layout)).x == 1


def test_beauty_flat_tie_break_uses_symmetry_before_brick_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grid = VoxelGrid(codes=np.full((5, 3, 3), 4, dtype=np.int16))
    strategy = BeautyStrategy(beauty=BeautyWeights.preset("aesthetics"))
    finalized_axes: list[int | None] = []

    def fake_tile_layout(
        self: BeautyStrategy,
        *,
        grid: VoxelGrid,
        rng: np.random.Generator,
        deadline: float | None,
    ) -> Layout:
        del self, grid, rng, deadline
        layout = Layout(catalog=default_catalog())
        for x, y in ((0, 0), (4, 0), (0, 2), (4, 2)):
            layout.add(
                part_key="brick_1x1",
                x=x,
                y=y,
                layer=0,
                yaw=0,
                colour_code=4,
            )
        return layout

    def fake_finalize_layout(  # noqa: PLR0913
        self: BeautyStrategy,
        *,
        layout: Layout,
        grid: VoxelGrid,
        rng: np.random.Generator,
        deadline: float | None,
        return_topology: bool = False,
    ) -> None:
        del grid, rng, deadline, return_topology
        finalized_axes.append(self._mirror_axis)
        if self._mirror_axis == 0:
            removed = next(brick for brick in layout if (brick.x, brick.y) == (4, 2))
            layout.remove(removed.brick_id)

    monkeypatch.setattr(LayeredStrategy, "_tile_layout", fake_tile_layout)
    monkeypatch.setattr(LayeredStrategy, "_finalize_layout", fake_finalize_layout)

    layout = strategy.place(grid=grid, rng=np.random.default_rng(0))

    assert finalized_axes == [0, 1]
    assert len(layout) == 4
    assert symmetry_error(layout) == 0.0
    assert any((brick.x, brick.y) == (4, 2) for brick in layout)


@pytest.mark.parametrize(
    ("axis_zero", "axis_one", "expected_layers"),
    [
        (
            ((0, 0), (2, 0)),
            ((0, 0), (0, 3), (0, 6)),
            frozenset((0, 3, 6)),
        ),
        (((0, 3),), ((0, 0), (0, 3)), frozenset((0, 3))),
        (((0, 3), (0, 6)), ((0, 0), (2, 0)), frozenset((0,))),
    ],
    ids=("connectivity", "grounding", "grounding-before-connectivity"),
)
def test_beauty_tie_break_prefers_feasibility_before_brick_count(
    monkeypatch: pytest.MonkeyPatch,
    axis_zero: tuple[tuple[int, int], ...],
    axis_one: tuple[tuple[int, int], ...],
    expected_layers: frozenset[int],
) -> None:
    grid = VoxelGrid(codes=np.full((3, 1, 9), 4, dtype=np.int16))
    strategy = BeautyStrategy(beauty=BeautyWeights.preset("aesthetics"))
    keyed_topologies: list[TopologyMetrics | None] = []

    def fake_tile_layout(
        self: BeautyStrategy,
        *,
        grid: VoxelGrid,
        rng: np.random.Generator,
        deadline: float | None,
    ) -> Layout:
        del grid, rng, deadline
        self._run_cost = 1.0
        layout = Layout(catalog=default_catalog())
        assert self._mirror_axis is not None
        placements = axis_zero if self._mirror_axis == 0 else axis_one
        for x, layer in placements:
            layout.add(
                part_key="brick_1x1",
                x=x,
                y=0,
                layer=layer,
                yaw=0,
                colour_code=4,
            )
        return layout

    def fake_finalize_layout(  # noqa: PLR0913
        self: BeautyStrategy,
        *,
        layout: Layout,
        grid: VoxelGrid,
        rng: np.random.Generator,
        deadline: float | None,
        return_topology: bool = False,
    ) -> TopologyMetrics | None:
        del self, grid, rng, deadline
        assert return_topology
        return ConnectionGraph.from_layout(layout).topology_metrics()

    from legolization.placement.layered import beauty as beauty_module

    original_candidate_key = beauty_module._candidate_key  # noqa: SLF001

    def record_candidate_key(
        finalist: beauty_module._Finalist,
        *,
        component_target: int,
    ) -> tuple[bool, bool, float, int, int, int, int]:
        keyed_topologies.append(finalist.topology)
        return original_candidate_key(
            finalist,
            component_target=component_target,
        )

    monkeypatch.setattr(LayeredStrategy, "_tile_layout", fake_tile_layout)
    monkeypatch.setattr(LayeredStrategy, "_finalize_layout", fake_finalize_layout)
    monkeypatch.setattr(beauty_module, "_candidate_key", record_candidate_key)

    layout = strategy.place(grid=grid, rng=np.random.default_rng(0))

    assert len(layout) == len(axis_one)
    assert {brick.layer for brick in layout} == expected_layers
    assert keyed_topologies
    assert all(topology is not None for topology in keyed_topologies)


def test_beauty_tied_finalists_split_remaining_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grid = VoxelGrid(codes=np.full((2, 1, 3), 4, dtype=np.int16))
    strategy = BeautyStrategy(beauty=BeautyWeights.preset("aesthetics"))
    tile_deadlines: list[float | None] = []
    finalist_deadlines: list[float | None] = []
    now = 0.0

    def fake_tile_layout(
        self: BeautyStrategy,
        *,
        grid: VoxelGrid,
        rng: np.random.Generator,
        deadline: float | None,
    ) -> Layout:
        nonlocal now
        del grid, rng
        tile_deadlines.append(deadline)
        self._run_cost = 1.0
        layout = Layout(catalog=default_catalog())
        assert self._mirror_axis is not None
        layout.add(
            part_key="brick_1x1",
            x=self._mirror_axis,
            y=0,
            layer=0,
            yaw=0,
            colour_code=4,
        )
        if self._mirror_axis == 1:
            now = 60.0
        return layout

    def fake_finalize_layout(  # noqa: PLR0913
        self: BeautyStrategy,
        *,
        layout: Layout,
        grid: VoxelGrid,
        rng: np.random.Generator,
        deadline: float | None,
        return_topology: bool = False,
    ) -> None:
        nonlocal now
        del self, layout, grid, rng, return_topology
        finalist_deadlines.append(deadline)
        now = 80.0

    monkeypatch.setattr(
        runtime_mod,
        "time",
        SimpleNamespace(monotonic=lambda: now),
    )
    monkeypatch.setattr(LayeredStrategy, "_tile_layout", fake_tile_layout)
    monkeypatch.setattr(LayeredStrategy, "_finalize_layout", fake_finalize_layout)

    strategy.place(grid=grid, rng=np.random.default_rng(0), deadline=100.0)

    assert tile_deadlines == [50.0, 100.0]
    assert finalist_deadlines == [80.0, 100.0]


def test_deadline_share_does_not_extend_an_expired_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runtime_mod,
        "time",
        SimpleNamespace(monotonic=lambda: 20.0),
    )

    assert runtime_mod.deadline_share(deadline=10.0, fraction=0.5) == 10.0


def test_aesthetics_metrics_on_hand_layouts():
    crossing = Layout(catalog=default_catalog())
    crossing.add("brick_1x4", 0, 0, 0, 0, 4)
    crossing.add("brick_1x4", 0, 0, 3, 90, 4)  # perpendicular on top
    assert perpendicularity_error(crossing) == 0.0

    parallel = Layout(catalog=default_catalog())
    parallel.add("brick_1x4", 0, 0, 0, 0, 4)
    parallel.add("brick_1x4", 0, 0, 3, 0, 4)
    assert perpendicularity_error(parallel) == 1.0

    symmetric = Layout(catalog=default_catalog())
    symmetric.add("brick_1x2", 0, 0, 0, 0, 4)
    symmetric.add("brick_1x2", 4, 0, 0, 0, 4)  # mirror partner about x = 2.5
    assert symmetry_error(symmetric) == 0.0

    lopsided = Layout(catalog=default_catalog())
    lopsided.add("brick_1x2", 0, 0, 0, 0, 4)
    lopsided.add("brick_1x1", 4, 1, 0, 0, 4)  # off both central axes
    assert symmetry_error(lopsided) == 1.0

    assert symmetry_error(Layout(catalog=default_catalog())) == 0.0
    centred = Layout(catalog=default_catalog())
    centred.add("brick_1x2", 0, 0, 0, 0, 4)  # its own mirror partner
    assert symmetry_error(centred) == 0.0


def test_symmetry_v2_uses_one_global_plane():
    # Layer 0 mirrors about x only, layer 1 about y only. Each layer alone is
    # perfect, so the per-layer v1 scores 0.0; no single global plane exists.
    axis_flip = Layout(catalog=default_catalog())
    axis_flip.add("brick_1x2", 0, 0, 0, 0, 4)
    axis_flip.add("brick_1x2", 4, 0, 0, 0, 4)  # x-mirror partner
    axis_flip.add("brick_1x2", 0, 0, 3, 90, 4)
    axis_flip.add("brick_1x2", 0, 3, 3, 90, 4)  # y-mirror partner
    assert layer_symmetry_error(axis_flip) == 0.0
    assert symmetry_error(axis_flip) == 0.5

    # Both layers are x-symmetric about their OWN bbox centres, but layer 1 is
    # shifted two studs along x: v1's per-layer centre hides the drift.
    drift = Layout(catalog=default_catalog())
    drift.add("brick_1x2", 0, 0, 0, 0, 4)
    drift.add("brick_1x2", 4, 0, 0, 0, 4)
    drift.add("brick_1x2", 2, 1, 0, 0, 4)  # centred filler, breaks y-mirror
    drift.add("brick_1x2", 2, 0, 3, 0, 4)
    drift.add("brick_1x2", 6, 0, 3, 0, 4)
    drift.add("brick_1x2", 4, 1, 3, 0, 4)
    assert layer_symmetry_error(drift) == 0.0
    assert symmetry_error(drift) == 1.0


def test_audition_metrics_pin_their_sign_conventions():
    # Alternating-colour checkerboard of 1x1s: every visible junction changes
    # colour. A monochrome pair: none do. Same-brick adjacencies never count.
    checker = Layout(catalog=default_catalog())
    checker.add("brick_1x1", 0, 0, 0, 0, 4)
    checker.add("brick_1x1", 1, 0, 0, 0, 1)
    checker.add("brick_1x1", 0, 1, 0, 0, 1)
    checker.add("brick_1x1", 1, 1, 0, 0, 4)
    assert colour_speckle_error(checker) == 1.0

    slab = Layout(catalog=default_catalog())
    slab.add("brick_1x2", 0, 0, 0, 0, 4)
    slab.add("brick_1x2", 0, 1, 0, 0, 4)
    assert colour_speckle_error(slab) == 0.0
    lone = Layout(catalog=default_catalog())
    lone.add("brick_2x2", 0, 0, 0, 0, 4)
    assert colour_speckle_error(lone) == 0.0

    # Identical stacked footprints taper nowhere; fully disjoint consecutive
    # plate footprints change every column.
    tower = Layout(catalog=default_catalog())
    tower.add("brick_1x2", 0, 0, 0, 0, 4)
    tower.add("brick_1x2", 0, 0, 3, 0, 4)
    assert profile_roughness(tower) == 0.0

    ragged = Layout(catalog=default_catalog())
    ragged.add("plate_1x2", 0, 0, 0, 0, 4)
    ragged.add("plate_1x2", 4, 0, 1, 0, 4)
    assert profile_roughness(ragged) == 1.0
    assert profile_roughness(Layout(catalog=default_catalog())) == 0.0


def test_evaluate_reports_new_terms_and_zero_weights_reproduce_old_total():
    grid = VoxelGrid(codes=np.full((4, 1, 3), 4, dtype=np.int16))
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x4", 0, 0, 0, 0, 4)
    report = evaluate(layout, grid)
    assert report.perpendicularity == 0.0
    assert report.symmetry == 0.0
    assert report.speckle == 0.0
    assert report.profile == 0.0

    weights = ObjectiveWeights(perpendicularity=0.0, symmetry=0.0)
    old_style = evaluate(layout, grid, weights)
    assert old_style.total == pytest.approx(
        weights.cost * old_style.cost
        + weights.stability * old_style.instability
        + weights.aesthetics * old_style.aesthetics
        + weights.colour * old_style.colour_error
    )

    # The audition terms default to weight 0.0, so scoring them must not move
    # the total; giving them weight must.
    speckled = Layout(catalog=default_catalog())
    speckled.add("brick_1x2", 0, 0, 0, 0, 4)
    speckled.add("brick_1x2", 0, 1, 0, 0, 1)
    baseline = evaluate(speckled, grid)
    assert baseline.speckle == 1.0
    weighted = evaluate(speckled, grid, ObjectiveWeights(speckle=1.0))
    assert weighted.total == pytest.approx(baseline.total + baseline.speckle)


def test_objective_dataclasses_preserve_positional_callers() -> None:
    weights = ObjectiveWeights(1.0, 4.0, 0.5, 1.0, 0.25, 0.25, 4.0, 0.8)
    assert weights.bond_alpha1 == 4.0
    assert weights.bond_alpha2 == 0.8
    assert weights.speckle == 0.0
    assert weights.profile == 0.0

    assert [field.name for field in fields(ObjectiveReport)] == [
        "cost",
        "instability",
        "aesthetics",
        "colour_error",
        "perpendicularity",
        "symmetry",
        "total",
        "stability",
        "speckle",
        "profile",
    ]
    evaluated = evaluate(
        Layout(catalog=default_catalog()),
        VoxelGrid(codes=np.zeros((1, 1, 1), dtype=np.int16)),
    )
    legacy = ObjectiveReport(
        evaluated.cost,
        evaluated.instability,
        evaluated.aesthetics,
        evaluated.colour_error,
        evaluated.perpendicularity,
        evaluated.symmetry,
        evaluated.total,
        evaluated.stability,
    )
    assert math.isnan(legacy.speckle)
    assert math.isnan(legacy.profile)


# --- grounded-at-band-time (support-aware placement) ---


def test_grounded_below_distinguishes_floating_supports():
    from legolization.placement.layered.engine import LayerProblem, build_context

    layout = Layout(catalog=default_catalog())
    grounded = layout.add("brick_1x2", 0, 0, 0, 0, 4)
    floater = layout.add("brick_1x2", 4, 0, 3, 0, 4)  # no stud path down
    problem = LayerProblem(
        layer=6,
        height_plates=3,
        columns=frozenset({(0, 0), (4, 0)}),
        colour_of={(0, 0): 4, (4, 0): 4},
    )
    # Give the problem supports directly below each column.
    layout.add("brick_1x2", 0, 0, 3, 0, 4)
    context = build_context(layout, problem)
    del grounded, floater
    assert context.grounded_below is not None
    assert (0, 0) in context.grounded_below  # stacked on the grounded brick
    assert (4, 0) not in context.grounded_below  # sits on the floater


def test_grounding_gain_counts_anchored_columns():
    from legolization.placement.layered.engine import (
        LayerContext,
        Rect2D,
        grounding_gain,
    )

    below = LayerContext(
        support_of={(0, 0): 1, (1, 0): 2, (2, 0): 2},
        gap_columns=frozenset(),
        seams={},
        seam_priority={},
        long_axis_of={},
        stackable_footprints={},
        grounded_below=frozenset({(0, 0)}),
    )
    spanning = Rect2D(x0=0, y0=0, x1=2, y1=0, colour=4)
    floating_only = Rect2D(x0=1, y0=0, x1=2, y1=0, colour=4)
    assert grounding_gain(spanning, below) == 2  # anchors both floaters
    assert grounding_gain(floating_only, below) == 0  # nothing grounded covered
    unsignalled = LayerContext(
        support_of=below.support_of,
        gap_columns=frozenset(),
        seams={},
        seam_priority={},
        long_axis_of={},
        stackable_footprints={},
    )
    assert grounding_gain(spanning, unsignalled) == 0


def test_kollsker_ground_weight_never_changes_counts():
    from legolization.placement.layered.kollsker import KollskerStrategy

    codes = np.full((6, 2, 6), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    counts = {}
    for weight in (0.0, 2.0):
        strategy = KollskerStrategy(ground_weight=weight)
        layout = strategy.place(grid, rng=np.random.default_rng(3))
        counts[weight] = len(layout)
    # Stage 2 pins the count at N*: grounding only re-spends the
    # equal-count freedom.
    assert counts[0.0] == counts[2.0]
