"""Corpus sweep runner tests (scorecard assembly + regression diffing)."""

import importlib.util
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "eval_corpus.py"


def _load_eval() -> ModuleType:
    spec = importlib.util.spec_from_file_location("eval_corpus_script", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def evaluator() -> ModuleType:
    return _load_eval()


@dataclass(frozen=True)
class _FakeModel:
    name: str = "cantilever"
    kind: str = "synthetic"
    traits: tuple[str, ...] = ("fast",)
    expect_min_buildable: int = 1
    plates_per_voxel: int = 3
    target_studs: int | None = None
    up: str | None = None
    generator: str | None = "cantilever"
    abs_path: Path = field(default=Path("/nonexistent"))


def _report_dict(
    *,
    winner: str = "greedy",
    buildable: bool = True,
    objective: float = 0.5,
    bricks: int = 10,
) -> dict:
    metrics = {
        "buildable": buildable,
        "objective_total": objective,
        "brick_count": bricks,
        "max_score": 0.1,
        "maximin_capacity": 2.0,
    }
    return {
        "winner": winner,
        "reason": "test",
        "buildable": buildable,
        "candidates": [
            {"strategy": winner, "seconds": 1.0, "error": None, "metrics": metrics},
            {"strategy": "luo", "seconds": 1.0, "error": "boom", "metrics": None},
        ],
    }


def test_build_row_extracts_winner_metrics(evaluator):
    row = evaluator.build_row(_FakeModel(), _report_dict(), "ok")
    assert row["winner"] == "greedy"
    assert row["buildable_count"] == 1
    assert row["expectation_ok"] is True
    assert row["objective_total"] == 0.5
    assert row["brick_count"] == 10


def test_build_row_skipped_has_no_winner(evaluator):
    row = evaluator.build_row(_FakeModel(), None, "skipped: mesh not on disk")
    assert row["winner"] is None
    assert row["buildable_count"] == 0
    assert row["expectation_ok"] is False  # expects 1 buildable, got none


def test_build_row_expect_zero_passes_when_skipped(evaluator):
    model = _FakeModel(name="sparse-pillars", expect_min_buildable=0)
    row = evaluator.build_row(model, None, "skipped")
    assert row["expectation_ok"] is True


def test_regression_buildable_drop_is_hard(evaluator):
    old = [evaluator.build_row(_FakeModel(), _report_dict(), "ok")]
    new_report = _report_dict(buildable=False)
    new = [evaluator.build_row(_FakeModel(), new_report, "ok")]
    hard, _info = evaluator.compare_to_baseline(new, old, tolerance=0.05)
    assert any("buildable strategies dropped" in line for line in hard)
    assert any("expectation newly failing" in line for line in hard)


def test_regression_objective_worsening_is_hard(evaluator):
    old = [evaluator.build_row(_FakeModel(), _report_dict(objective=0.5), "ok")]
    new = [evaluator.build_row(_FakeModel(), _report_dict(objective=0.6), "ok")]
    hard, _info = evaluator.compare_to_baseline(new, old, tolerance=0.05)
    assert any("objective worsened" in line for line in hard)


def test_regression_within_tolerance_is_quiet(evaluator):
    old = [evaluator.build_row(_FakeModel(), _report_dict(objective=0.5), "ok")]
    new = [evaluator.build_row(_FakeModel(), _report_dict(objective=0.51), "ok")]
    hard, info = evaluator.compare_to_baseline(new, old, tolerance=0.05)
    assert hard == []
    assert info == []


def test_winner_change_is_informational(evaluator):
    old = [evaluator.build_row(_FakeModel(), _report_dict(winner="greedy"), "ok")]
    new = [evaluator.build_row(_FakeModel(), _report_dict(winner="bond"), "ok")]
    hard, info = evaluator.compare_to_baseline(new, old, tolerance=0.05)
    assert hard == []
    assert any("winner greedy -> bond" in line for line in info)


def test_new_and_missing_models_are_informational(evaluator):
    old = [evaluator.build_row(_FakeModel(name="gone"), _report_dict(), "ok")]
    new = [evaluator.build_row(_FakeModel(name="fresh"), _report_dict(), "ok")]
    hard, info = evaluator.compare_to_baseline(new, old, tolerance=0.05)
    assert hard == []
    assert any("new model" in line for line in info)
    assert any("not in this run" in line for line in info)


def test_markdown_rendering(evaluator):
    rows = [evaluator.build_row(_FakeModel(), _report_dict(), "ok")]
    table = evaluator.to_markdown(rows)
    assert table.splitlines()[0].startswith("| model | status | winner")
    assert "| cantilever | ok | greedy | 1 | 0.5 | 10 |" in table


@pytest.mark.slow
def test_smoke_sweep_two_models(evaluator, tmp_path):
    exit_code = evaluator.main(
        [
            "--models",
            "cantilever,letter-t",
            "--strategies",
            "greedy",
            "--jobs",
            "1",
            "--out",
            str(tmp_path / "runs"),
            "--baseline",
            str(tmp_path / "baseline.json"),
            "--write-baseline",
        ]
    )
    assert exit_code == 0
    assert (tmp_path / "baseline.json").exists()
    runs = list((tmp_path / "runs").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "scorecard.md").exists()
