"""Detached worker lifecycle: stamps, liveness, harvest, cancel isolation."""

from __future__ import annotations

import time

import pytest

from legolization.bundle.identity import BundleIdentity
from legolization.bundle.workers import (
    RESULT_FILENAME,
    cancel_workers,
    harvest_results,
    is_alive,
    prepare_worker,
    read_result,
    scan_pending,
    spawn_worker,
)

IDENTITY = BundleIdentity(
    input_sha256="a" * 64,
    config_sha256="b" * 64,
    legolization_version="0.6.0",
    catalog_sha256="c" * 64,
)
FOREIGN = BundleIdentity(
    input_sha256="d" * 64,
    config_sha256="e" * 64,
    legolization_version="0.6.0",
    catalog_sha256="f" * 64,
)


def _wait_until(predicate, *, timeout_s: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_scan_pending_matches_only_this_identity(tmp_path):
    prepare_worker(
        tmp_path,
        identity=IDENTITY,
        candidate_key="bond-s0",
        job={"kind": "noop"},
    )
    prepare_worker(
        tmp_path,
        identity=FOREIGN,
        candidate_key="foreign-s0",
        job={"kind": "noop"},
    )
    matched = scan_pending(tmp_path, identity=IDENTITY)
    assert [worker.candidate_key for worker in matched] == ["bond-s0"]


def test_worker_completes_and_is_harvested(tmp_path):
    directory = prepare_worker(
        tmp_path,
        identity=IDENTITY,
        candidate_key="bond-s0",
        job={"kind": "noop"},
    )
    pid = spawn_worker(directory)
    assert pid > 0
    assert _wait_until((directory / RESULT_FILENAME).is_file)
    harvested = harvest_results(tmp_path, identity=IDENTITY)
    assert len(harvested) == 1
    worker, payload = harvested[0]
    assert worker.candidate_key == "bond-s0"
    assert payload["status"] == "ok"
    assert payload["identity"] == IDENTITY.to_dict()
    assert _wait_until(lambda: not is_alive(worker))


def test_harvest_rejects_corrupt_results(tmp_path):
    directory = prepare_worker(
        tmp_path,
        identity=IDENTITY,
        candidate_key="bond-s0",
        job={"kind": "noop"},
    )
    (directory / RESULT_FILENAME).write_text("{corrupt")
    assert harvest_results(tmp_path, identity=IDENTITY) == ()
    (worker,) = scan_pending(tmp_path, identity=IDENTITY)
    assert read_result(worker) is None


def test_cancel_terminates_only_identity_matched_workers(tmp_path):
    mine = prepare_worker(
        tmp_path,
        identity=IDENTITY,
        candidate_key="mine",
        job={"kind": "sleep", "seconds": 60},
    )
    other = prepare_worker(
        tmp_path,
        identity=FOREIGN,
        candidate_key="other",
        job={"kind": "sleep", "seconds": 60},
    )
    spawn_worker(mine)
    spawn_worker(other)
    (mine_worker,) = scan_pending(tmp_path, identity=IDENTITY)
    (other_worker,) = scan_pending(tmp_path, identity=FOREIGN)
    try:
        assert _wait_until(lambda: is_alive(mine_worker))
        assert _wait_until(lambda: is_alive(other_worker))
        outcomes = cancel_workers(tmp_path, identity=IDENTITY)
        assert [outcome.action for outcome in outcomes] == ["cancelled"]
        assert _wait_until(lambda: not is_alive(mine_worker))
        assert is_alive(other_worker)
    finally:
        cancel_workers(tmp_path, identity=FOREIGN)
    assert _wait_until(lambda: not is_alive(other_worker))


def test_cancel_marks_dead_stamp_stale_without_killing(tmp_path):
    prepare_worker(
        tmp_path,
        identity=IDENTITY,
        candidate_key="never-started",
        job={"kind": "noop"},
    )
    outcomes = cancel_workers(tmp_path, identity=IDENTITY)
    assert [outcome.action for outcome in outcomes] == ["stale"]


@pytest.mark.parametrize("kind", ["unknown-kind"])
def test_worker_reports_unknown_job_kinds(tmp_path, kind):
    directory = prepare_worker(
        tmp_path,
        identity=IDENTITY,
        candidate_key="bad",
        job={"kind": kind},
    )
    spawn_worker(directory)
    assert _wait_until((directory / RESULT_FILENAME).is_file)
    (worker,) = scan_pending(tmp_path, identity=IDENTITY)
    payload = read_result(worker)
    assert payload is not None
    assert payload["status"] == "error"
    harvested = harvest_results(tmp_path, identity=IDENTITY)
    assert len(harvested) == 1
