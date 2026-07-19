"""Per-layer Kollsker set-partitioning MILP strategy."""

import numpy as np
import pytest

from legolization.catalog import default_catalog
from legolization.grid import EMPTY, VoxelGrid
from legolization.layout import Layout
from legolization.pipeline import PipelineConfig
from legolization.placement.layered.bond import BondStrategy
from legolization.placement.layered.engine import (
    LayerProblem,
    build_context,
    enumerate_layer_rects,
    slab_decompose,
)
from legolization.placement.layered.kollsker import KollskerStrategy, _components
from legolization.placement.registry import make_strategy, strategy_names


def _layer_problem(columns: set[tuple[int, int]], colour: int = 4) -> LayerProblem:
    nx = max(x for x, _ in columns) + 1
    ny = max(y for _, y in columns) + 1
    codes = np.full((nx, ny, 3), EMPTY, dtype=np.int16)
    for x, y in columns:
        codes[x, y, :] = colour
    grid = VoxelGrid(codes=codes)
    (problem,) = slab_decompose(grid)
    return problem


def _min_parts_brute(problem, catalog) -> int:
    """Exhaustive DFS exact-cover minimum for tiny shapes."""
    rects = enumerate_layer_rects(problem, problem.columns, catalog)
    best = len(problem.columns) + 1

    def search(free: frozenset, used: int) -> None:
        nonlocal best
        if used >= best:
            return
        if not free:
            best = used
            return
        seed = min(free)
        for rect in rects:
            columns = rect.columns()
            if seed in columns and columns <= free:
                search(free - columns, used + 1)

    search(problem.columns, 0)
    return best


@pytest.mark.parametrize(
    "columns",
    [
        {(x, y) for x in range(4) for y in range(3)},  # 4x3 slab
        {(x, 0) for x in range(7)},  # 7-run (needs 2 parts)
        {(0, 0), (1, 0), (2, 0), (2, 1), (2, 2), (1, 2)},  # S-bend
        {(x, y) for x in range(3) for y in range(3)} - {(1, 1)},  # ring
    ],
)
def test_layer_tilings_are_minimum_cardinality(columns):
    catalog = default_catalog()
    problem = _layer_problem(columns)
    strategy = KollskerStrategy(catalog=catalog)
    layout = Layout(catalog=catalog)
    context = build_context(layout, problem)
    rng = np.random.default_rng(0)
    rects = strategy.tile(problem, context, rng=rng, deadline=None)
    assert {c for r in rects for c in r.columns()} == problem.columns
    assert sum(r.area for r in rects) == len(problem.columns)
    assert len(rects) == _min_parts_brute(problem, catalog)


def test_never_worse_than_bond_on_random_layers():
    catalog = default_catalog()
    rng = np.random.default_rng(7)
    for _ in range(5):
        nx, ny = 6, 5
        columns = {(x, y) for x in range(nx) for y in range(ny) if rng.random() < 0.75}
        if not columns:
            continue
        problem = _layer_problem(columns)
        layout = Layout(catalog=default_catalog())
        context = build_context(layout, problem)
        milp_rects = KollskerStrategy(catalog=catalog).tile(
            problem, context, rng=np.random.default_rng(0), deadline=None
        )
        bond_rects = BondStrategy(catalog=catalog).tile(
            problem, context, rng=np.random.default_rng(0), deadline=None
        )
        assert len(milp_rects) <= len(bond_rects)


def test_stage_two_staggers_a_two_course_wall():
    # Bottom course: 4+4+2 bricks with seams at 3|4 and 7|8. The 10-long
    # top course needs two parts; stage 2 must place the internal border
    # away from both below seams (stacked seams weaken the wall).
    catalog = default_catalog()
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x4", 0, 0, 0, 0, 4)
    layout.add("brick_1x4", 4, 0, 0, 0, 4)
    layout.add("brick_1x2", 8, 0, 0, 0, 4)
    codes = np.full((10, 1, 6), EMPTY, dtype=np.int16)
    codes[:, :, :] = 4
    grid = VoxelGrid(codes=codes)
    problems = slab_decompose(grid)
    top = next(p for p in problems if p.layer == 3)
    context = build_context(layout, top)
    rects = KollskerStrategy(catalog=catalog).tile(
        top, context, rng=np.random.default_rng(0), deadline=None
    )
    assert len(rects) == 2  # minimum for a 10-run with 8-long parts
    borders = sorted(r.x0 for r in rects)[1:]
    assert borders
    assert all(b not in (4, 8) for b in borders)


def test_falls_back_to_bond_on_milp_failure(monkeypatch):
    from types import SimpleNamespace

    import legolization.placement.layered.kollsker as kollsker_mod

    def failing_milp(*args: object, **kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(success=False, x=None, fun=None)

    monkeypatch.setattr(kollsker_mod, "milp", failing_milp)
    catalog = default_catalog()
    problem = _layer_problem({(x, y) for x in range(4) for y in range(2)})
    layout = Layout(catalog=default_catalog())
    context = build_context(layout, problem)
    rects = KollskerStrategy(catalog=catalog).tile(
        problem, context, rng=np.random.default_rng(0), deadline=None
    )
    assert {c for r in rects for c in r.columns()} == problem.columns


def test_candidate_blowup_falls_back_to_bond():
    catalog = default_catalog()
    problem = _layer_problem({(x, y) for x in range(4) for y in range(3)})
    layout = Layout(catalog=default_catalog())
    context = build_context(layout, problem)
    strategy = KollskerStrategy(catalog=catalog, candidate_limit=1)
    rects = strategy.tile(problem, context, rng=np.random.default_rng(0), deadline=None)
    assert {c for r in rects for c in r.columns()} == problem.columns


def test_registered_and_constructed_from_config():
    assert "kollsker" in strategy_names()
    strategy = make_strategy(
        "kollsker",
        catalog=default_catalog(),
        config=PipelineConfig(milp_layer_time_s=3.0, milp_bond_weight=2.0),
    )
    assert isinstance(strategy, KollskerStrategy)
    assert strategy.layer_time_s == 3.0
    assert strategy.bond_weight == 2.0


def test_repeat_runs_are_identical():
    codes = np.full((6, 6, 6), EMPTY, dtype=np.int16)
    codes[:, :, :3] = 4
    codes[1:5, 1:5, 3:] = 14
    grid = VoxelGrid(codes=codes)
    catalog = default_catalog()

    def run_once() -> list[tuple]:
        strategy = KollskerStrategy(catalog=catalog)
        layout = strategy.place(grid, rng=np.random.default_rng(3))
        return sorted(
            (b.part_key, b.x, b.y, b.layer, b.yaw, b.colour_code) for b in layout
        )

    assert run_once() == run_once()


def test_components_split_and_order():
    parts = _components(frozenset({(0, 0), (1, 0), (5, 5), (5, 6), (9, 9)}))
    assert parts == [[(0, 0), (1, 0)], [(5, 5), (5, 6)], [(9, 9)]]
