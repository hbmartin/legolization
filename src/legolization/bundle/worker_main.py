"""Detached worker entry point: ``python -m legolization.bundle.worker_main``.

The worker holds ``stamp.lock`` for its whole lifetime (the liveness
oracle), executes the job described in ``job.json``, and atomically
writes ``result.json``. Job kinds beyond the trivial test kinds land
with the candidate-execution phase.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from filelock import FileLock

from legolization.bundle.workers import (
    JOB_FILENAME,
    LOCK_FILENAME,
    RESULT_FILENAME,
    RESULT_SCHEMA,
    STAMP_FILENAME,
)


def main(argv: list[str] | None = None) -> int:
    """Run one worker job to completion."""
    from legolization.eval_artifacts import atomic_json  # noqa: PLC0415

    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1:
        print("usage: worker_main WORKER_DIR", file=sys.stderr)
        return 1
    directory = Path(arguments[0])
    stamp = json.loads((directory / STAMP_FILENAME).read_text(encoding="utf-8"))
    with FileLock(directory / LOCK_FILENAME):
        job = json.loads((directory / JOB_FILENAME).read_text(encoding="utf-8"))
        result = _execute(job)
        result.update(
            {
                "schema": RESULT_SCHEMA,
                "identity": stamp.get("identity"),
                "candidate_key": stamp.get("candidate_key"),
            }
        )
        atomic_json(directory / RESULT_FILENAME, result)
    return 0 if result.get("status") == "ok" else 1


def _execute(job: dict[str, Any]) -> dict[str, Any]:
    """Run the job body; test kinds only until candidate execution lands."""
    match job.get("kind"):
        case "noop":
            return {"status": "ok"}
        case "sleep":
            time.sleep(float(job.get("seconds", 1.0)))
            return {"status": "ok"}
        case unknown:
            return {"status": "error", "error": f"unknown job kind {unknown!r}"}


if __name__ == "__main__":
    sys.exit(main())
