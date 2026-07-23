"""Collection assembly validates artifacts and never executes placement."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from legolization.compare import Candidate, CandidateMetrics
from legolization.eval_artifacts import (
    SourceIdentity,
    atomic_json,
    candidate_payload,
)

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT = Path(__file__).parent.parent / "scripts" / "assemble_eval.py"


def _load_assembler() -> ModuleType:
    spec = importlib.util.spec_from_file_location("assemble_eval_script", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def assembler() -> ModuleType:
    return _load_assembler()


def _metrics() -> CandidateMetrics:
    return CandidateMetrics(
        buildable=True,
        stable=True,
        component_count=1,
        floating_count=0,
        objective_total=1.0,
        maximin_feasible=True,
        maximin_capacity=1.0,
        max_score=0.1,
        min_capacity=1.0,
        brick_count=10,
        mass_g=20.0,
        step_count=2,
        cost=0.5,
        aesthetics=0.0,
        colour_error=0.0,
        perpendicularity=0.0,
        symmetry=0.0,
    )


def _manifest(
    tmp_path: Path,
    *,
    artifact: Path,
    identity: SourceIdentity,
    candidate_status: str = "ok",
) -> Path:
    payload = {
        "schema": 1,
        "collection_id": "fixture",
        "status": "complete",
        "identity": identity.to_dict(),
        "scope": {
            "kind": "synthetic",
            "models": ["fixture"],
            "strategies": ["greedy"],
            "seeds": [0],
            "timeout_s": 1.0,
        },
        "models": [
            {
                "model": "fixture",
                "kind": "synthetic",
                "traits": ["fast"],
                "expect_min_buildable": 1,
                "input_hash": "input",
                "unsupported_ratio": 0.0,
                "candidates": [
                    {
                        "strategy": "greedy",
                        "seed": 0,
                        "status": candidate_status,
                        "config_hash": "config",
                        "artifact": str(artifact),
                    }
                ],
            }
        ],
    }
    path = tmp_path / "collection.json"
    atomic_json(path, payload)
    return path


def test_incomplete_collection_cannot_write_baseline(
    assembler: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identity = SourceIdentity(git_sha="a" * 40, source_hash="b" * 64, dirty=True)
    collection = _manifest(
        tmp_path,
        artifact=tmp_path / "missing.json",
        identity=identity,
    )
    baseline = tmp_path / "baseline.json"
    baseline.write_text("preserve\n")
    assert (
        assembler.main(
            [
                str(collection),
                "--write-baseline",
                "--baseline",
                str(baseline),
                "--out",
                str(tmp_path / "assembled"),
            ]
        )
        == 1
    )
    assert "fixture/greedy/seed-0" in capsys.readouterr().err
    assert baseline.read_text() == "preserve\n"


def test_identity_mismatch_blocks_assembly(
    assembler: ModuleType,
    tmp_path: Path,
) -> None:
    identity = SourceIdentity(git_sha="a" * 40, source_hash="b" * 64, dirty=False)
    artifact = tmp_path / "candidate.json"
    atomic_json(
        artifact,
        candidate_payload(
            Candidate("greedy", 0.1, metrics=_metrics()),
            identity=SourceIdentity(
                git_sha="c" * 40,
                source_hash="b" * 64,
                dirty=False,
            ),
            config_hash="config",
            input_hash="input",
            model="fixture",
        ),
    )
    collection = _manifest(tmp_path, artifact=artifact, identity=identity)
    assert assembler.main([str(collection), "--out", str(tmp_path / "assembled")]) == 1
    assert not (tmp_path / "assembled" / "fixture" / "scorecard.json").exists()


@pytest.mark.parametrize("payload", [None, []])
def test_non_object_artifact_blocks_assembly_without_crashing(
    assembler: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: object,
) -> None:
    identity = SourceIdentity(git_sha="a" * 40, source_hash="b" * 64, dirty=False)
    artifact = tmp_path / "candidate.json"
    atomic_json(artifact, payload)
    collection = _manifest(tmp_path, artifact=artifact, identity=identity)

    assert assembler.main([str(collection), "--out", str(tmp_path / "out")]) == 1
    assert "invalid payload structure" in capsys.readouterr().err


def test_failed_candidate_rehydrates_as_complete_collection(
    assembler: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identity = SourceIdentity(git_sha="a" * 40, source_hash="b" * 64, dirty=False)
    artifact = tmp_path / "candidate.json"
    atomic_json(
        artifact,
        candidate_payload(
            Candidate("greedy", 0.1, error="solver failed"),
            identity=identity,
            config_hash="config",
            input_hash="input",
            model="fixture",
        ),
    )
    collection = _manifest(
        tmp_path,
        artifact=artifact,
        identity=identity,
        candidate_status="error",
    )
    out = tmp_path / "assembled"

    assert assembler.main([str(collection), "--out", str(out)]) == 1
    captured = capsys.readouterr()
    assert "collection is incomplete" not in captured.err
    scorecard = json.loads((out / "fixture" / "scorecard.json").read_text())
    assert scorecard["models"][0]["status"] == "error: all failed"


def test_successful_dirty_collection_assembles_without_placement(
    assembler: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = SourceIdentity(git_sha="a" * 40, source_hash="b" * 64, dirty=True)
    artifact = tmp_path / "candidate.json"
    atomic_json(
        artifact,
        candidate_payload(
            Candidate("greedy", 0.1, metrics=_metrics()),
            identity=identity,
            config_hash="config",
            input_hash="input",
            model="fixture",
        ),
    )
    collection = _manifest(tmp_path, artifact=artifact, identity=identity)
    # The assembler module has no placement entry point; pin that this
    # path only consumes the supplied JSON.
    monkeypatch.setattr(
        "legolization.pipeline.run",
        lambda *_args, **_kwargs: pytest.fail("placement must not run"),
    )
    out = tmp_path / "assembled"
    assert assembler.main([str(collection), "--out", str(out)]) == 0
    scorecard = json.loads((out / "fixture" / "scorecard.json").read_text())
    assert scorecard["identity"]["dirty"] is True
    assert scorecard["configuration_hashes"]["fixture/greedy/seed-0"] == "config"
    assert scorecard["input_hashes"] == {"fixture": "input"}
