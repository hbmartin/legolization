"""Profile one isolated pipeline run under a per-stage watchdog.

A parent process supervises a single child running inside a telemetry
recording session (see ``legolization.telemetry``) and writes
``eval/profiles/<UTC>-<name>-<strategy>.json`` capturing per-span call
counts and wall seconds, run metadata, and the git sha — so a claim like
"build_model dominates" can be re-checked after any change against the
same pinned inputs. ``--cprofile`` additionally writes a sibling
``.pstats`` for line-level drilling (it inflates telemetry seconds, so
never compare timings across that flag; call counts stay comparable).
Every watched stage gets a fresh timeout, transitions print immediately,
and heartbeats expose elapsed time. A timeout terminates the child and
atomically preserves the active stage plus completed telemetry.

Usage::

    uv run python scripts/profile_pipeline.py MODEL [--strategy greedy]
        [--seed 0] [--target-studs N] [--up x|y|z] [--label TEXT]
        [--out eval/profiles] [--cprofile] [--solid] [--no-repair]
        [--steps smart|layer] [--stage-timeout 600] [--heartbeat 30]

MODEL is a ``.vox/.npy/.obj/.stl/.ply`` path or a corpus manifest name
(synthetic models regenerate in memory; meshes use their manifest
``target_studs``/``up`` — the explicit flags apply to file paths only).
Timings from parallel sweeps are out of scope: telemetry does not cross
spawn workers, so this script always profiles one strategy in one isolated
child.
"""

from __future__ import annotations

import argparse
import cProfile
import hashlib
import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from legolization import telemetry
from legolization.eval_artifacts import atomic_json
from legolization.instructions.sequencer import InstructionsConfig
from legolization.mesh import MeshOptions
from legolization.pipeline import PipelineConfig, PipelineResult, load_grid, run

if TYPE_CHECKING:
    from legolization.grid import VoxelGrid

_SCRIPTS = Path(__file__).resolve().parent
_REPO = _SCRIPTS.parent
PROFILES = _REPO / "eval" / "profiles"
_WATCHED_STAGES = frozenset(
    {
        "phase.voxelize",
        "phase.place",
        "place.tile",
        "place.compact",
        "place.connectivity",
        "stability.analyze",
        "phase.repair",
    }
)
# These enclosing stages own any stability solves they invoke. Without this
# ordering, a nested ``stability.analyze`` repeatedly resets the watchdog for
# a long connectivity or stability-repair pass and misattributes its time.
_STAGE_OWNERSHIP = (
    "phase.repair",
    "place.connectivity",
    "place.compact",
    "place.tile",
    "phase.voxelize",
    "phase.place",
    "stability.analyze",
)


git_sha = telemetry.git_sha
"""Shared with the CLI --profile writer (legolization.telemetry)."""


@dataclass(frozen=True, slots=True)
class ResolvedInput:
    """A model resolved to a grid plus the identity that produced it.

    ``mesh`` holds the EFFECTIVE options — for corpus names the manifest
    values actually used, not the CLI flags (PR #18 review: two profiles
    of the same corpus model at different ``--target-studs`` recorded
    different identities for identical runs). ``input_hash`` pins the
    bytes: sha256 for files, ``generator:<name>`` for synthetics.
    """

    name: str
    input: str
    grid: VoxelGrid
    source: str  # "file" | "manifest" | "synthetic"
    mesh: MeshOptions | None
    input_hash: str

    def run_identity(self) -> dict[str, object]:
        """Return the payload fragment both profile writers stamp."""
        return {
            "model": self.name,
            "input": self.input,
            "input_source": self.source,
            "input_hash": self.input_hash,
            "target_studs": self.mesh.target_studs if self.mesh else None,
            "up": self.mesh.up if self.mesh else None,
            "keep_largest": self.mesh.keep_largest if self.mesh else None,
        }


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_grid(
    model: str,
    config: PipelineConfig,
) -> ResolvedInput:
    """Resolve MODEL to a grid with its effective input identity."""
    path = Path(model)
    if path.suffix and path.exists():
        return ResolvedInput(
            name=path.stem,
            input=str(path),
            grid=load_grid(path, config),
            source="file",
            mesh=config.mesh,
            input_hash=_sha256_of(path),
        )
    spec_path = _SCRIPTS / "eval_corpus.py"
    import importlib.util  # noqa: PLC0415 - only needed for corpus names

    spec = importlib.util.spec_from_file_location("eval_corpus_script", spec_path)
    if spec is None or spec.loader is None:
        msg = "cannot load scripts/eval_corpus.py"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    corpus = module.load_corpus_module()
    matches = [m for m in corpus.load_manifest() if m.name == model]
    if not matches:
        msg = f"{model!r} is neither an existing input file nor a corpus model"
        raise SystemExit(msg)
    entry = matches[0]
    grid = module.model_grid(corpus, entry)
    if grid is None:
        msg = f"corpus mesh {model!r} is not on disk; run scripts/corpus.py download"
        raise SystemExit(msg)
    mesh_options = module.model_mesh_options(entry)
    return ResolvedInput(
        name=entry.name,
        input=str(entry.path),
        grid=grid,
        source="synthetic" if mesh_options is None else "manifest",
        mesh=mesh_options,
        input_hash=(
            f"generator:{entry.generator}"
            if mesh_options is None
            else _sha256_of(entry.abs_path)
        ),
    )


def _run_profiled(
    grid: VoxelGrid,
    config: PipelineConfig,
    *,
    pstats_path: Path | None,
) -> tuple[PipelineResult, telemetry.Telemetry, float]:
    """Execute the pipeline under recording; returns (result, spans, wall)."""
    started = time.perf_counter()
    with telemetry.record() as session:
        if pstats_path is not None:
            profiler = cProfile.Profile()
            result = profiler.runcall(run, grid, config)
            profiler.dump_stats(pstats_path)
        else:
            result = run(grid, config)
    return result, session, time.perf_counter() - started


@dataclass(slots=True)
class _Lifecycle:
    """Checkpoint watched stage transitions for the supervising process."""

    path: Path
    model: str
    stack: list[tuple[str, float]]
    active_stage: str | None = None
    active_started: float | None = None
    last_write: float = 0.0

    @classmethod
    def create(cls, path: Path, model: str) -> _Lifecycle:
        return cls(path=path, model=model, stack=[])

    def __call__(
        self,
        event: str,
        name: str,
        now: float,
        session: telemetry.Telemetry,
    ) -> None:
        """Update the owning stage and periodically checkpoint spans."""
        if event == "start":
            self.stack.append((name, now))
        else:
            for index in range(len(self.stack) - 1, -1, -1):
                if self.stack[index][0] == name:
                    self.stack.pop(index)
                    break
        present = {
            span_name for span_name, _ in self.stack if span_name in _WATCHED_STAGES
        }
        active = next(
            (name for name in _STAGE_OWNERSHIP if name in present),
            None,
        )
        changed = active != self.active_stage
        if changed:
            self.active_stage = active
            self.active_started = next(
                (
                    started
                    for span_name, started in reversed(self.stack)
                    if span_name == active
                ),
                None,
            )
        if changed or now - self.last_write >= 5.0:
            self.last_write = now
            atomic_json(
                self.path,
                {
                    "model": self.model,
                    "active_stage": self.active_stage,
                    "stage_started": self.active_started,
                    "updated": now,
                    "spans": session.to_dict(),
                },
            )


def _profile_worker(
    args: argparse.Namespace,
    *,
    base: Path,
    events_path: Path,
) -> int:
    """Resolve and profile one strategy inside the supervised child."""
    config = PipelineConfig(
        strategy=args.strategy,
        seed=args.seed,
        hollow=not args.solid,
        repair=not args.no_repair,
        instructions=InstructionsConfig(mode=args.steps),
        mesh=MeshOptions(target_studs=args.target_studs, up=args.up),
        progress=lambda message: print(f"  {message}", file=sys.stderr),
    )
    pstats_path = base.with_suffix(".pstats") if args.cprofile else None
    lifecycle = _Lifecycle.create(events_path, args.model)
    profiler = cProfile.Profile() if args.cprofile else None
    started = time.perf_counter()
    with telemetry.record(span_sink=lifecycle) as session:
        if profiler is not None:
            profiler.enable()
        with telemetry.span("phase.voxelize"):
            resolved = _resolve_grid(args.model, config)
        result = run(resolved.grid, config)
        if profiler is not None:
            profiler.disable()
            assert pstats_path is not None  # noqa: S101 - paired construction
            profiler.dump_stats(pstats_path)
    total_seconds = time.perf_counter() - started
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "schema": 1,
        "status": "ok",
        "generated": stamp,
        "git_sha": git_sha(),
        "label": args.label,
        "host": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "run": {
            **resolved.run_identity(),
            "strategy": args.strategy,
            "seed": args.seed,
            "hollow": not args.solid,
            "repair": not args.no_repair,
            "steps": args.steps,
        },
        "result": {
            "brick_count": result.brick_count,
            "step_count": result.step_count,
            "mass_g": round(result.mass_g, 2),
            "stable": result.stability.stable,
            "buildable": result.buildable,
        },
        "total_seconds": round(total_seconds, 3),
        "stage_timeout_seconds": args.stage_timeout,
        "cprofile_active": args.cprofile,
        "cprofile_path": str(pstats_path) if pstats_path is not None else None,
        "spans": session.to_dict(),
    }
    atomic_json(base.with_suffix(".json"), payload)
    events_path.unlink(missing_ok=True)
    print(f"wrote {base.with_suffix('.json')}")
    if pstats_path is not None:
        print(f"wrote {pstats_path}")
    _print_profile(session, total_seconds=total_seconds, result=result)
    return 0


def _print_profile(
    session: telemetry.Telemetry,
    *,
    total_seconds: float,
    result: PipelineResult,
) -> None:
    """Print the traditional human-readable profile table."""
    rows = sorted(session.spans.items(), key=lambda item: -item[1].seconds)
    print(f"{'span':<28} {'calls':>7} {'seconds':>10}")
    for span_name, stats in rows:
        print(f"{span_name:<28} {stats.calls:>7} {stats.seconds:>10.3f}")
    print(
        f"{'TOTAL (wall)':<28} {'':>7} {total_seconds:>10.3f}   "
        f"bricks={result.brick_count} steps={result.step_count}"
    )


def _validate_model_reference(model: str) -> None:
    """Fail before spawning when MODEL is neither a file nor manifest name."""
    path = Path(model)
    if path.suffix and path.exists():
        return
    spec = importlib.util.spec_from_file_location(
        "eval_corpus_script_for_profile_validation",
        _SCRIPTS / "eval_corpus.py",
    )
    if spec is None or spec.loader is None:
        msg = "cannot load scripts/eval_corpus.py"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    corpus = module.load_corpus_module()
    if model not in {entry.name for entry in corpus.load_manifest()}:
        msg = f"{model!r} is neither an existing input file nor a corpus model"
        raise SystemExit(msg)


def _monitor(  # noqa: C901 - lifecycle loop keeps timeout state together
    process: subprocess.Popen[str],
    *,
    args: argparse.Namespace,
    base: Path,
    events_path: Path,
) -> int:
    """Print stage progress and terminate a child that exceeds its stage cap."""
    active: str | None = None
    last_heartbeat = time.monotonic()
    latest: dict[str, object] = {}
    while process.poll() is None:
        time.sleep(0.25)
        try:
            latest = json.loads(events_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        stage = latest.get("active_stage")
        stage_name = str(stage) if stage is not None else None
        if stage_name != active:
            active = stage_name
            if active is not None:
                print(f"  stage: {active}", file=sys.stderr)
            last_heartbeat = time.monotonic()
        started = latest.get("stage_started")
        if active is None or not isinstance(started, int | float):
            continue
        elapsed = time.monotonic() - float(started)
        if time.monotonic() - last_heartbeat >= args.heartbeat:
            print(f"  {active}: {elapsed:.0f}s elapsed", file=sys.stderr)
            last_heartbeat = time.monotonic()
        if elapsed <= args.stage_timeout:
            continue
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if process.stdout is not None and (output := process.stdout.read()):
            print(output, end="")
        payload = {
            "schema": 1,
            "status": "timed_out",
            "generated": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
            "git_sha": git_sha(),
            "run": {
                "model": args.model,
                "strategy": args.strategy,
                "seed": args.seed,
                "steps": args.steps,
            },
            "result": None,
            "total_seconds": None,
            "active_stage": active,
            "active_stage_seconds": round(elapsed, 3),
            "stage_timeout_seconds": args.stage_timeout,
            "spans": latest.get("spans", {}),
        }
        atomic_json(base.with_suffix(".json"), payload)
        print(
            f"timed out {active} after {elapsed:.1f}s; "
            f"wrote {base.with_suffix('.json')}",
            file=sys.stderr,
        )
        events_path.unlink(missing_ok=True)
        return 124
    if process.stdout is not None and (output := process.stdout.read()):
        print(output, end="")
    events_path.unlink(missing_ok=True)
    return process.returncode or 0


def main(argv: list[str] | None = None) -> int:
    """Supervise one isolated profile worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model")
    parser.add_argument("--strategy", default="greedy")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--target-studs", type=int, default=32, metavar="N")
    parser.add_argument("--up", choices=("x", "y", "z"), default="z")
    parser.add_argument("--label", default="")
    parser.add_argument("--out", type=Path, default=PROFILES)
    parser.add_argument("--cprofile", action="store_true")
    parser.add_argument("--solid", action="store_true")
    parser.add_argument("--no-repair", action="store_true")
    parser.add_argument("--steps", choices=("smart", "layer"), default="smart")
    parser.add_argument("--stage-timeout", type=float, default=600.0, metavar="SECONDS")
    parser.add_argument("--heartbeat", type=float, default=30.0, metavar="SECONDS")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--base", type=Path, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--events", type=Path, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.stage_timeout <= 0 or args.heartbeat <= 0:
        parser.error("--stage-timeout and --heartbeat must be positive")
    if args.worker:
        if args.base is None or args.events is None:
            parser.error("worker mode requires --base and --events")
        return _profile_worker(args, base=args.base, events_path=args.events)

    _validate_model_reference(args.model)
    name = Path(args.model).stem if Path(args.model).suffix else args.model
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    args.out.mkdir(parents=True, exist_ok=True)
    base = args.out / f"{stamp}-{name}-{args.strategy}"
    events_path = base.with_suffix(".running.json")
    raw_args = list(argv) if argv is not None else sys.argv[1:]
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        *raw_args,
        "--worker",
        "--base",
        str(base),
        "--events",
        str(events_path),
    ]
    process = subprocess.Popen(  # noqa: S603 - fixed current interpreter
        command,
        stdout=subprocess.PIPE,
        text=True,
    )
    return _monitor(
        process,
        args=args,
        base=base,
        events_path=events_path,
    )


if __name__ == "__main__":
    sys.exit(main())
