"""Strategy sweep: candidate scoring, lexicographic selection, run_all."""

import json
import string
from concurrent.futures import Future
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

import legolization.compare
import legolization.main as main_module
from legolization.compare import (
    Candidate,
    CandidateMetrics,
    _candidate_config,
    _collect,
    candidate_metrics,
    run_all,
    select_best,
)
from legolization.grid import EMPTY, VoxelGrid
from legolization.main import main
from legolization.pipeline import PipelineConfig, PipelineResult, run
from legolization.placement.base import ObjectiveWeights
from legolization.placement.registry import strategy_names
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


def _metrics(**overrides: object) -> CandidateMetrics:
    return replace(_BASE_METRICS, **cast("dict[str, Any]", overrides))


def _candidate(
    name: str,
    *,
    error: str | None = None,
    **overrides: float,
) -> Candidate:
    if error is not None:
        return Candidate(strategy=name, seconds=0.1, error=error)
    return Candidate(strategy=name, seconds=0.1, metrics=_metrics(**overrides))


def _box_grid() -> VoxelGrid:
    codes = np.full((4, 3, 2), 4, dtype=np.int16)
    return VoxelGrid(codes=codes)


# --- select_best: deterministic units ------------------------------------


def test_empty_input_has_no_winner() -> None:
    report = select_best([])
    assert report.winner is None
    assert report.candidates == ()
    assert "failed" in report.reason


def test_buildable_gate_dominates_objective() -> None:
    # An unbuildable layout with a spectacular objective must still lose.
    shaky = _candidate("shaky", buildable=False, objective_total=0.01)
    solid = _candidate("solid", buildable=True, objective_total=5.0)
    report = select_best([shaky, solid])
    assert report.winner is solid
    assert "buildable" in report.reason


def test_lower_objective_wins_among_buildable() -> None:
    a = _candidate("a", objective_total=2.0)
    b = _candidate("b", objective_total=1.0)
    assert select_best([a, b]).winner is b


def test_capacity_breaks_objective_ties() -> None:
    weak = _candidate("weak", objective_total=1.0, maximin_capacity=0.2)
    strong = _candidate("strong", objective_total=1.0, maximin_capacity=0.9)
    assert select_best([weak, strong]).winner is strong


def test_brick_count_breaks_capacity_ties() -> None:
    many = _candidate("many", brick_count=50)
    few = _candidate("few", brick_count=20)
    assert select_best([many, few]).winner is few


def test_name_breaks_full_ties() -> None:
    twin_b = _candidate("b")
    twin_a = _candidate("a")
    assert select_best([twin_b, twin_a]).winner is twin_a


def test_unbuildable_fallback_orders_by_components_then_stress() -> None:
    shattered = _candidate(
        "shattered", buildable=False, component_count=3, max_score=0.2
    )
    stressed = _candidate("stressed", buildable=False, component_count=1, max_score=0.9)
    calm = _candidate("calm", buildable=False, component_count=1, max_score=0.4)
    report = select_best([shattered, stressed, calm])
    assert report.winner is calm
    assert "no candidate is buildable" in report.reason


def test_errored_candidates_are_reported_but_never_win() -> None:
    broken = _candidate("broken", error="RuntimeError: kaboom")
    ok = _candidate("ok", buildable=False, component_count=4, max_score=1.0)
    report = select_best([broken, ok])
    assert report.winner is ok
    assert broken in report.candidates


def test_all_errored_yields_no_winner() -> None:
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
def _candidate_lists(draw: st.DrawFn) -> list[Candidate]:
    keys = draw(
        st.lists(
            st.tuples(
                st.text(alphabet=string.ascii_lowercase, min_size=1, max_size=8),
                st.integers(min_value=0, max_value=3),
            ),
            min_size=1,
            max_size=8,
            unique=True,
        )
    )
    return [
        Candidate(strategy=name, seconds=0.1, seed=seed, error="boom")
        if draw(st.booleans())
        else Candidate(strategy=name, seconds=0.1, seed=seed, metrics=draw(_metrics_st))
        for name, seed in keys
    ]


@given(st.data())
def test_selection_is_order_independent(data: st.DataObject) -> None:
    candidates = data.draw(_candidate_lists())
    shuffled = data.draw(st.permutations(candidates))
    assert select_best(shuffled) == select_best(candidates)


@given(_candidate_lists())
def test_winner_is_gated_and_optimal(candidates: list[Candidate]) -> None:
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
        winner.seed,
    )
    assert winner_key == min(
        (m.objective_total, -m.maximin_capacity, m.brick_count, c.strategy, c.seed)
        for c, m in buildable
    )


@given(_candidate_lists())
def test_report_lists_everyone_and_serializes(candidates: list[Candidate]) -> None:
    report = select_best(candidates)
    assert [(c.strategy, c.seed) for c in report.candidates] == sorted(
        (c.strategy, c.seed) for c in candidates
    )
    payload = json.loads(json.dumps(report.to_dict()))
    assert len(payload["candidates"]) == len(candidates)
    winner_name = report.winner.strategy if report.winner is not None else None
    assert payload["winner"] == winner_name
    winner_seed = report.winner.seed if report.winner is not None else None
    assert payload["winner_seed"] == winner_seed
    assert all("seed" in entry for entry in payload["candidates"])


# --- candidate_metrics on a real pipeline result --------------------------


def test_candidate_metrics_on_real_result() -> None:
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


def test_candidate_metrics_uses_configured_maximin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    config = SolverConfig(torque_z=True, ground_pull=False)
    helper = Mock(wraps=legolization.compare.build_model_from_config)
    monkeypatch.setattr(legolization.compare, "build_model_from_config", helper)
    candidate_metrics(
        run(grid=_box_grid(), config=PipelineConfig(seed=0)),
        weights=ObjectiveWeights(),
        solver_config=config,
    )
    helper.assert_called_once()
    assert helper.call_args.args[1] is config


# --- run_all: sequential, error isolation, parallel ------------------------


def test_run_all_sequential_two_strategies() -> None:
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


def test_one_failing_strategy_does_not_kill_sweep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_run = legolization.compare.run

    def flaky_run(*, grid: VoxelGrid, config: PipelineConfig) -> PipelineResult:
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


def test_collect_converts_future_exception_to_candidate() -> None:
    future: Future[Candidate] = Future()
    future.set_exception(TypeError("cannot unpickle result"))

    candidate = _collect(future, strategy="bond", seed=2)

    assert candidate.strategy == "bond"
    assert candidate.seed == 2
    assert candidate.error == (
        "failed to retrieve result: TypeError: cannot unpickle result"
    )


@pytest.mark.parametrize("strategy", ["greedy", "bond"])
def test_candidate_config_strips_progress_and_sets_strategy(strategy: str) -> None:
    config = PipelineConfig(progress=print, time_budget_s=9.0)
    clone = _candidate_config(config, strategy=strategy, seed=3, timeout_s=4.0)
    assert clone.strategy == strategy
    assert clone.progress is None
    assert clone.time_budget_s == 4.0
    assert clone.seed == 3


def test_parallel_matches_sequential() -> None:
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


def test_run_all_exact_skip_and_completion_callback() -> None:
    completed: list[tuple[str, int]] = []
    candidates = run_all(
        _box_grid(),
        PipelineConfig(seed=0),
        jobs=1,
        names=("greedy", "bond"),
        skip={("greedy", 0)},
        on_complete=lambda candidate: completed.append(
            (candidate.strategy, candidate.seed)
        ),
    )
    assert [(candidate.strategy, candidate.seed) for candidate in candidates] == [
        ("bond", 0)
    ]
    assert completed == [("bond", 0)]


def test_parallel_callback_runs_in_completion_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    futures: list[Future[Candidate]] = []

    class FakeExecutor:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def submit(
            self,
            _function: object,
            _grid: VoxelGrid,
            config: PipelineConfig,
        ) -> Future[Candidate]:
            future: Future[Candidate] = Future()
            future.set_result(
                Candidate(
                    strategy=config.strategy,
                    seconds=0.1,
                    seed=config.seed,
                    metrics=_metrics(),
                )
            )
            futures.append(future)
            return future

        def shutdown(self, **_kwargs: object) -> None:
            pass

    monkeypatch.setattr(legolization.compare, "ProcessPoolExecutor", FakeExecutor)
    monkeypatch.setattr(
        legolization.compare,
        "as_completed",
        lambda _pending, timeout: iter(reversed(futures)),
    )
    configs = {
        ("greedy", 0): PipelineConfig(strategy="greedy", seed=0),
        ("bond", 0): PipelineConfig(strategy="bond", seed=0),
    }
    completed: list[str] = []
    legolization.compare._run_parallel(  # noqa: SLF001 - completion unit seam
        _box_grid(),
        configs,
        workers=2,
        timeout_s=2.0,
        progress=None,
        on_complete=lambda candidate: completed.append(candidate.strategy),
    )
    assert completed == ["bond", "greedy"]


# --- multi-seed restarts ---------------------------------------------------


def test_seed_breaks_full_ties() -> None:
    late = Candidate(strategy="a", seconds=0.1, seed=1, metrics=_metrics())
    early = Candidate(strategy="a", seconds=0.1, seed=0, metrics=_metrics())
    assert select_best([late, early]).winner is early


def test_run_all_seeds_produces_strategy_seed_grid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, int]] = []
    real_run = legolization.compare.run

    def spy_run(*, grid: VoxelGrid, config: PipelineConfig) -> PipelineResult:
        seen.append((config.strategy, config.seed))
        return real_run(grid=grid, config=config)

    monkeypatch.setattr(legolization.compare, "run", spy_run)
    candidates = run_all(
        _box_grid(),
        PipelineConfig(seed=9),
        jobs=1,
        names=("greedy", "bond"),
        seeds=(0, 1),
    )
    assert [(c.strategy, c.seed) for c in candidates] == [
        ("bond", 0),
        ("bond", 1),
        ("greedy", 0),
        ("greedy", 1),
    ]
    assert sorted(seen) == [
        ("bond", 0),
        ("bond", 1),
        ("greedy", 0),
        ("greedy", 1),
    ]


def test_run_all_default_seeds_uses_config_seed() -> None:
    candidates = run_all(
        _box_grid(),
        PipelineConfig(seed=5),
        jobs=1,
        names=("greedy",),
        seeds=None,
    )
    assert [(c.strategy, c.seed) for c in candidates] == [("greedy", 5)]


def test_run_all_dedupes_seeds() -> None:
    candidates = run_all(
        _box_grid(),
        PipelineConfig(seed=0),
        jobs=1,
        names=("greedy",),
        seeds=(1, 1, 0, 1),
    )
    assert [(c.strategy, c.seed) for c in candidates] == [
        ("greedy", 0),
        ("greedy", 1),
    ]


def test_parallel_matches_sequential_with_seeds() -> None:
    grid = _box_grid()
    config = PipelineConfig(seed=0)
    sequential = run_all(grid, config, jobs=1, names=("greedy", "bond"), seeds=(0, 1))
    parallel = run_all(grid, config, jobs=4, names=("greedy", "bond"), seeds=(0, 1))
    assert [(c.strategy, c.seed) for c in sequential] == [
        (c.strategy, c.seed) for c in parallel
    ]
    assert [c.metrics for c in sequential] == [c.metrics for c in parallel]


# --- CLI ------------------------------------------------------------------


def test_cli_sweep_end_to_end(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    npy = tmp_path / "box.npy"
    np.save(npy, np.full((4, 3, 2), 4, dtype=np.int16))
    out = tmp_path / "box.ldr"
    report_path = tmp_path / "reports" / "report.json"
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
    assert len(payload["candidates"]) == len(strategy_names())
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


def test_cli_sweep_flags_require_strategy_all(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # --jobs/--timeout/--seeds now also serve single-strategy restart
    # races (v5); only the sweep-reporting flags stay sweep-only.
    with pytest.raises(SystemExit) as excinfo:
        main([str(tmp_path / "x.npy"), "--report", str(tmp_path / "r.json")])
    assert excinfo.value.code == 2
    assert "--strategy all" in capsys.readouterr().err


def test_cli_restarts_rejects_nonpositive(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([str(tmp_path / "x.npy"), "--restarts", "0"])
    assert excinfo.value.code == 2
    assert "--restarts" in capsys.readouterr().err


def test_cli_profile_requires_single_seed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([str(tmp_path / "x.npy"), "--profile", str(tmp_path / "p.json")])
    assert excinfo.value.code == 2
    assert "--restarts 1" in capsys.readouterr().err


def test_cli_single_explicit_seed_updates_profile(tmp_path: Path) -> None:
    npy = tmp_path / "box.npy"
    np.save(npy, np.full((3, 3, 2), 4, dtype=np.int16))
    profile = tmp_path / "profile.json"
    out = tmp_path / "box.ldr"
    code = main(
        [
            str(npy),
            "-o",
            str(out),
            "--seeds",
            "7",
            "--profile",
            str(profile),
        ]
    )
    assert code == 0
    assert json.loads(profile.read_text())["seed"] == 7


@pytest.mark.parametrize("value", ["", "a,b", "0,,1"])
def test_cli_seeds_reject_malformed_lists(tmp_path: Path, value: str) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main([str(tmp_path / "x.npy"), "--strategy", "all", "--seeds", value])
    assert excinfo.value.code == 2


def test_cli_sweep_multi_seed_end_to_end(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
            "--seeds",
            "0,1",
            "--report",
            str(report_path),
            "--keep-candidates",
            str(candidates_dir),
        ]
    )
    assert code == 0
    payload = json.loads(report_path.read_text())
    assert payload["seeds"] == [0, 1]
    assert len(payload["candidates"]) == 2 * len(strategy_names())
    assert {c["seed"] for c in payload["candidates"]} == {0, 1}
    assert payload["winner_seed"] in (0, 1)
    succeeded = [c for c in payload["candidates"] if c["error"] is None]
    assert {p.name for p in candidates_dir.iterdir()} == {
        f"box.{c['strategy']}.seed{c['seed']}.ldr" for c in succeeded
    }
    captured = capsys.readouterr()
    assert " seed " in captured.out
    assert "(seed " in captured.out


@pytest.mark.parametrize(
    ("flag", "value", "message"),
    [
        ("--jobs", "-1", "greater than or equal to zero"),
        ("--timeout", "0", "greater than zero"),
        ("--timeout", "-1", "greater than zero"),
        ("--timeout", "nan", "greater than zero"),
        ("--timeout", "inf", "greater than zero"),
    ],
)
def test_cli_sweep_rejects_invalid_numeric_options(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    flag: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                str(tmp_path / "x.npy"),
                "--strategy",
                "all",
                flag,
                value,
            ]
        )

    assert excinfo.value.code == 2
    assert message in capsys.readouterr().err


def test_cli_sweep_reports_output_oserror(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    npy = tmp_path / "box.npy"
    np.save(npy, np.full((4, 3, 2), 4, dtype=np.int16))
    result = run(grid=_box_grid(), config=PipelineConfig(seed=0))
    candidate = Candidate(
        strategy="greedy",
        seconds=0.1,
        result=result,
        metrics=_metrics(),
    )

    def fake_run_all(*args: object, **kwargs: object) -> list[Candidate]:
        return [candidate]

    def fail_write_outputs(*args: object, **kwargs: object) -> None:
        message = "disk full"
        raise OSError(message)

    monkeypatch.setattr(main_module, "run_all", fake_run_all)
    monkeypatch.setattr(main_module, "write_outputs", fail_write_outputs)

    code = main(
        [
            str(npy),
            "-o",
            str(tmp_path / "box.ldr"),
            "--strategy",
            "all",
            "--jobs",
            "1",
        ]
    )

    assert code == 1
    assert "error: disk full" in capsys.readouterr().err


@pytest.mark.slow
def test_full_sweep_selects_buildable_winner() -> None:
    codes = np.full((6, 6, 3), EMPTY, dtype=np.int16)
    for z in range(3):
        lo, hi = z, 6 - z
        codes[lo:hi, lo:hi, z] = 4
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    candidates = run_all(grid, PipelineConfig(seed=0, time_budget_s=10.0), jobs=0)
    assert len(candidates) == len(strategy_names())
    winner = select_best(candidates).winner
    assert winner is not None
    assert winner.metrics is not None
    assert winner.metrics.buildable


def test_sequential_sweep_enforces_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PR #17 review (P1): with jobs=1 every candidate used to get a fresh
    # full timeout; the sweep must stop launching once its single
    # monotonic deadline expires and report the skipped candidates.
    clock = {"now": 0.0}
    monkeypatch.setattr(legolization.compare.time, "monotonic", lambda: clock["now"])

    def fake_run_candidate(grid: object, config: PipelineConfig) -> Candidate:
        clock["now"] += 0.05  # each candidate costs 50 ms
        return Candidate(
            strategy=config.strategy,
            seconds=0.05,
            seed=config.seed,
        )

    monkeypatch.setattr(legolization.compare, "_run_candidate", fake_run_candidate)
    codes = np.full((2, 2, 2), 4, dtype=np.int16)
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    candidates = run_all(
        grid,
        PipelineConfig(seed=0),
        names=("greedy",),
        seeds=(0, 1, 2),
        jobs=1,
        timeout_s=0.08,
    )
    ran = [c for c in candidates if c.error is None]
    skipped = [c for c in candidates if c.error is not None]
    assert len(ran) == 2  # third start would exceed the 80 ms deadline
    assert len(skipped) == 1
    assert "deadline expired" in (skipped[0].error or "")
