"""StableLego dataset sweep: discovery, sampling, verdicts, report."""

import importlib.util
import json
import sys
from pathlib import Path
from typing import Protocol, cast

import numpy as np
import pytest

_SCRIPT = Path(__file__).parent.parent / "scripts" / "stablelego_sweep.py"
_DATA = Path(__file__).parent / "data" / "stablelego"


class _SweepModule(Protocol):
    """Typed surface of the dynamically loaded sweep script."""

    def main(self, argv: list[str] | None = None) -> int:
        """Run the sweep CLI."""
        ...

    def sample_objects(self, objects: list[Path], sample: int, seed: int) -> list[Path]:
        """Deterministic subset selection."""
        ...

    def release_verdict(self, scores: np.ndarray) -> tuple[bool, float]:
        """Verdict from a release score file."""
        ...


@pytest.fixture(scope="module")
def sweep() -> _SweepModule:
    spec = importlib.util.spec_from_file_location("stablelego_sweep", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return cast("_SweepModule", module)


def _write_object(
    root: Path,
    name: str,
    fixture: str,
    scores: np.ndarray,
) -> None:
    obj = root / name
    obj.mkdir(parents=True)
    (obj / "task_graph.json").write_text((_DATA / f"{fixture}.json").read_text())
    np.save(obj / "stability_score.npy", scores)


def _fixture_brick_count(fixture: str) -> int:
    return len(json.loads((_DATA / f"{fixture}.json").read_text()))


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    root = tmp_path / "dataset"
    # stick_light stands for us; a zero score file agrees.
    _write_object(
        root,
        "agree_stable",
        "stick_light",
        np.zeros(_fixture_brick_count("stick_light")),
    )
    # stair_20 collapses for us; an all-clear score file disagrees.
    _write_object(
        root,
        "disagree",
        "stair_20",
        np.zeros(_fixture_brick_count("stair_20")),
    )
    # A score file with the wrong length must be skipped, never guessed.
    _write_object(root, "malformed", "stick_light", np.zeros(3))
    # A directory without the release files is not an object.
    (root / "not_an_object").mkdir()
    return root


def test_release_verdict_convention(sweep: _SweepModule) -> None:
    stands, max_score = sweep.release_verdict(np.array([0.2, 0.9]))
    assert stands
    assert max_score == pytest.approx(0.9)
    assert not sweep.release_verdict(np.array([0.2, 1.0]))[0]
    assert not sweep.release_verdict(np.array([0.2, np.inf]))[0]


def test_sampling_is_deterministic_and_sorted(sweep: _SweepModule) -> None:
    objects = [Path(f"obj_{i}") for i in range(10)]
    first = sweep.sample_objects(objects, 4, seed=7)
    assert first == sweep.sample_objects(objects, 4, seed=7)
    assert len(first) == 4
    assert first == sorted(first)
    assert sweep.sample_objects(objects, 0, seed=7) == objects


def test_sweep_reports_agreement_and_skips(
    sweep: _SweepModule,
    dataset: Path,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    out = tmp_path / "reports"
    exit_code = sweep.main(
        [
            "--dataset",
            str(dataset),
            "--sample",
            "0",
            "--out",
            str(out),
        ]
    )
    assert exit_code == 0
    report = json.loads(next(out.glob("*/report.json")).read_text())
    assert report["agree"] == 1
    assert report["disagree"] == 1
    assert len(report["skipped"]) == 1
    assert "malformed" in report["skipped"][0]
    by_name = {row["name"]: row for row in report["rows"]}
    assert by_name["agree_stable"]["agree"] is True
    assert by_name["disagree"]["ours_stable"] is False
    assert by_name["disagree"]["theirs_stable"] is True
    markdown = next(out.glob("*/report.md")).read_text()
    assert "| disagree |" in markdown
    assert "collapses" in markdown
    assert "objects=2 agree=1 disagree=1 skipped=1" in markdown
    assert "wrote" in capsys.readouterr().out


def test_sweep_rejects_missing_dataset(
    sweep: _SweepModule,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert sweep.main(["--dataset", str(tmp_path / "missing")]) == 1
    assert "not a directory" in capsys.readouterr().err
