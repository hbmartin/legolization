"""Detached worker entry point: ``python -m legolization.bundle.worker_main``.

The worker holds ``stamp.lock`` for its whole lifetime (the liveness
oracle), executes the job described in ``job.json``, and atomically
writes ``result.json``. The ``candidate`` kind runs one
(strategy, seed, colour-variant) pipeline case; the trivial kinds
exist for lifecycle tests.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Literal, cast

from filelock import FileLock

from legolization.bundle.workers import (
    JOB_FILENAME,
    LOCK_FILENAME,
    MODEL_FILENAME,
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
        result = _execute(job, directory)
        result.update(
            {
                "schema": RESULT_SCHEMA,
                "identity": stamp.get("identity"),
                "candidate_key": stamp.get("candidate_key"),
            }
        )
        atomic_json(directory / RESULT_FILENAME, result)
    return 0 if result.get("status") == "ok" else 1


def _execute(job: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Run the job body for its declared kind."""
    match job.get("kind"):
        case "noop":
            return {"status": "ok"}
        case "sleep":
            time.sleep(float(job.get("seconds", 1.0)))
            return {"status": "ok"}
        case "candidate":
            return _run_candidate(job, directory)
        case unknown:
            return {"status": "error", "error": f"unknown job kind {unknown!r}"}


def _run_candidate(job: dict[str, Any], directory: Path) -> dict[str, Any]:
    """Run one placement case and score it for selection."""
    from legolization.errors import LegolizationError  # noqa: PLC0415

    started = time.perf_counter()
    base = {
        "strategy": str(job["strategy"]),
        "seed": int(job["seed"]),
        "variant": str(job["variant"]["name"]),
    }
    try:
        payload = _candidate_payload(job, directory)
    except (LegolizationError, OSError, ValueError, RuntimeError) as error:
        return {
            **base,
            "status": "error",
            "error": str(error),
            "seconds": round(time.perf_counter() - started, 3),
        }
    payload.update(base)
    payload["seconds"] = round(time.perf_counter() - started, 3)
    return payload


def _candidate_payload(job: dict[str, Any], directory: Path) -> dict[str, Any]:
    from dataclasses import asdict, replace  # noqa: PLC0415

    from legolization.compare import candidate_metrics  # noqa: PLC0415
    from legolization.configuration import (  # noqa: PLC0415
        project_config_from_mapping,
    )
    from legolization.instructions.sequencer import InstructionsConfig  # noqa: PLC0415
    from legolization.ldraw_out import write_model  # noqa: PLC0415
    from legolization.pipeline import run  # noqa: PLC0415
    from legolization.placement.base import evaluate  # noqa: PLC0415

    config = project_config_from_mapping(dict(job["config"]))
    variant = dict(job["variant"])
    pipeline = replace(
        config.to_pipeline(strategy=str(job["strategy"])),
        seed=int(job["seed"]),
        colour_mode=cast("Literal['hard', 'soft']", variant["colour_mode"]),
        dither=bool(variant["dither"]),
        instructions=InstructionsConfig(mode="layer"),
    )
    input_path = Path(str(job["input_path"]))
    grid = _variant_grid(input_path, pipeline, retile=bool(job.get("retile")))
    result = run(grid, pipeline)
    metrics = candidate_metrics(
        result,
        weights=pipeline.weights,
        solver_config=pipeline.solver,
    )
    if bool(variant["dither"]):
        canonical = _variant_grid(
            input_path,
            replace(pipeline, dither=False),
            retile=bool(job.get("retile")),
        )
    else:
        canonical = grid
    canonical_report = evaluate(
        result.layout,
        canonical,
        weights=pipeline.weights,
        solver_config=pipeline.solver,
    )
    write_model(result.layout, directory / MODEL_FILENAME)
    return {
        "status": "ok",
        "metrics": asdict(metrics),
        "selection_objective": canonical_report.total,
        "cross_colour_error": canonical_report.colour_error,
    }


def _variant_grid(input_path: Path, pipeline: object, *, retile: bool) -> Any:  # noqa: ANN401
    from legolization.pipeline import PipelineConfig, load_grid  # noqa: PLC0415

    assert isinstance(pipeline, PipelineConfig)  # noqa: S101 - internal seam
    if retile:
        from legolization.layout import occupancy_grid  # noqa: PLC0415
        from legolization.ldraw_in import import_ldraw  # noqa: PLC0415

        grid, _, _ = occupancy_grid(import_ldraw(input_path).layout)
        return grid
    return load_grid(input_path, pipeline)


if __name__ == "__main__":
    sys.exit(main())
