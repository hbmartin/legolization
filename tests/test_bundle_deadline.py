"""Soft deadlines, provisional publication, late adoption, worker caps."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import TYPE_CHECKING

import numpy as np
import pytest
from filelock import FileLock

if TYPE_CHECKING:
    from pathlib import Path

from legolization.bundle import orchestrator, workers
from legolization.bundle.identity import bundle_identity
from legolization.bundle.record import read_record
from legolization.configuration import ProjectConfig
from legolization.eval_artifacts import atomic_json


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _colour_pair_npy(path: Path) -> Path:
    """Red/white RGBA voxels: multi-colour, dither-identical targets."""
    values = np.zeros((4, 3, 2, 4), dtype=np.uint8)
    values[..., 3] = 255
    values[:2, :, :, 0] = 255
    values[2:, :, :, :3] = 255
    np.save(path, values)
    return path


def _metrics_payload(*, buildable: bool = True) -> dict:
    from legolization.compare import CandidateMetrics

    return asdict(
        CandidateMetrics(
            buildable=buildable,
            stable=buildable,
            component_count=1 if buildable else 2,
            floating_count=0,
            objective_total=1.0,
            maximin_feasible=True,
            maximin_capacity=1.0,
            max_score=0.5,
            min_capacity=1.0,
            brick_count=8,
            mass_g=8.0,
            step_count=2,
            cost=1.0,
            aesthetics=0.0,
            colour_error=0.0,
            perpendicularity=0.0,
            symmetry=0.0,
        )
    )


def _plant_result(  # noqa: PLR0913 - a full worker result payload
    directory: Path,
    *,
    identity: dict,
    candidate_key: str,
    strategy: str,
    variant: str,
    objective: float,
) -> None:
    atomic_json(
        directory / workers.RESULT_FILENAME,
        {
            "schema": workers.RESULT_SCHEMA,
            "identity": identity,
            "candidate_key": candidate_key,
            "status": "ok",
            "strategy": strategy,
            "seed": 0,
            "variant": variant,
            "seconds": 0.5,
            "metrics": _metrics_payload(),
            "selection_objective": objective,
            "cross_colour_error": 0.0,
        },
    )
    (directory / "model.ldr").write_text("0 fake candidate model\n")


@pytest.fixture
def pair_npy(tmp_path) -> Path:
    return _colour_pair_npy(tmp_path / "pair.npy")


def test_deadline_publishes_provisional_and_adopts_late_result(
    pair_npy,
    monkeypatch,
):
    config = ProjectConfig()
    clock = _FakeClock()
    identity = bundle_identity(pair_npy, config).to_dict()
    held: list[FileLock] = []

    def _fake_spawn(directory: Path, *, launcher=None) -> int:
        key = directory.name
        stamp = json.loads((directory / workers.STAMP_FILENAME).read_text())
        if key.endswith("-hard"):
            _plant_result(
                directory,
                identity=stamp["identity"],
                candidate_key=key,
                strategy="greedy",
                variant="hard",
                objective=5.0,
            )
        else:
            lock = FileLock(directory / workers.LOCK_FILENAME)
            lock.acquire()
            held.append(lock)
            if key.endswith("-soft-dither"):
                clock.now = 10_000.0
        return 4242

    monkeypatch.setattr(workers, "spawn_worker", _fake_spawn)
    request = orchestrator.BundleRequest(
        input_path=pair_npy,
        config=config,
        quality="fast",
        clock=clock,
        render="off",
    )
    envelope = orchestrator.run_bundle(request)
    assert envelope.exit_code == 3
    bundle_dir = pair_npy.parent / "pair-legolization"
    record = read_record(bundle_dir)
    assert record is not None
    assert record.status == "partial"
    assert record.verdicts["provisional"] is True
    assert record.verdicts["winner"]["variant"] == "hard"
    assert sorted(entry["candidate_key"] for entry in record.pending) == [
        "greedy-s0-soft",
        "greedy-s0-soft-dither",
    ]
    assert (bundle_dir / "model" / "model.mpd").is_file()

    for lock in held:
        lock.release()
    pending_root = bundle_dir / "work" / "pending"
    _plant_result(
        pending_root / "greedy-s0-soft",
        identity=identity,
        candidate_key="greedy-s0-soft",
        strategy="greedy",
        variant="soft",
        objective=1.0,
    )
    _plant_result(
        pending_root / "greedy-s0-soft-dither",
        identity=identity,
        candidate_key="greedy-s0-soft-dither",
        strategy="greedy",
        variant="soft-dither",
        objective=2.0,
    )
    resumed = orchestrator.run_bundle(request)
    assert resumed.exit_code == 0
    record = read_record(bundle_dir)
    assert record is not None
    assert record.status == "complete"
    assert record.verdicts["provisional"] is False
    assert record.verdicts["winner"]["variant"] == "soft"
    assert record.pending == []
    report = json.loads((bundle_dir / "comparison" / "report.json").read_text())
    assert {row["variant"] for row in report["candidates"]} == {
        "hard",
        "soft",
        "soft-dither",
    }


def test_worker_cap_limits_concurrent_spawns(pair_npy, monkeypatch):
    from legolization import runtime

    config = ProjectConfig()
    clock = _FakeClock()
    monkeypatch.setattr(runtime, "logical_cpu_count", lambda: 1)
    spawned: list[str] = []
    locks: list[FileLock] = []

    def _fake_spawn(directory: Path, *, launcher=None) -> int:
        spawned.append(directory.name)
        lock = FileLock(directory / workers.LOCK_FILENAME)
        lock.acquire()
        locks.append(lock)
        clock.now += 10_000.0
        return 4242

    monkeypatch.setattr(workers, "spawn_worker", _fake_spawn)
    envelope = orchestrator.run_bundle(
        orchestrator.BundleRequest(
            input_path=pair_npy,
            config=config,
            quality="fast",
            clock=clock,
            render="off",
        )
    )
    try:
        assert len(spawned) == 1
        assert envelope.exit_code == 3
    finally:
        for lock in locks:
            lock.release()


def test_no_buildable_candidate_with_pending_is_indeterminate(
    pair_npy,
    monkeypatch,
):
    clock = _FakeClock()
    locks: list[FileLock] = []

    def _fake_spawn(directory: Path, *, launcher=None) -> int:
        lock = FileLock(directory / workers.LOCK_FILENAME)
        lock.acquire()
        locks.append(lock)
        clock.now = 10_000.0
        return 4242

    monkeypatch.setattr(workers, "spawn_worker", _fake_spawn)
    envelope = orchestrator.run_bundle(
        orchestrator.BundleRequest(
            input_path=pair_npy,
            config=ProjectConfig(),
            quality="fast",
            clock=clock,
            render="off",
        )
    )
    try:
        assert envelope.exit_code == 3
        record = read_record(pair_npy.parent / "pair-legolization")
        assert record is not None
        assert record.status == "partial"
        assert record.verdicts["buildable"] is False
        assert record.stages["model"].status == "skipped"
    finally:
        for lock in locks:
            lock.release()


def test_fast_quality_runs_real_worker_end_to_end(tmp_path):
    box = tmp_path / "box.npy"
    codes = np.full((4, 3, 2), -1, dtype=np.int16)
    codes[:, :, :] = 4
    np.save(box, codes)
    envelope = orchestrator.run_bundle(
        orchestrator.BundleRequest(
            input_path=box,
            config=ProjectConfig(),
            quality="fast",
            render="off",
        )
    )
    assert envelope.exit_code == 0
    bundle_dir = tmp_path / "box-legolization"
    record = read_record(bundle_dir)
    assert record is not None
    assert record.status == "complete"
    assert record.verdicts["winner"]["strategy"] == "greedy"
    assert record.stages["candidates"].detail["completed"] == 1
    assert (bundle_dir / "comparison" / "report.json").is_file()
    assert (bundle_dir / "model" / "model.mpd").is_file()
    assert (bundle_dir / "bom" / "bom.json").is_file()
