"""Resumable evaluation identity and artifact tests."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from typing import TYPE_CHECKING

from legolization.compare import Candidate, CandidateMetrics
from legolization.eval_artifacts import (
    SourceIdentity,
    all_successful,
    atomic_json,
    candidate_path,
    candidate_payload,
    configuration_hash,
    matching_candidate,
    source_identity,
)
from legolization.pipeline import PipelineConfig

if TYPE_CHECKING:
    from pathlib import Path


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


def _candidate() -> Candidate:
    return Candidate(strategy="greedy", seconds=1.25, metrics=_metrics(), seed=3)


def test_matching_artifact_invalidates_each_identity_axis(tmp_path: Path) -> None:
    identity = SourceIdentity(git_sha="a" * 40, source_hash="b" * 64, dirty=True)
    config_hash = configuration_hash(PipelineConfig(seed=3))
    input_hash = "c" * 64
    path = candidate_path(
        tmp_path,
        model="shape",
        strategy="greedy",
        seed=3,
        identity=identity,
        config_hash=config_hash,
        input_hash=input_hash,
    )
    atomic_json(
        path,
        candidate_payload(
            _candidate(),
            identity=identity,
            config_hash=config_hash,
            input_hash=input_hash,
            model="shape",
        ),
    )

    def match(
        *,
        candidate_identity: SourceIdentity = identity,
        candidate_config: str = config_hash,
        candidate_input: str = input_hash,
    ) -> Candidate | None:
        return matching_candidate(
            path,
            identity=candidate_identity,
            config_hash=candidate_config,
            input_hash=candidate_input,
            model="shape",
            strategy="greedy",
            seed=3,
        )

    assert match() is not None
    assert match(candidate_identity=replace(identity, git_sha="d" * 40)) is None
    assert match(candidate_identity=replace(identity, source_hash="e" * 64)) is None
    assert match(candidate_config="f" * 64) is None
    assert match(candidate_input="0" * 64) is None


def test_failures_and_corrupt_artifacts_are_never_reused(tmp_path: Path) -> None:
    identity = SourceIdentity(git_sha="a" * 40, source_hash="b" * 64, dirty=False)
    path = tmp_path / "candidate.json"
    failed = Candidate(strategy="greedy", seconds=0.2, error="boom", seed=0)
    atomic_json(
        path,
        candidate_payload(
            failed,
            identity=identity,
            config_hash="c",
            input_hash="d",
            model="shape",
        ),
    )
    kwargs = {
        "identity": identity,
        "config_hash": "c",
        "input_hash": "d",
        "model": "shape",
        "strategy": "greedy",
        "seed": 0,
    }
    assert matching_candidate(path, **kwargs) is None
    path.write_text("{")
    assert matching_candidate(path, **kwargs) is None
    for payload in (None, []):
        path.write_text(json.dumps(payload))
        assert matching_candidate(path, **kwargs) is None
        assert not all_successful([path])


def test_atomic_json_never_exposes_partial_json(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "result.json"
    for value in range(20):
        atomic_json(path, {"value": value, "payload": list(range(100))})
        assert json.loads(path.read_text())["value"] == value
        assert not path.with_name(f".{path.name}.tmp").exists()


def test_source_hash_includes_untracked_runtime_but_ignores_outputs(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "config", "user.email", "tests@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tests"],
        cwd=tmp_path,
        check=True,
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "runtime.py").write_text("VALUE = 1\n")
    (tmp_path / ".gitignore").write_text("eval/\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)

    clean = source_identity(tmp_path)
    ignored = tmp_path / "eval" / "candidate.json"
    ignored.parent.mkdir()
    ignored.write_text("{}")
    assert source_identity(tmp_path) == clean

    (tmp_path / "src" / "new_runtime.py").write_text("VALUE = 2\n")
    changed = source_identity(tmp_path)
    assert changed.git_sha == clean.git_sha
    assert changed.source_hash != clean.source_hash
    assert changed.dirty
