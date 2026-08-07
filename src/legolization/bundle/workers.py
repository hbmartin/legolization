"""Detached, identity-stamped worker lifecycle for bundle candidates.

Workers run in their own session so they survive the parent CLI exit
(soft deadlines leave them running). Liveness is proven by a
``filelock`` on ``stamp.lock`` held by the worker for its whole
lifetime — pid probes alone are unsafe under pid reuse. Cancellation
only ever touches workers whose stamp matches the invoking bundle
identity, and completed results are always retained for resume.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from filelock import FileLock, Timeout

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from legolization.bundle.identity import BundleIdentity

WORKER_SCHEMA = "legolization.worker/v1"
RESULT_SCHEMA = "legolization.candidate/v1"
STAMP_FILENAME = "stamp.json"
LOCK_FILENAME = "stamp.lock"
JOB_FILENAME = "job.json"
RESULT_FILENAME = "result.json"
LOG_FILENAME = "log.txt"

_CANCEL_GRACE_S = 5.0

type Launcher = Callable[[Path], subprocess.Popen[bytes]]


@dataclass(frozen=True, slots=True, kw_only=True)
class PendingWorker:
    """One worker directory whose stamp matches the bundle identity."""

    directory: Path
    candidate_key: str
    pid: int | None


@dataclass(frozen=True, slots=True, kw_only=True)
class CancelOutcome:
    """What cancellation did to one pending worker."""

    candidate_key: str
    action: Literal["cancelled", "stale"]
    pid: int | None


def pending_root(bundle_dir: Path) -> Path:
    """Return the bundle's pending-worker directory."""
    return bundle_dir / "work" / "pending"


def worker_dir(bundle_dir: Path, candidate_key: str) -> Path:
    """Return the directory for one candidate worker."""
    return pending_root(bundle_dir) / candidate_key


def prepare_worker(
    bundle_dir: Path,
    *,
    identity: BundleIdentity,
    candidate_key: str,
    job: Mapping[str, Any],
) -> Path:
    """Create the worker directory with its stamp and job description."""
    from legolization.eval_artifacts import atomic_json  # noqa: PLC0415

    directory = worker_dir(bundle_dir, candidate_key)
    directory.mkdir(parents=True, exist_ok=True)
    atomic_json(directory / JOB_FILENAME, dict(job))
    atomic_json(
        directory / STAMP_FILENAME,
        {
            "schema": WORKER_SCHEMA,
            "identity": identity.to_dict(),
            "candidate_key": candidate_key,
            "pid": None,
        },
    )
    return directory


def spawn_worker(directory: Path, *, launcher: Launcher | None = None) -> int:
    """Launch the detached worker process and stamp its pid."""
    from legolization.eval_artifacts import atomic_json  # noqa: PLC0415

    process = (_default_launcher if launcher is None else launcher)(directory)
    stamp = _read_stamp(directory)
    if stamp is None:
        msg = f"worker stamp missing under {directory}"
        raise OSError(msg)
    stamp["pid"] = process.pid
    atomic_json(directory / STAMP_FILENAME, stamp)
    return process.pid


def scan_pending(
    bundle_dir: Path,
    *,
    identity: BundleIdentity,
) -> tuple[PendingWorker, ...]:
    """Return the workers stamped with exactly this bundle identity."""
    root = pending_root(bundle_dir)
    if not root.is_dir():
        return ()
    matched: list[PendingWorker] = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        stamp = _read_stamp(directory)
        if stamp is None or stamp.get("identity") != identity.to_dict():
            continue
        pid = stamp.get("pid")
        matched.append(
            PendingWorker(
                directory=directory,
                candidate_key=str(stamp.get("candidate_key", directory.name)),
                pid=int(pid) if pid is not None else None,
            )
        )
    return tuple(matched)


def is_alive(worker: PendingWorker) -> bool:
    """Return whether the worker still holds its lifetime lock."""
    lock = FileLock(worker.directory / LOCK_FILENAME)
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return True
    lock.release()
    return False


def read_result(worker: PendingWorker) -> dict[str, Any] | None:
    """Load a completed worker result, rejecting corruption."""
    path = worker.directory / RESULT_FILENAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != RESULT_SCHEMA:
        return None
    return payload


def harvest_results(
    bundle_dir: Path,
    *,
    identity: BundleIdentity,
) -> tuple[tuple[PendingWorker, dict[str, Any]], ...]:
    """Collect valid completed results from identity-matched workers."""
    harvested: list[tuple[PendingWorker, dict[str, Any]]] = []
    for worker in scan_pending(bundle_dir, identity=identity):
        payload = read_result(worker)
        if payload is not None and payload.get("identity") == identity.to_dict():
            harvested.append((worker, payload))
    return tuple(harvested)


def cancel_workers(
    bundle_dir: Path,
    *,
    identity: BundleIdentity,
    grace_s: float = _CANCEL_GRACE_S,
) -> tuple[CancelOutcome, ...]:
    """Terminate live identity-matched workers; keep their artifacts.

    Dead stamps (lock free, no live process) are marked stale without a
    kill attempt, narrowing the pid-reuse window to lock-check → kill.
    """
    outcomes: list[CancelOutcome] = []
    for worker in scan_pending(bundle_dir, identity=identity):
        if not is_alive(worker) or worker.pid is None:
            outcomes.append(
                CancelOutcome(
                    candidate_key=worker.candidate_key,
                    action="stale",
                    pid=worker.pid,
                )
            )
            continue
        _terminate(worker, grace_s=grace_s)
        outcomes.append(
            CancelOutcome(
                candidate_key=worker.candidate_key,
                action="cancelled",
                pid=worker.pid,
            )
        )
    return tuple(outcomes)


def _terminate(worker: PendingWorker, *, grace_s: float) -> None:
    pid = worker.pid
    if pid is None:
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if not is_alive(worker):
            return
        time.sleep(0.1)
    kill_signal = getattr(signal, "SIGKILL", signal.SIGTERM)
    try:
        os.kill(pid, kill_signal)
    except (ProcessLookupError, PermissionError):
        return


def _default_launcher(directory: Path) -> subprocess.Popen[bytes]:
    command = [
        sys.executable,
        "-m",
        "legolization.bundle.worker_main",
        str(directory),
    ]
    with (directory / LOG_FILENAME).open("ab") as log:
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            return subprocess.Popen(  # noqa: S603 - fixed interpreter module
                command,
                stdout=log,
                stderr=log,
                creationflags=flags,
            )
        return subprocess.Popen(  # noqa: S603 - fixed interpreter module
            command,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def _read_stamp(directory: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((directory / STAMP_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != WORKER_SCHEMA:
        return None
    return payload
