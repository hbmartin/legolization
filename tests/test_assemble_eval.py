"""Collection assembly validates artifacts and never executes placement."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from legolization.compare import Candidate, CandidateMetrics
from legolization.corpus import assemble
from legolization.eval_artifacts import (
    SourceIdentity,
    atomic_json,
    candidate_payload,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path
    from types import ModuleType


@pytest.fixture(scope="module")
def assembler() -> ModuleType:
    return assemble


def test_default_out_is_local_eval_runs(assembler: ModuleType) -> None:
    from pathlib import Path

    args = assembler.parse_args(["collection.json"])
    assert args.out == Path("legolization-eval") / "runs"


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


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda payload: payload.pop("identity"), "identity must be an object"),
        (
            lambda payload: payload["scope"].__setitem__("seeds", ["0"]),
            "scope.seeds must be a list of integers",
        ),
        (
            lambda payload: payload["models"][0].__setitem__("traits", [1]),
            "has invalid traits",
        ),
        (
            lambda payload: payload["models"][0]["candidates"].clear(),
            "does not carry the complete candidate matrix",
        ),
        (
            lambda payload: payload["scope"]["models"].append("absent"),
            "models entries do not match the scope model set",
        ),
        (
            lambda payload: payload.__setitem__("collection_id", "."),
            "collection_id must be a single relative path component",
        ),
        (
            lambda payload: payload.__setitem__("collection_id", ".."),
            "collection_id must be a single relative path component",
        ),
    ],
)
def test_malformed_manifest_names_the_bad_field(
    assembler: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    mutate: Callable[[dict[str, Any]], object],
    expected: str,
) -> None:
    # A hand-edited or truncated manifest used to surface as a KeyError from
    # deep inside row building rather than a message naming the bad field.
    identity = SourceIdentity(git_sha="a" * 40, source_hash="b" * 64, dirty=False)
    collection = _manifest(
        tmp_path,
        artifact=tmp_path / "candidate.json",
        identity=identity,
    )
    payload = json.loads(collection.read_text())
    mutate(payload)
    atomic_json(collection, payload)

    assert assembler.main([str(collection), "--out", str(tmp_path / "assembled")]) == 1
    assert expected in capsys.readouterr().err


def test_mid_flight_collection_still_reports_named_candidates(
    assembler: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Validation checks shape, not progress: the None placeholders a running
    # collection carries must survive into the per-candidate error list.
    identity = SourceIdentity(git_sha="a" * 40, source_hash="b" * 64, dirty=False)
    collection = _manifest(
        tmp_path,
        artifact=tmp_path / "candidate.json",
        identity=identity,
    )
    payload = json.loads(collection.read_text())
    payload["models"][0]["input_hash"] = None
    payload["models"][0]["candidates"][0]["config_hash"] = None
    payload["models"][0]["candidates"][0]["artifact"] = None
    atomic_json(collection, payload)

    assert assembler.main([str(collection), "--out", str(tmp_path / "assembled")]) == 1
    assert "fixture/greedy/seed-0: missing artifact path" in capsys.readouterr().err


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


def test_write_baseline_refused_when_evaluation_failed(
    assembler: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Even with a canonical scope, a failed evaluation must never
    # replace the baseline.
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
    baseline = tmp_path / "baseline.json"
    monkeypatch.setattr(assembler, "_canonical_scope", lambda _manifest: True)

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
    assert not baseline.exists()
    assert "baseline not written" in capsys.readouterr().err


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
