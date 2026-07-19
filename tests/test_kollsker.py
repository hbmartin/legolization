"""Per-layer Kollsker set-partitioning MILP strategy."""

import numpy as np
import pytest

from legolization.catalog import Catalog, default_catalog
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


def _min_parts_brute(problem: LayerProblem, catalog: Catalog) -> int:
    """Exhaustive DFS exact-cover minimum for tiny shapes."""
    rects = enumerate_layer_rects(problem, problem.columns, catalog)
    best = len(problem.columns) + 1

    def search(free: frozenset[tuple[int, int]], used: int) -> None:
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
def test_layer_tilings_are_minimum_cardinality(columns: set[tuple[int, int]]) -> None:
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


def test_never_worse_than_bond_on_random_layers() -> None:
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


def test_stage_two_staggers_a_two_course_wall() -> None:
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


def test_falls_back_to_bond_on_milp_failure(monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_candidate_blowup_falls_back_to_bond() -> None:
    catalog = default_catalog()
    problem = _layer_problem({(x, y) for x in range(4) for y in range(3)})
    layout = Layout(catalog=default_catalog())
    context = build_context(layout, problem)
    strategy = KollskerStrategy(catalog=catalog, candidate_limit=1)
    rects = strategy.tile(problem, context, rng=np.random.default_rng(0), deadline=None)
    assert {c for r in rects for c in r.columns()} == problem.columns


def test_registered_and_constructed_from_config() -> None:
    assert "kollsker" in strategy_names()
    strategy = make_strategy(
        "kollsker",
        catalog=default_catalog(),
        config=PipelineConfig(milp_layer_time_s=3.0, milp_bond_weight=2.0),
    )
    assert isinstance(strategy, KollskerStrategy)
    assert strategy.layer_time_s == 3.0
    assert strategy.bond_weight == 2.0


def test_repeat_runs_are_identical() -> None:
    codes = np.full((6, 6, 6), EMPTY, dtype=np.int16)
    codes[:, :, :3] = 4
    codes[1:5, 1:5, 3:] = 14
    grid = VoxelGrid(codes=codes)
    catalog = default_catalog()

    def run_once() -> list[tuple[str, int, int, int, int, int]]:
        strategy = KollskerStrategy(catalog=catalog)
        layout = strategy.place(grid, rng=np.random.default_rng(3))
        return sorted(
            (b.part_key, b.x, b.y, b.layer, b.yaw, b.colour_code) for b in layout
        )

    assert run_once() == run_once()


def test_components_split_and_order() -> None:
    parts = _components(frozenset({(0, 0), (1, 0), (5, 5), (5, 6), (9, 9)}))
    assert parts == [[(0, 0), (1, 0)], [(5, 5), (5, 6)], [(9, 9)]]


def test_stage_two_deadline_recomputed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Stage 1 can consume most of the deadline budget; stage 2's
    # time_limit must reflect what actually remains, not the pre-stage-1
    # snapshot (review finding on PR #17).
    from types import SimpleNamespace
    from typing import cast

    import legolization.placement.layered.kollsker as kollsker_mod

    clock = {"now": 100.0}
    monkeypatch.setattr(kollsker_mod.time, "monotonic", lambda: clock["now"])

    captured: list[float] = []

    def fake_milp(*args: object, **kwargs: object) -> SimpleNamespace:
        options = cast("dict[str, float]", kwargs["options"])
        captured.append(options["time_limit"])
        clock["now"] += 6.0  # each stage burns 6 s of the budget
        costs = kwargs["c"]
        assert isinstance(costs, np.ndarray)
        x = np.zeros(len(costs))
        x[0] = 1.0
        return SimpleNamespace(success=True, x=x, fun=1.0)

    monkeypatch.setattr(kollsker_mod, "milp", fake_milp)
    catalog = default_catalog()
    problem = _layer_problem({(0, 0)})
    layout = Layout(catalog=catalog)
    context = build_context(layout, problem)
    strategy = KollskerStrategy(catalog=catalog, layer_time_s=60.0)
    strategy.tile(
        problem,
        context,
        rng=np.random.default_rng(0),
        deadline=clock["now"] + 10.0,  # 10 s total budget
    )
    assert len(captured) == 2
    assert captured[0] == pytest.approx(10.0)  # full remaining budget
    assert captured[1] == pytest.approx(4.0)  # recomputed after stage 1


def test_milp_exception_falls_back_to_bond(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A crashing solver must become the bond fallback, not a failed
    # candidate (PR #17 review).
    import legolization.placement.layered.kollsker as kollsker_mod

    def crashing_milp(*args: object, **kwargs: object) -> object:
        msg = "HiGHS crashed"
        raise RuntimeError(msg)

    monkeypatch.setattr(kollsker_mod, "milp", crashing_milp)
    catalog = default_catalog()
    problem = _layer_problem({(x, y) for x in range(4) for y in range(2)})
    layout = Layout(catalog=catalog)
    context = build_context(layout, problem)
    rects = KollskerStrategy(catalog=catalog).tile(
        problem, context, rng=np.random.default_rng(0), deadline=None
    )
    assert {c for r in rects for c in r.columns()} == problem.columns


def test_non_finite_tuning_rejected() -> None:
    with pytest.raises(ValueError, match="bond_weight"):
        KollskerStrategy(catalog=default_catalog(), bond_weight=float("nan"))
    with pytest.raises(ValueError, match="layer_time_s"):
        KollskerStrategy(catalog=default_catalog(), layer_time_s=float("inf"))
    with pytest.raises(ValueError, match="layer_time_s"):
        KollskerStrategy(catalog=default_catalog(), layer_time_s=0.0)


def test_expired_deadline_skips_the_milp_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import legolization.placement.layered.kollsker as kollsker_mod

    calls = {"milp": 0}

    def counting_milp(*args: object, **kwargs: object) -> object:
        calls["milp"] += 1
        msg = "must not be called"
        raise AssertionError(msg)

    monkeypatch.setattr(kollsker_mod, "milp", counting_milp)
    monkeypatch.setattr(kollsker_mod.time, "monotonic", lambda: 1000.0)
    catalog = default_catalog()
    problem = _layer_problem({(x, y) for x in range(3) for y in range(2)})
    layout = Layout(catalog=catalog)
    context = build_context(layout, problem)
    rects = KollskerStrategy(catalog=catalog).tile(
        problem,
        context,
        rng=np.random.default_rng(0),
        deadline=999.0,  # already in the past
    )
    assert calls["milp"] == 0  # straight to the bond fallback
    assert {c for r in rects for c in r.columns()} == problem.columns
