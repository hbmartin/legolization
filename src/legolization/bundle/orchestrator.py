"""The bundle stage machine: run, resume, regenerate, interrupt, cancel.

The orchestrator is the single writer of ``bundle.json`` (guarded by a
file lock). Every stage boundary persists atomically, so a SIGINT at
any point leaves a resumable record (exit 130). On resume, completed
stages whose artifacts still hash-match are skipped; a drifted artifact
reruns only its producing stage, relying on seed-deterministic
regeneration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from legolization.bundle.identity import BundleIdentity, bundle_identity
from legolization.bundle.paths import ResumeDecision, resolve_output_dir
from legolization.bundle.record import (
    BundleRecord,
    read_identity_key,
    read_record,
    source_payload,
    versions_payload,
    write_record,
)
from legolization.cli.envelope import ArtifactRecord, ResultEnvelope
from legolization.cli.exit_codes import (
    COMPLETE,
    INTERRUPTED,
    OPERATIONAL_ERROR,
    UNBUILDABLE,
)
from legolization.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from legolization.bundle.paths import BundleFlavor
    from legolization.configuration import ProjectConfig
    from legolization.grid import VoxelGrid
    from legolization.ldraw_in import ImportedLdrawModel
    from legolization.pipeline import PipelineResult

NATIVE_SUFFIXES = {".vox", ".npy", ".obj", ".stl", ".ply"}
LDRAW_SUFFIXES = {".ldr", ".mpd"}


@dataclass(frozen=True, slots=True, kw_only=True)
class BundleRequest:
    """One noninteractive bundle invocation."""

    input_path: Path
    config: ProjectConfig
    output_dir: Path | None = None
    quality: Literal["fast", "balanced", "exhaustive", "direct"] = "direct"
    duration_s: float | None = None
    fresh: bool = False
    cancel_pending: bool = False
    render: Literal["auto", "required", "off"] = "auto"
    retile: bool = False

    def invocation_payload(self) -> dict[str, object]:
        """Result-affecting request options folded into the identity."""
        payload: dict[str, object] = {}
        if self.retile:
            payload["retile"] = True
        return payload


type BundleMode = Literal["generate", "preserve", "retile"]


@dataclass(slots=True, kw_only=True)
class _BundleContext:
    """Mutable working state shared by the stage runners."""

    request: BundleRequest
    record: BundleRecord
    directory: Path
    resume: bool
    mode: BundleMode = "generate"
    grid: VoxelGrid | None = None
    result: PipelineResult | None = None
    imported: ImportedLdrawModel | None = None
    reruns: list[str] = field(default_factory=list)

    def ensure_imported(self) -> ImportedLdrawModel:
        """Import the LDraw source on first use (strict, warning-carrying)."""
        if self.imported is None:
            from legolization.ldraw_in import import_ldraw  # noqa: PLC0415

            self.imported = import_ldraw(self.request.input_path)
        return self.imported

    def ensure_grid(self) -> VoxelGrid:
        """Load or derive the target grid on first use."""
        if self.grid is None:
            if self.mode == "retile":
                from legolization.layout import occupancy_grid  # noqa: PLC0415

                self.grid, _, _ = occupancy_grid(self.ensure_imported().layout)
            else:
                from legolization.pipeline import load_grid  # noqa: PLC0415

                pipeline = self.request.config.to_pipeline(strategy="bond")
                self.grid = load_grid(self.request.input_path, pipeline)
        return self.grid

    def ensure_result(self) -> PipelineResult:
        """Produce the working result on first use.

        Generation modes run the pipeline (deterministic at fixed seed);
        preserve mode analyzes the imported assembly as-is.
        """
        if self.result is None:
            self.result = (
                self._imported_result()
                if self.mode == "preserve"
                else self._generated_result()
            )
        return self.result

    def _generated_result(self) -> PipelineResult:
        from legolization.cli.build import select_strategy  # noqa: PLC0415
        from legolization.pipeline import run  # noqa: PLC0415

        grid = self.ensure_grid()
        config = self.request.config
        strategy, automatic = select_strategy(config, grid)
        return run(
            grid,
            config.to_pipeline(strategy=strategy, automatic=automatic),
        )

    def _imported_result(self) -> PipelineResult:
        from legolization.graph import ConnectionGraph  # noqa: PLC0415
        from legolization.pipeline import PipelineResult  # noqa: PLC0415
        from legolization.stability.solver import analyze  # noqa: PLC0415

        layout = self.ensure_imported().layout
        stability = analyze(
            layout,
            self.request.config.stability.effective_solver(),
        )
        graph = ConnectionGraph.from_layout(layout)
        return PipelineResult(
            layout=layout,
            stability=stability,
            grid=None,
            brick_count=len(layout),
            mass_g=layout.total_mass_g(),
            component_count=graph.component_count(),
            floating_count=len(graph.floating_ids()),
        )


type _StageRunner = Callable[[_BundleContext], None]


def run_bundle(request: BundleRequest) -> ResultEnvelope:
    """Run (or resume, or cancel) one bundle invocation."""
    from filelock import FileLock, Timeout  # noqa: PLC0415

    _validate_input(request)
    identity = bundle_identity(
        request.input_path,
        request.config,
        invocation=request.invocation_payload(),
    )
    decision = resolve_output_dir(
        request.input_path,
        request.output_dir,
        flavor=_flavor(request),
        identity_key=identity.key(),
        fresh=request.fresh,
        read_key=read_identity_key,
    )
    if request.cancel_pending:
        return _cancel_pending(request, identity, decision)
    decision.directory.mkdir(parents=True, exist_ok=True)
    lock = FileLock(decision.directory / "bundle.lock")
    try:
        lock.acquire(timeout=0)
    except Timeout:
        msg = f"another process is writing the bundle at {decision.directory}"
        raise ConfigurationError(msg) from None
    try:
        return _run_stages(request, identity, decision)
    finally:
        lock.release()


def _run_stages(
    request: BundleRequest,
    identity: BundleIdentity,
    decision: ResumeDecision,
) -> ResultEnvelope:
    context = _BundleContext(
        request=request,
        record=_load_or_create_record(request, identity, decision),
        directory=decision.directory,
        resume=decision.resume,
        mode=_mode(request),
    )
    stages: tuple[tuple[str, _StageRunner], ...] = (
        ("ingest", _stage_ingest),
        ("generate", _stage_generate),
        ("model", _stage_model),
        ("bom", _stage_bom),
    )
    try:
        for name, runner in stages:
            _run_stage(context, name, runner)
    except KeyboardInterrupt:
        _mark_interrupted(context)
        return _envelope(context, exit_code=INTERRUPTED)
    return _finalize(context)


def _run_stage(context: _BundleContext, name: str, runner: _StageRunner) -> None:
    stage = context.record.stage(name)
    if (
        context.resume
        and stage.status in {"complete", "skipped"}
        and _artifacts_valid(context, name)
    ):
        return
    if stage.status == "complete":
        context.reruns.append(name)
        stage.detail["regenerated"] = True
    stage.status = "running"
    stage.error = None
    write_record(context.record, context.directory)
    try:
        runner(context)
    except KeyboardInterrupt:
        stage.status = "interrupted"
        raise
    except (ConfigurationError, OSError, ValueError, RuntimeError) as error:
        stage.status = "failed"
        stage.error = str(error)
        context.record.status = "error"
        context.record.exit_code = OPERATIONAL_ERROR
        write_record(context.record, context.directory)
        raise
    if stage.status == "running":
        stage.status = "complete"
    write_record(context.record, context.directory)


def _artifacts_valid(context: _BundleContext, stage_name: str) -> bool:
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415

    for entry in context.record.artifacts:
        if entry.stage != stage_name:
            continue
        path = context.directory / entry.path
        if not path.is_file():
            return False
        if entry.sha256 is not None and input_sha256(path) != entry.sha256:
            return False
    return True


def _stage_ingest(context: _BundleContext) -> None:
    stage = context.record.stage("ingest")
    stage.detail["format"] = context.request.input_path.suffix.lower().lstrip(".")
    stage.detail["mode"] = context.mode
    if context.mode == "preserve":
        imported = context.ensure_imported()
        stage.detail.update(
            {
                "shape_authority": "imported-assembly",
                "brick_count": len(imported.layout),
            }
        )
        _record_import_warnings(context, imported)
        return
    if context.mode == "retile":
        imported = context.ensure_imported()
        stage.detail["shape_authority"] = "imported-assembly"
        _record_import_warnings(context, imported)
    grid = context.ensure_grid()
    stage.detail.update(
        {
            "filled_count": int(grid.filled_count),
            "shape": list(grid.codes.shape),
        }
    )


def _record_import_warnings(
    context: _BundleContext,
    imported: ImportedLdrawModel,
) -> None:
    from ldraw import Severity  # noqa: PLC0415

    for diagnostic in imported.diagnostics:
        if diagnostic.severity is Severity.WARNING:
            message = f"{diagnostic.code}: {diagnostic.message}"
            if message not in context.record.warnings:
                context.record.warnings.append(message)


def _stage_generate(context: _BundleContext) -> None:
    stage = context.record.stage("generate")
    result = context.ensure_result()
    if context.mode == "preserve":
        stage.status = "skipped"
        stage.detail.update(
            {
                "reason": "imported assembly preserved",
                "buildable": result.buildable,
                "stable": result.stability.stable,
                "brick_count": result.brick_count,
            }
        )
    else:
        stage.detail.update(
            {
                "strategy": result.placement_strategy,
                "seed": context.request.config.placement.seed,
                "buildable": result.buildable,
                "stable": result.stability.stable,
                "brick_count": result.brick_count,
            }
        )
    winner = (
        {"strategy": "imported", "seed": None}
        if context.mode == "preserve"
        else {
            "strategy": result.placement_strategy,
            "seed": context.request.config.placement.seed,
        }
    )
    context.record.verdicts.update(
        {
            "buildable": result.buildable,
            "stable": result.stability.stable,
            "provisional": False,
            "winner": winner,
        }
    )
    if result.plan is not None:
        for warning in result.plan.warnings:
            if warning not in context.record.warnings:
                context.record.warnings.append(warning)


def _stage_model(context: _BundleContext) -> None:
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415
    from legolization.ldraw_out import write_model  # noqa: PLC0415

    result = context.ensure_result()
    model_dir = context.directory / "model"
    model_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("model.mpd", "model.ldr"):
        path = model_dir / filename
        write_model(result.layout, path, plan=result.plan)
        context.record.record_artifact(
            path=f"model/{filename}",
            stage="model",
            kind="model",
            sha256=input_sha256(path),
        )


def _stage_bom(context: _BundleContext) -> None:
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415
    from legolization.instructions.bom import bill_of_materials  # noqa: PLC0415

    result = context.ensure_result()
    bom_dir = context.directory / "bom"
    bom_dir.mkdir(parents=True, exist_ok=True)
    path = bom_dir / "bom.json"
    bom = bill_of_materials(result.layout)
    path.write_text(
        bom.to_json(model_name=context.request.input_path.stem),
        encoding="utf-8",
    )
    context.record.record_artifact(
        path="bom/bom.json",
        stage="bom",
        kind="bom",
        sha256=input_sha256(path),
    )


def _load_or_create_record(
    request: BundleRequest,
    identity: BundleIdentity,
    decision: ResumeDecision,
) -> BundleRecord:
    from legolization.bundle.identity import result_affecting_config  # noqa: PLC0415
    from legolization.configuration import mapping_hash  # noqa: PLC0415

    if decision.resume and (existing := read_record(decision.directory)) is not None:
        existing.status = "in-progress"
        return existing
    values = result_affecting_config(
        request.config,
        invocation=request.invocation_payload(),
    )
    return BundleRecord(
        identity=identity,
        source=source_payload(
            request.input_path,
            bundle_dir=decision.directory,
            sha256=identity.input_sha256,
        ),
        configuration={"sha256": mapping_hash(values), "values": values},
        versions=versions_payload(),
        quality=request.quality,
    )


def _finalize(context: _BundleContext) -> ResultEnvelope:
    buildable = bool(context.record.verdicts.get("buildable"))
    context.record.status = "complete" if buildable else "unbuildable"
    context.record.exit_code = COMPLETE if buildable else UNBUILDABLE
    write_record(context.record, context.directory)
    return _envelope(context, exit_code=context.record.exit_code)


def _mark_interrupted(context: _BundleContext) -> None:
    context.record.status = "interrupted"
    context.record.exit_code = INTERRUPTED
    write_record(context.record, context.directory)


def _envelope(context: _BundleContext, *, exit_code: int) -> ResultEnvelope:
    record = context.record
    artifacts = tuple(
        ArtifactRecord(
            path=str(context.directory / entry.path),
            kind=entry.kind,
            sha256=entry.sha256,
        )
        for entry in record.artifacts
    )
    return ResultEnvelope(
        command="bundle",
        exit_code=exit_code,
        artifacts=artifacts,
        warnings=tuple(record.warnings),
        data={
            "bundle_dir": str(context.directory),
            "status": record.status,
            "resume": context.resume,
            "regenerated_stages": list(context.reruns),
            "stages": {name: stage.status for name, stage in record.stages.items()},
            "verdicts": dict(record.verdicts),
        },
    )


def _cancel_pending(
    request: BundleRequest,
    identity: BundleIdentity,
    decision: ResumeDecision,
) -> ResultEnvelope:
    from legolization.bundle.workers import cancel_workers  # noqa: PLC0415

    if not decision.resume:
        msg = "no identity-matched bundle exists to cancel"
        raise ConfigurationError(msg)
    record = read_record(decision.directory)
    if record is None:
        msg = f"unreadable bundle record in {decision.directory}"
        raise ConfigurationError(msg)
    outcomes = cancel_workers(decision.directory, identity=identity)
    cancelled = {outcome.candidate_key: outcome.action for outcome in outcomes}
    for entry in record.pending:
        key = str(entry.get("candidate_key", ""))
        if key in cancelled:
            entry["status"] = cancelled[key]
    record.status = "cancelled"
    write_record(record, decision.directory)
    del request
    return ResultEnvelope(
        command="bundle",
        exit_code=COMPLETE,
        data={
            "bundle_dir": str(decision.directory),
            "cancelled": [
                {"candidate": key, "action": action}
                for key, action in sorted(cancelled.items())
            ],
        },
    )


def _flavor(request: BundleRequest) -> BundleFlavor:
    if request.input_path.suffix.lower() in LDRAW_SUFFIXES:
        return "optimized" if request.retile else "instructions"
    return "legolization"


def _mode(request: BundleRequest) -> BundleMode:
    if request.input_path.suffix.lower() not in LDRAW_SUFFIXES:
        return "generate"
    return "retile" if request.retile else "preserve"


def _validate_input(request: BundleRequest) -> None:
    from legolization.cli.common import require_file  # noqa: PLC0415

    require_file(request.input_path, label="input")
    suffix = request.input_path.suffix.lower()
    if suffix not in NATIVE_SUFFIXES | LDRAW_SUFFIXES:
        msg = (
            f"unsupported input {request.input_path.name}: expected one of "
            f"{', '.join(sorted(NATIVE_SUFFIXES | LDRAW_SUFFIXES))}"
        )
        raise ConfigurationError(msg)
    if request.retile and suffix not in LDRAW_SUFFIXES:
        msg = "--retile applies only to .ldr/.mpd inputs"
        raise ConfigurationError(msg)
