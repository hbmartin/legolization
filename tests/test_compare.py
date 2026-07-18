"""Strategy sweep: candidate scoring, lexicographic selection, run_all."""

import json
import string
from dataclasses import replace

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

import legolization.compare
from legolization.compare import (
    Candidate,
    CandidateMetrics,
    _candidate_config,
    candidate_metrics,
    run_all,
    select_best,
)
from legolization.grid import EMPTY, VoxelGrid
from legolization.main import main
from legolization.pipeline import PipelineConfig, run
from legolization.placement.base import ObjectiveWeights
from legolization.stability import SolverConfig

# --- helpers -------------------------------------------------------------


_BASE_METRICS = CandidateMetrics(
    buildable=True,
    stable=True,
    component_count=1,
    floating_count=0,
    objective_total=1.0,
    maximin_feasible=True,
    maximin_capacity=0.5,
    max_score=0.1,
    min_capacity=0.5,
    brick_count=10,
    mass_g=10.0,
    step_count=3,
    cost=0.5,
    aesthetics=0.0,
    colour_error=0.0,
    perpendicularity=0.0,
    symmetry=0.0,
)


def _metrics(**overrides) -> CandidateMetrics:
    return replace(_BASE_METRICS, **overrides)


def _candidate(name: str, *, error: str | None = None, **overrides) -> Candidate:
    if error is not None:
        return Candidate(strategy=name, seconds=0.1, error=error)
    return Candidate(strategy=name, seconds=0.1, metrics=_metrics(**overrides))


def _box_grid() -> VoxelGrid:
    codes = np.full((4, 3, 2), 4, dtype=np.int16)
    return VoxelGrid(codes=codes)


# --- select_best: deterministic units ------------------------------------


def test_empty_input_has_no_winner():
    report = select_best([])
    assert report.winner is None
    assert report.candidates == ()
    assert "failed" in report.reason


def test_buildable_gate_dominates_objective():
    # An unbuildable layout with a spectacular objective must still lose.
    shaky = _candidate("shaky", buildable=False, objective_total=0.01)
    solid = _candidate("solid", buildable=True, objective_total=5.0)
    report = select_best([shaky, solid])
    assert report.winner is solid
    assert "buildable" in report.reason


def test_lower_objective_wins_among_buildable():
    a = _candidate("a", objective_total=2.0)
    b = _candidate("b", objective_total=1.0)
    assert select_best([a, b]).winner is b


def test_capacity_breaks_objective_ties():
    weak = _candidate("weak", objective_total=1.0, maximin_capacity=0.2)
    strong = _candidate("strong", objective_total=1.0, maximin_capacity=0.9)
    assert select_best([weak, strong]).winner is strong


def test_brick_count_breaks_capacity_ties():
    many = _candidate("many", brick_count=50)
    few = _candidate("few", brick_count=20)
    assert select_best([many, few]).winner is few


def test_name_breaks_full_ties():
    twin_b = _candidate("b")
    twin_a = _candidate("a")
    assert select_best([twin_b, twin_a]).winner is twin_a


def test_unbuildable_fallback_orders_by_components_then_stress():
    shattered = _candidate(
        "shattered", buildable=False, component_count=3, max_score=0.2
    )
    stressed = _candidate("stressed", buildable=False, component_count=1, max_score=0.9)
    calm = _candidate("calm", buildable=False, component_count=1, max_score=0.4)
    report = select_best([shattered, stressed, calm])
    assert report.winner is calm
    assert "no candidate is buildable" in report.reason


def test_errored_candidates_are_reported_but_never_win():
    broken = _candidate("broken", error="RuntimeError: kaboom")
    ok = _candidate("ok", buildable=False, component_count=4, max_score=1.0)
    report = select_best([broken, ok])
    assert report.winner is ok
    assert broken in report.candidates


def test_all_errored_yields_no_winner():
    report = select_best([_candidate("x", error="boom"), _candidate("y", error="pow")])
    assert report.winner is None
    assert report.reason == "every strategy failed"
    assert len(report.candidates) == 2


# --- select_best: hypothesis properties ----------------------------------

_metrics_st = st.builds(
    CandidateMetrics,
    buildable=st.booleans(),
    stable=st.booleans(),
    component_count=st.integers(min_value=1, max_value=5),
    floating_count=st.integers(min_value=0, max_value=5),
    objective_total=st.floats(min_value=0, max_value=100, allow_nan=False),
    maximin_feasible=st.booleans(),
    maximin_capacity=st.floats(min_value=-10, max_value=10, allow_nan=False),
    max_score=st.floats(min_value=0, max_value=2, allow_nan=False),
    min_capacity=st.floats(min_value=-10, max_value=10, allow_nan=False),
    brick_count=st.integers(min_value=1, max_value=500),
    mass_g=st.floats(min_value=0, max_value=1000, allow_nan=False),
    step_count=st.integers(min_value=0, max_value=100),
    cost=st.floats(min_value=0, max_value=10, allow_nan=False),
    aesthetics=st.floats(min_value=0, max_value=1, allow_nan=False),
    colour_error=st.floats(min_value=0, max_value=1, allow_nan=False),
    perpendicularity=st.floats(min_value=0, max_value=1, allow_nan=False),
    symmetry=st.floats(min_value=0, max_value=1, allow_nan=False),
)


@st.composite
def _candidate_lists(draw) -> list[Candidate]:
    names = draw(
        st.lists(
            st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=8),
            min_size=1,
            max_size=8,
            unique=True,
        )
    )
    return [
        Candidate(strategy=name, seconds=0.1, error="boom")
        if draw(st.booleans())
        else Candidate(strategy=name, seconds=0.1, metrics=draw(_metrics_st))
        for name in names
    ]


@given(st.data())
def test_selection_is_order_independent(data):
    candidates = data.draw(_candidate_lists())
    shuffled = data.draw(st.permutations(candidates))
    assert select_best(shuffled) == select_best(candidates)


@given(_candidate_lists())
def test_winner_is_gated_and_optimal(candidates):
    report = select_best(candidates)
    scored = [
        (c, c.metrics) for c in candidates if c.error is None and c.metrics is not None
    ]
    if not scored:
        assert report.winner is None
        return
    winner = report.winner
    assert winner is not None
    assert winner in [c for c, _ in scored]
    buildable = [(c, m) for c, m in scored if m.buildable]
    if not buildable:
        return
    # Differential oracle: the winner carries the minimal ranking key.
    winner_metrics = winner.metrics
    assert winner_metrics is not None
    assert winner_metrics.buildable
    winner_key = (
        winner_metrics.objective_total,
        -winner_metrics.maximin_capacity,
        winner_metrics.brick_count,
        winner.strategy,
    )
    assert winner_key == min(
        (m.objective_total, -m.maximin_capacity, m.brick_count, c.strategy)
        for c, m in buildable
    )


@given(_candidate_lists())
def test_report_lists_everyone_and_serializes(candidates):
    report = select_best(candidates)
    assert [c.strategy for c in report.candidates] == sorted(
        c.strategy for c in candidates
    )
    payload = json.loads(json.dumps(report.to_dict()))
    assert len(payload["candidates"]) == len(candidates)
    winner_name = report.winner.strategy if report.winner is not None else None
    assert payload["winner"] == winner_name


# --- candidate_metrics on a real pipeline result --------------------------


def test_candidate_metrics_on_real_result():
    result = run(grid=_box_grid(), config=PipelineConfig(seed=0))
    metrics = candidate_metrics(
        result,
        weights=ObjectiveWeights(),
        solver_config=SolverConfig(),
    )
    assert metrics.brick_count == result.brick_count == len(result.layout)
    assert metrics.buildable == result.buildable
    assert metrics.stable == result.stability.stable
    assert metrics.maximin_feasible
    assert 0.0 <= metrics.max_score <= 1.0
    assert metrics.objective_total > 0.0


# --- run_all: sequential, error isolation, parallel ------------------------


def test_run_all_sequential_two_strategies():
    lines: list[str] = []
    candidates = run_all(
        _box_grid(),
        PipelineConfig(seed=0),
        jobs=1,
        names=("greedy", "bond"),
        progress=lines.append,
    )
    assert [c.strategy for c in candidates] == ["bond", "greedy"]
    assert all(c.ok for c in candidates)
    assert len(lines) == 2
    report = select_best(candidates)
    winner = report.winner
    assert winner is not None
    assert winner.metrics is not None
    assert winner.metrics.buildable


def test_one_failing_strategy_does_not_kill_sweep(monkeypatch):
    real_run = legolization.compare.run

    def flaky_run(*, grid, config) -> object:
        if config.strategy == "bond":
            raise RuntimeError("kaboom")
        return real_run(grid=grid, config=config)

    monkeypatch.setattr(legolization.compare, "run", flaky_run)
    candidates = run_all(
        _box_grid(),
        PipelineConfig(seed=0),
        jobs=1,
        names=("bond", "greedy"),
    )
    bond, greedy = candidates
    assert bond.error == "RuntimeError: kaboom"
    assert not bond.ok
    assert greedy.ok
    report = select_best(candidates)
    winner = report.winner
    assert winner is not None
    assert winner.strategy == "greedy"


@pytest.mark.parametrize("strategy", ["greedy", "bond"])
def test_candidate_config_strips_progress_and_sets_strategy(strategy):
    config = PipelineConfig(progress=print, time_budget_s=9.0)
    clone = _candidate_config(config, strategy=strategy, timeout_s=4.0)
    assert clone.strategy == strategy
    assert clone.progress is None
    assert clone.time_budget_s == 4.0
    assert clone.seed == config.seed


def test_parallel_matches_sequential():
    grid = _box_grid()
    config = PipelineConfig(seed=0)
    sequential = run_all(grid, config, jobs=1, names=("greedy", "bond"))
    parallel = run_all(grid, config, jobs=2, names=("greedy", "bond"))
    assert [c.strategy for c in sequential] == [c.strategy for c in parallel]
    assert [c.metrics for c in sequential] == [c.metrics for c in parallel]
    seq_winner = select_best(sequential).winner
    par_winner = select_best(parallel).winner
    assert seq_winner is not None
    assert par_winner is not None
    assert seq_winner.strategy == par_winner.strategy
    assert select_best(sequential).reason == select_best(parallel).reason


# --- CLI ------------------------------------------------------------------


def test_cli_sweep_end_to_end(tmp_path, capsys):
    npy = tmp_path / "box.npy"
    np.save(npy, np.full((4, 3, 2), 4, dtype=np.int16))
    out = tmp_path / "box.ldr"
    report_path = tmp_path / "report.json"
    candidates_dir = tmp_path / "candidates"
    code = main(
        [
            str(npy),
            "-o",
            str(out),
            "--strategy",
            "all",
            "--jobs",
            "1",
            "--time-budget",
            "5",
            "--report",
            str(report_path),
            "--keep-candidates",
            str(candidates_dir),
        ]
    )
    assert code == 0
    assert out.exists()
    payload = json.loads(report_path.read_text())
    assert len(payload["candidates"]) == 6
    names = {c["strategy"] for c in payload["candidates"]}
    assert payload["winner"] in names
    assert payload["buildable"] is True
    succeeded = [c for c in payload["candidates"] if c["error"] is None]
    assert {p.name for p in candidates_dir.iterdir()} == {
        f"box.{c['strategy']}.ldr" for c in succeeded
    }
    captured = capsys.readouterr()
    assert f"selected {payload['winner']}" in captured.out
    assert "wrote" in captured.out


def test_cli_sweep_flags_require_strategy_all(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main([str(tmp_path / "x.npy"), "--jobs", "2"])
    assert excinfo.value.code == 2
    assert "--strategy all" in capsys.readouterr().err


@pytest.mark.slow
def test_full_sweep_selects_buildable_winner():
    codes = np.full((6, 6, 3), EMPTY, dtype=np.int16)
    for z in range(3):
        lo, hi = z, 6 - z
        codes[lo:hi, lo:hi, z] = 4
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    candidates = run_all(grid, PipelineConfig(seed=0, time_budget_s=10.0), jobs=0)
    assert len(candidates) == 6
    winner = select_best(candidates).winner
    assert winner is not None
    assert winner.metrics is not None
    assert winner.metrics.buildable
