"""Corpus sweep runner tests (scorecard assembly + regression diffing)."""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import pytest

from legolization.mesh import MeshOptions

if TYPE_CHECKING:
    import argparse

_SCRIPT = Path(__file__).parent.parent / "scripts" / "eval_corpus.py"


def _load_eval() -> _EvaluatorModule:
    spec = importlib.util.spec_from_file_location("eval_corpus_script", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast("_EvaluatorModule", module)


@pytest.fixture(scope="module")
def evaluator() -> _EvaluatorModule:
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
    largest_component_only: bool = False
    abs_path: Path = field(default=Path("/nonexistent"))


class _EvaluatorModule(Protocol):
    """Typed surface of the dynamically loaded evaluator script."""

    def model_mesh_options(self, model: _FakeModel) -> MeshOptions | None:
        """Return the effective mesh options a manifest model resolves with."""
        ...

    def build_row(
        self,
        model: _FakeModel,
        report_dict: dict | None,
        status: str,
    ) -> dict:
        """Build one evaluator scorecard row."""
        ...

    def compare_to_baseline(
        self,
        rows: list[dict],
        baseline_rows: list[dict],
        *,
        tolerance: float,
    ) -> tuple[list[str], list[str]]:
        """Compare scorecard rows to their baseline."""
        ...

    def to_markdown(self, rows: list[dict]) -> str:
        """Render scorecard rows as Markdown."""
        ...

    def main(self, argv: list[str] | None = None) -> int:
        """Run the evaluator CLI."""
        ...

    BASELINE: Path
    MESH_BASELINE: Path
    _BASELINE_BY_KIND: dict[str, Path]

    def parse_args(self, argv: list[str] | None) -> argparse.Namespace:
        """Parse evaluator CLI arguments."""
        ...

    def baseline_write_path(self, args: argparse.Namespace) -> Path:
        """Resolve the canonical baseline file a run may write."""
        ...

    def baseline_rows(self, args: argparse.Namespace) -> list[dict] | None:
        """Baseline rows to diff against, or None when none exist."""
        ...


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


def test_build_row_extracts_winner_metrics(evaluator: _EvaluatorModule) -> None:
    row = evaluator.build_row(
        model=_FakeModel(),
        report_dict=_report_dict(),
        status="ok",
    )
    assert row["winner"] == "greedy"
    assert row["buildable_count"] == 1
    assert row["expectation_ok"] is True
    assert row["objective_total"] == 0.5
    assert row["brick_count"] == 10


def test_build_row_skipped_has_no_winner(evaluator: _EvaluatorModule) -> None:
    row = evaluator.build_row(
        model=_FakeModel(),
        report_dict=None,
        status="skipped: mesh not on disk",
    )
    assert row["winner"] is None
    assert row["buildable_count"] == 0
    assert row["expectation_ok"] is False  # expects 1 buildable, got none


def test_build_row_expect_zero_passes_when_skipped(
    evaluator: _EvaluatorModule,
) -> None:
    model = _FakeModel(name="sparse-pillars", expect_min_buildable=0)
    row = evaluator.build_row(
        model=model,
        report_dict=None,
        status="skipped: mesh not on disk",
    )
    assert row["expectation_ok"] is True


def test_regression_buildable_drop_is_hard(
    evaluator: _EvaluatorModule,
) -> None:
    old = [
        evaluator.build_row(
            model=_FakeModel(),
            report_dict=_report_dict(),
            status="ok",
        )
    ]
    new_report = _report_dict(buildable=False)
    new = [
        evaluator.build_row(
            model=_FakeModel(),
            report_dict=new_report,
            status="ok",
        )
    ]
    hard, _info = evaluator.compare_to_baseline(
        rows=new,
        baseline_rows=old,
        tolerance=0.05,
    )
    assert any("buildable strategies dropped" in line for line in hard)
    assert any("expectation newly failing" in line for line in hard)


def test_regression_objective_worsening_is_hard(
    evaluator: _EvaluatorModule,
) -> None:
    old = [
        evaluator.build_row(
            model=_FakeModel(),
            report_dict=_report_dict(objective=0.5),
            status="ok",
        )
    ]
    new = [
        evaluator.build_row(
            model=_FakeModel(),
            report_dict=_report_dict(objective=0.6),
            status="ok",
        )
    ]
    hard, _info = evaluator.compare_to_baseline(
        rows=new,
        baseline_rows=old,
        tolerance=0.05,
    )
    assert any("objective worsened" in line for line in hard)


def test_regression_within_tolerance_is_quiet(
    evaluator: _EvaluatorModule,
) -> None:
    old = [
        evaluator.build_row(
            model=_FakeModel(),
            report_dict=_report_dict(objective=0.5),
            status="ok",
        )
    ]
    new = [
        evaluator.build_row(
            model=_FakeModel(),
            report_dict=_report_dict(objective=0.51),
            status="ok",
        )
    ]
    hard, info = evaluator.compare_to_baseline(
        rows=new,
        baseline_rows=old,
        tolerance=0.05,
    )
    assert hard == []
    assert info == []


def test_winner_change_is_informational(evaluator: _EvaluatorModule) -> None:
    old = [
        evaluator.build_row(
            model=_FakeModel(),
            report_dict=_report_dict(winner="greedy"),
            status="ok",
        )
    ]
    new = [
        evaluator.build_row(
            model=_FakeModel(),
            report_dict=_report_dict(winner="bond"),
            status="ok",
        )
    ]
    hard, info = evaluator.compare_to_baseline(
        rows=new,
        baseline_rows=old,
        tolerance=0.05,
    )
    assert hard == []
    assert any("winner greedy -> bond" in line for line in info)


def test_new_and_missing_models_are_informational(
    evaluator: _EvaluatorModule,
) -> None:
    old = [
        evaluator.build_row(
            model=_FakeModel(name="gone"),
            report_dict=_report_dict(),
            status="ok",
        )
    ]
    new = [
        evaluator.build_row(
            model=_FakeModel(name="fresh"),
            report_dict=_report_dict(),
            status="ok",
        )
    ]
    hard, info = evaluator.compare_to_baseline(
        rows=new,
        baseline_rows=old,
        tolerance=0.05,
    )
    assert hard == []
    assert any("new model" in line for line in info)
    assert any("not in this run" in line for line in info)


def test_markdown_rendering(evaluator: _EvaluatorModule) -> None:
    rows = [
        evaluator.build_row(
            model=_FakeModel(),
            report_dict=_report_dict(),
            status="ok",
        )
    ]
    table = evaluator.to_markdown(rows=rows)
    assert table.splitlines()[0].startswith("| model | status | winner")
    assert "| cantilever | ok | greedy | 1 | 0.5 | 10 |" in table


@pytest.mark.parametrize(
    "scope_args",
    [
        ["--kind", "synthetic", "--models", "cantilever"],
        ["--kind", "synthetic", "--traits", "fast"],
        ["--kind", "synthetic", "--strategies", "greedy"],
        ["--kind", "synthetic", "--seed", "1"],
        ["--kind", "synthetic", "--seeds", "0,1"],
        ["--kind", "mesh", "--models", "suzanne"],
        ["--kind", "mesh", "--seed", "2"],
    ],
)
def test_write_baseline_rejects_noncanonical_scope(
    evaluator: _EvaluatorModule,
    scope_args: list[str],
) -> None:
    with pytest.raises(SystemExit, match="unfiltered --kind synthetic"):
        evaluator.main(argv=[*scope_args, "--write-baseline"])


def test_default_scope_is_synthetic(evaluator: _EvaluatorModule) -> None:
    assert evaluator.parse_args([]).kind == "synthetic"


def test_baseline_paths_route_by_kind(evaluator: _EvaluatorModule) -> None:
    # Each kind owns one committed baseline file; an explicit --baseline
    # overrides both the write target and the comparison source.
    synthetic = evaluator.parse_args(["--kind", "synthetic", "--write-baseline"])
    mesh = evaluator.parse_args(["--kind", "mesh", "--write-baseline"])
    explicit = evaluator.parse_args(
        ["--kind", "mesh", "--write-baseline", "--baseline", "other.json"]
    )
    assert evaluator.baseline_write_path(synthetic) == evaluator.BASELINE
    assert evaluator.baseline_write_path(mesh) == evaluator.MESH_BASELINE
    assert evaluator.baseline_write_path(explicit).name == "other.json"


def test_baseline_rows_route_by_kind(
    evaluator: _EvaluatorModule,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic = tmp_path / "scorecard.json"
    mesh = tmp_path / "scorecard-mesh.json"
    synthetic.write_text(json.dumps({"models": [{"model": "cantilever"}]}))
    mesh.write_text(json.dumps({"models": [{"model": "suzanne"}]}))
    monkeypatch.setattr(evaluator, "BASELINE", synthetic)
    monkeypatch.setattr(evaluator, "MESH_BASELINE", mesh)
    monkeypatch.setattr(
        evaluator, "_BASELINE_BY_KIND", {"synthetic": synthetic, "mesh": mesh}
    )
    default_rows = evaluator.baseline_rows(evaluator.parse_args([]))
    assert default_rows is not None
    assert {row["model"] for row in default_rows} == {"cantilever"}
    mesh_rows = evaluator.baseline_rows(evaluator.parse_args(["--kind", "mesh"]))
    assert mesh_rows is not None
    assert {row["model"] for row in mesh_rows} == {"suzanne"}
    # Explicit --baseline restricts to that one file.
    only = evaluator.baseline_rows(evaluator.parse_args(["--baseline", str(mesh)]))
    assert only is not None
    assert {row["model"] for row in only} == {"suzanne"}
    # A typo'd EXPLICIT path errors loudly (PR #20 review) ...
    with pytest.raises(SystemExit, match="does not exist"):
        evaluator.baseline_rows(
            evaluator.parse_args(["--baseline", str(tmp_path / "absent.json")])
        )
    # ... while absent committed defaults still skip the comparison.
    synthetic.unlink()
    assert evaluator.baseline_rows(evaluator.parse_args([])) is None


def _multi_seed_report(*, objectives: dict[int, float]) -> dict:
    candidates = []
    for seed, objective in objectives.items():
        metrics = {
            "buildable": True,
            "objective_total": objective,
            "brick_count": 10 + seed,
            "max_score": 0.1,
            "maximin_capacity": 2.0,
        }
        candidates.append(
            {
                "strategy": "greedy",
                "seed": seed,
                "seconds": 1.0,
                "error": None,
                "metrics": metrics,
            }
        )
    best_seed = min(objectives, key=lambda seed: objectives[seed])
    return {
        "winner": "greedy",
        "winner_seed": best_seed,
        "reason": "test",
        "buildable": True,
        "candidates": candidates,
    }


def test_build_row_multi_seed_spread(evaluator: _EvaluatorModule) -> None:
    report = _multi_seed_report(objectives={0: 0.9, 1: 0.5, 2: 0.7})
    row = evaluator.build_row(model=_FakeModel(), report_dict=report, status="ok")
    assert row["seeds"] == [0, 1, 2]
    assert row["winner_seed"] == 1
    assert row["buildable_count"] == 1  # one distinct strategy, not three
    assert row["seed_spread"]["buildable_seeds"] == [0, 1, 2]
    assert row["seed_spread"]["objective_min"] == 0.5
    assert row["seed_spread"]["objective_max"] == 0.9
    assert row["seed_spread"]["brick_min"] == 10
    assert row["seed_spread"]["brick_max"] == 12
    assert row["objective_total"] == 0.5  # the winner's (seed 1) metrics


def test_single_seed_row_has_no_spread(evaluator: _EvaluatorModule) -> None:
    row = evaluator.build_row(
        model=_FakeModel(),
        report_dict=_report_dict(),
        status="ok",
    )
    assert "seed_spread" not in row
    assert "seeds" not in row


def test_multi_seed_skips_baseline_diff(
    evaluator: _EvaluatorModule,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"models": []}))
    rows = [
        evaluator.build_row(
            model=_FakeModel(),
            report_dict=_multi_seed_report(objectives={0: 0.9, 1: 0.5}),
            status="ok",
        )
    ]

    def fake_sweep(
        _corpus: object,
        _models: object,
        _args: object,
    ) -> list[dict]:
        return rows

    monkeypatch.setattr(evaluator, "_sweep", fake_sweep)
    exit_code = evaluator.main(
        argv=[
            "--models",
            "cantilever",
            "--seeds",
            "0,1",
            "--out",
            str(tmp_path / "runs"),
            "--baseline",
            str(baseline),
        ]
    )
    assert exit_code == 0
    assert "baseline comparison skipped" in capsys.readouterr().out


def test_skips_are_unevaluated_but_errors_fail(
    evaluator: _EvaluatorModule,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        evaluator.build_row(
            model=_FakeModel(name="missing-mesh", expect_min_buildable=1),
            report_dict=None,
            status="skipped: mesh not on disk (download)",
        ),
        evaluator.build_row(
            model=_FakeModel(name="expected-unbuildable", expect_min_buildable=0),
            report_dict=None,
            status="error: all failed",
        ),
    ]

    def fake_sweep(
        _corpus: object,
        _models: object,
        _args: object,
    ) -> list[dict]:
        return rows

    empty_baseline = tmp_path / "empty-baseline.json"
    empty_baseline.write_text(json.dumps({"models": []}))
    monkeypatch.setattr(evaluator, "_sweep", fake_sweep)
    exit_code = evaluator.main(
        argv=[
            "--models",
            "cantilever",
            "--out",
            str(tmp_path / "runs"),
            "--baseline",
            str(empty_baseline),
        ]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "evaluation failures: expected-unbuildable" in output
    assert "expectation failures: missing-mesh" not in output


def test_failed_sweep_does_not_replace_baseline(
    evaluator: _EvaluatorModule,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text("existing baseline\n")
    rows = [
        evaluator.build_row(
            model=_FakeModel(name="expected-unbuildable", expect_min_buildable=0),
            report_dict=None,
            status="error: all failed",
        )
    ]

    def fake_sweep(
        _corpus: object,
        _models: object,
        _args: object,
    ) -> list[dict]:
        return rows

    monkeypatch.setattr(evaluator, "_sweep", fake_sweep)
    exit_code = evaluator.main(
        argv=[
            "--kind",
            "synthetic",
            "--out",
            str(tmp_path / "runs"),
            "--baseline",
            str(baseline),
            "--write-baseline",
        ]
    )

    assert exit_code == 1
    assert baseline.read_text() == "existing baseline\n"


def test_clean_canonical_sweep_writes_baseline(
    evaluator: _EvaluatorModule,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    baseline = tmp_path / "baseline.json"
    rows = [
        evaluator.build_row(
            model=_FakeModel(),
            report_dict=_report_dict(),
            status="ok",
        )
    ]

    def fake_sweep(
        _corpus: object,
        _models: object,
        _args: object,
    ) -> list[dict]:
        return rows

    monkeypatch.setattr(evaluator, "_sweep", fake_sweep)
    exit_code = evaluator.main(
        argv=[
            "--kind",
            "synthetic",
            "--out",
            str(tmp_path / "runs"),
            "--baseline",
            str(baseline),
            "--write-baseline",
        ]
    )

    assert exit_code == 0
    assert baseline.exists()


@pytest.mark.slow
def test_smoke_sweep_two_models(
    evaluator: _EvaluatorModule,
    tmp_path: Path,
) -> None:
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"models": []}))
    exit_code = evaluator.main(
        argv=[
            "--models",
            "cantilever,letter-t",
            "--strategies",
            "greedy",
            "--jobs",
            "1",
            "--out",
            str(tmp_path / "runs"),
            "--baseline",
            str(baseline),
        ]
    )
    assert exit_code == 0
    # Without --write-baseline the explicit file is never touched.
    assert json.loads(baseline.read_text()) == {"models": []}
    runs = list((tmp_path / "runs").iterdir())
    assert len(runs) == 1
    assert (runs[0] / "scorecard.md").exists()


def test_model_mesh_options_reports_manifest_resolution(
    evaluator: _EvaluatorModule,
) -> None:
    # PR #18 review: profile identity must stamp the values a corpus run
    # actually used — manifest values for meshes, None for synthetics.
    explicit = _FakeModel(
        kind="mesh", target_studs=48, up="y", largest_component_only=True
    )
    assert evaluator.model_mesh_options(explicit) == MeshOptions(
        target_studs=48, up="y", keep_largest=True
    )
    defaulted = evaluator.model_mesh_options(_FakeModel(kind="mesh"))
    assert defaulted == MeshOptions(target_studs=32, up="z", keep_largest=False)
    assert evaluator.model_mesh_options(_FakeModel()) is None
