"""Layer engine and the four per-layer tiling strategies."""

import numpy as np
import pytest

from legolization.catalog import default_catalog
from legolization.grid import EMPTY, IGNORE, VoxelGrid
from legolization.layout import Layout
from legolization.placement.aesthetics import perpendicularity_error, symmetry_error
from legolization.placement.base import ObjectiveWeights, _seam_alignment, evaluate
from legolization.placement.layered import (
    BeautyStrategy,
    BeautyWeights,
    BondStrategy,
    FastStrategy,
    SmGaConfig,
    SmGaStrategy,
)
from legolization.placement.layered.engine import (
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

    def fake_next_generation(  # noqa: PLR0913 - mirrors the production method
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


def test_fast_prefers_bigger_bricks():
    codes = np.full((8, 2, 3), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    layout = FastStrategy().place(grid, rng=np.random.default_rng(0))
    areas = sorted(len({(x, y) for x, y, _ in layout.cells_of(b)}) for b in layout)
    assert areas[-1] == 16  # a 2x8 emerged from the all-1x1 start


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


def test_evaluate_reports_new_terms_and_zero_weights_reproduce_old_total():
    grid = VoxelGrid(codes=np.full((4, 1, 3), 4, dtype=np.int16))
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x4", 0, 0, 0, 0, 4)
    report = evaluate(layout, grid)
    assert report.perpendicularity == 0.0
    assert report.symmetry == 0.0

    weights = ObjectiveWeights(perpendicularity=0.0, symmetry=0.0)
    old_style = evaluate(layout, grid, weights)
    assert old_style.total == pytest.approx(
        weights.cost * old_style.cost
        + weights.stability * old_style.instability
        + weights.aesthetics * old_style.aesthetics
        + weights.colour * old_style.colour_error
    )
