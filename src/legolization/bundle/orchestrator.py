"""The bundle stage machine: run, resume, regenerate, interrupt, cancel.

The orchestrator is the single writer of ``bundle.json`` (guarded by a
file lock). Every stage boundary persists atomically, so a SIGINT at
any point leaves a resumable record (exit 130). On resume, completed
stages whose artifacts still hash-match are skipped; a drifted artifact
reruns only its producing stage, relying on seed-deterministic
regeneration.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from legolization.bundle.identity import BundleIdentity, bundle_identity
from legolization.bundle.paths import ResumeDecision, resolve_output_dir
from legolization.bundle.record import (
    BundleRecord,
    StageRecord,
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
    PARTIAL,
    UNBUILDABLE,
)
from legolization.errors import ConfigurationError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from legolization.bundle.candidates import CandidatePlan, CandidateSpec
    from legolization.bundle.paths import BundleFlavor
    from legolization.bundle.selection import BundleCandidate, BundleSelection
    from legolization.configuration import ProjectConfig
    from legolization.grid import VoxelGrid
    from legolization.ldraw_in import ImportedLdrawModel
    from legolization.pipeline import PipelineResult

NATIVE_SUFFIXES = {".vox", ".npy", ".obj", ".stl", ".ply"}
LDRAW_SUFFIXES = {".ldr", ".mpd"}

_POLL_INTERVAL_S = 0.05


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
    clock: Callable[[], float] = field(default=time.monotonic)

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
    identity: BundleIdentity
    mode: BundleMode = "generate"
    grid: VoxelGrid | None = None
    result: PipelineResult | None = None
    imported: ImportedLdrawModel | None = None
    plan: CandidatePlan | None = None
    variant_grids: dict[str, VoxelGrid] = field(default_factory=dict)
    candidates: list[BundleCandidate] = field(default_factory=list)
    force_rerun: set[str] = field(default_factory=set)
    publish: bool = True
    exit_override: int | None = None
    reruns: list[str] = field(default_factory=list)

    @property
    def sweeping(self) -> bool:
        """Whether this invocation runs the candidate sweep stages."""
        return self.mode != "preserve" and self.request.quality != "direct"

    def ensure_plan(self) -> CandidatePlan:
        """Build the candidate plan and variant grids on first use."""
        if self.plan is None:
            from dataclasses import replace  # noqa: PLC0415

            from legolization.bundle.candidates import (  # noqa: PLC0415
                dedup_variants,
                plan_candidates,
            )
            from legolization.placement.global_exact import (  # noqa: PLC0415
                preflight_reason,
            )

            config = self.request.config
            if self.mode == "retile":
                canonical = self.ensure_grid()
                self.variant_grids = {
                    "hard": canonical,
                    "soft": canonical,
                    "soft-dither": canonical,
                }
            else:
                from legolization.pipeline import load_grid  # noqa: PLC0415

                base = config.to_pipeline(strategy="bond")
                plain = load_grid(
                    self.request.input_path,
                    replace(base, dither=False),
                )
                dithered = load_grid(
                    self.request.input_path,
                    replace(base, dither=True),
                )
                self.variant_grids = {
                    "hard": plain,
                    "soft": plain,
                    "soft-dither": dithered,
                }
            variants, collapsed = dedup_variants(self.variant_grids)
            quality = self.request.quality
            if quality == "direct":  # pragma: no cover - guarded by sweeping
                msg = "candidate plans need a quality tier"
                raise ConfigurationError(msg)
            self.plan = plan_candidates(
                quality=quality,
                variants=variants,
                collapsed=collapsed,
                exact_skip_reason=preflight_reason(
                    self.variant_grids["hard"],
                    max_cells=config.placement.exact.max_cells,
                ),
                duration_s=self.request.duration_s,
            )
        return self.plan

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
        sweep mode re-runs the recorded winner with full outputs;
        preserve mode analyzes the imported assembly as-is. Every mode
        finishes with a publication instruction plan: automatic step
        density with subassemblies and insertion auditing always on.
        """
        if self.result is None:
            if self.mode == "preserve":
                result = self._imported_result()
            elif self.sweeping:
                result = self._winner_result()
            else:
                result = self._generated_result()
            self.result = self._with_publication_plan(result)
        return self.result

    def _with_publication_plan(self, result: PipelineResult) -> PipelineResult:
        from dataclasses import replace  # noqa: PLC0415

        from legolization.instructions.sequencer import (  # noqa: PLC0415
            InstructionsConfig,
            automatic_target_step_size,
            plan_instructions,
        )

        plan = plan_instructions(
            result.layout,
            config=InstructionsConfig(
                target_step_size=automatic_target_step_size(len(result.layout)),
                subassemblies=True,
                insertion_check=True,
                solver=self.request.config.stability.effective_solver(),
            ),
        )
        return replace(result, plan=plan)

    def _generated_result(self) -> PipelineResult:
        from dataclasses import replace  # noqa: PLC0415

        from legolization.cli.build import select_strategy  # noqa: PLC0415
        from legolization.instructions.sequencer import (  # noqa: PLC0415
            InstructionsConfig,
        )
        from legolization.pipeline import run  # noqa: PLC0415

        grid = self.ensure_grid()
        config = self.request.config
        strategy, automatic = select_strategy(config, grid)
        pipeline = replace(
            config.to_pipeline(strategy=strategy, automatic=automatic),
            instructions=InstructionsConfig(mode="layer"),
        )
        return run(grid, pipeline)

    def _winner_result(self) -> PipelineResult:
        from dataclasses import replace  # noqa: PLC0415

        from legolization.bundle.candidates import VARIANTS  # noqa: PLC0415
        from legolization.pipeline import run  # noqa: PLC0415

        winner = self.record.verdicts.get("winner")
        if not isinstance(winner, dict) or "strategy" not in winner:
            msg = "no selected winner is recorded for this bundle"
            raise ConfigurationError(msg)
        self.ensure_plan()
        variant_name = str(winner.get("variant", "hard"))
        variant = next(
            (item for item in VARIANTS if item.name == variant_name),
            VARIANTS[0],
        )
        from legolization.instructions.sequencer import (  # noqa: PLC0415
            InstructionsConfig,
        )

        grid = self.variant_grids.get(variant_name) or self.variant_grids["hard"]
        pipeline = replace(
            self.request.config.to_pipeline(strategy=str(winner["strategy"])),
            seed=int(winner.get("seed") or 0),
            colour_mode=variant.colour_mode,
            dither=variant.dither,
            instructions=InstructionsConfig(mode="layer"),
        )
        return run(grid, pipeline)

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
        identity=identity,
        mode=_mode(request),
    )
    if context.resume and (
        context.record.pending or context.record.verdicts.get("provisional")
    ):
        context.force_rerun |= {"candidates", "selection"}
    middle: tuple[tuple[str, _StageRunner], ...] = (
        (("candidates", _stage_candidates), ("selection", _stage_selection))
        if context.sweeping
        else (("generate", _stage_generate),)
    )
    stages: tuple[tuple[str, _StageRunner], ...] = (
        ("ingest", _stage_ingest),
        *middle,
        ("model", _stage_model),
        ("bom", _stage_bom),
        ("instructions", _stage_instructions),
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
        and name not in context.force_rerun
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
    stage.detail["format"] = (
        "prepared"
        if context.request.input_path.is_dir()
        else context.request.input_path.suffix.lower().lstrip(".")
    )
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


def _stage_candidates(context: _BundleContext) -> None:
    # The spawn/harvest/deadline loop is one orchestration boundary.
    # lizard forgives(cyclomatic_complexity)
    from legolization.bundle.workers import (  # noqa: PLC0415
        is_alive,
        prepare_worker,
        scan_pending,
        spawn_worker,
    )
    from legolization.runtime import logical_cpu_count  # noqa: PLC0415

    stage = context.record.stage("candidates")
    plan = context.ensure_plan()
    stage.detail.update(plan.to_detail())
    clock = context.request.clock
    deadline = clock() + plan.time_budget_s
    completed = _harvested_payloads(context)
    spawned: set[str] = set()
    while True:
        live = {
            worker.candidate_key
            for worker in scan_pending(context.directory, identity=context.identity)
            if worker.candidate_key not in completed and is_alive(worker)
        }
        waiting = [
            spec
            for spec in plan.specs
            if spec.key() not in completed
            and spec.key() not in live
            and spec.key() not in spawned
        ]
        capacity = max(logical_cpu_count() - len(live), 0)
        for spec in waiting[:capacity]:
            directory = prepare_worker(
                context.directory,
                identity=context.identity,
                candidate_key=spec.key(),
                job=_candidate_job(context, spec),
            )
            spawn_worker(directory)
            spawned.add(spec.key())
        completed = _harvested_payloads(context)
        unresolved = [spec for spec in plan.specs if spec.key() not in completed]
        if not unresolved or clock() >= deadline:
            break
        time.sleep(_POLL_INTERVAL_S)
    still_running = [
        worker
        for worker in scan_pending(context.directory, identity=context.identity)
        if worker.candidate_key not in completed and is_alive(worker)
    ]
    context.record.pending = [
        {"candidate_key": worker.candidate_key, "pid": worker.pid, "status": "running"}
        for worker in still_running
    ]
    stage.detail.update(
        {
            "completed": len(completed),
            "pending": len(still_running),
        }
    )
    if still_running:
        stage.status = "partial"
    context.candidates = _bundle_candidates(context)


def _harvested_payloads(context: _BundleContext) -> dict[str, dict[str, object]]:
    from legolization.bundle.workers import harvest_results  # noqa: PLC0415

    payloads: dict[str, dict[str, object]] = {}
    for worker, payload in harvest_results(
        context.directory,
        identity=context.identity,
    ):
        enriched = dict(payload)
        enriched["model_relpath"] = str(
            (worker.directory / "model.ldr").relative_to(context.directory)
        )
        payloads[worker.candidate_key] = enriched
    return payloads


def _bundle_candidates(context: _BundleContext) -> list[BundleCandidate]:
    from legolization.bundle.selection import BundleCandidate  # noqa: PLC0415

    return [
        BundleCandidate.from_payload(payload)
        for payload in _harvested_payloads(context).values()
    ]


def _candidate_job(
    context: _BundleContext,
    spec: CandidateSpec,
) -> dict[str, object]:
    return {
        "kind": "candidate",
        "input_path": str(context.request.input_path.resolve()),
        "config": context.request.config.to_dict(),
        "strategy": spec.strategy,
        "seed": spec.seed,
        "variant": spec.variant.to_dict(),
        "retile": context.mode == "retile",
    }


def _stage_selection(context: _BundleContext) -> None:
    from legolization.bundle.selection import select_bundle_winner  # noqa: PLC0415
    from legolization.eval_artifacts import atomic_json, input_sha256  # noqa: PLC0415

    stage = context.record.stage("selection")
    selection = select_bundle_winner(context.candidates)
    comparison_dir = context.directory / "comparison"
    comparison_dir.mkdir(parents=True, exist_ok=True)
    report_path = comparison_dir / "report.json"
    atomic_json(report_path, selection.to_report())
    context.record.record_artifact(
        path="comparison/report.json",
        stage="selection",
        kind="report",
        sha256=input_sha256(report_path),
    )
    pending = bool(context.record.pending)
    winner = selection.winner
    buildable = bool(
        winner is not None and winner.metrics is not None and winner.metrics.buildable
    )
    old_winner = context.record.verdicts.get("winner")
    new_winner = (
        {
            "strategy": winner.strategy,
            "seed": winner.seed,
            "variant": winner.variant,
        }
        if winner is not None and buildable
        else None
    )
    context.record.verdicts.update(
        {
            "buildable": buildable,
            "stable": buildable,
            "provisional": buildable and pending,
            "winner": new_winner,
        }
    )
    stage.detail.update({"reason": selection.reason, "pending": pending})
    if new_winner is None:
        context.publish = False
        if pending:
            context.exit_override = PARTIAL
        elif winner is not None:
            _retain_best_rejected(context, selection)
        return
    if buildable and pending:
        context.exit_override = PARTIAL
    if old_winner != new_winner:
        context.force_rerun |= {"model", "bom"}


def _retain_best_rejected(
    context: _BundleContext,
    selection: BundleSelection,
) -> None:
    """Keep the least-bad rejected model in diagnostics with reasons."""
    import shutil  # noqa: PLC0415

    from legolization.eval_artifacts import atomic_json, input_sha256  # noqa: PLC0415

    winner = selection.winner
    if winner is None or winner.model_relpath is None:
        return
    source = context.directory / winner.model_relpath
    if not source.is_file():
        return
    diagnostics = context.directory / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    model_path = diagnostics / "best-rejected.ldr"
    shutil.copyfile(source, model_path)
    atomic_json(
        diagnostics / "best-rejected.json",
        {
            "reason": selection.reason,
            "unbuildable": True,
            "candidate": winner.to_dict(),
        },
    )
    context.record.record_artifact(
        path="diagnostics/best-rejected.ldr",
        stage="selection",
        kind="rejected-model",
        sha256=input_sha256(model_path),
    )
    context.record.record_artifact(
        path="diagnostics/best-rejected.json",
        stage="selection",
        kind="rejected-report",
        sha256=input_sha256(diagnostics / "best-rejected.json"),
    )


def _stage_model(context: _BundleContext) -> None:
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415
    from legolization.ldraw_out import write_model  # noqa: PLC0415

    if not context.publish:
        stage = context.record.stage("model")
        stage.status = "skipped"
        stage.detail["reason"] = "no buildable candidate to publish"
        return
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

    if not context.publish:
        stage = context.record.stage("bom")
        stage.status = "skipped"
        stage.detail["reason"] = "no buildable candidate to publish"
        return
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


def _stage_instructions(context: _BundleContext) -> None:
    """Publish the instruction surfaces per the render mode.

    The step-annotated MPD (written by the model stage) and the audit
    report are emitted in every mode; HTML/PDF booklets appear only
    when rendering is available, with explicit missing-step markers —
    never placeholder pages — when some steps fail.
    """
    from legolization.eval_artifacts import atomic_json, input_sha256  # noqa: PLC0415
    from legolization.instructions.audit import audit_model  # noqa: PLC0415

    stage = context.record.stage("instructions")
    render_mode = context.request.render
    stage.detail["render"] = render_mode
    if not context.publish:
        stage.status = "skipped"
        stage.detail["reason"] = "no buildable candidate to publish"
        return
    instructions_dir = context.directory / "instructions"
    instructions_dir.mkdir(parents=True, exist_ok=True)
    audit_payload = audit_model(context.directory / "model" / "model.mpd")
    audit_path = instructions_dir / "audit.json"
    atomic_json(audit_path, audit_payload)
    context.record.record_artifact(
        path="instructions/audit.json",
        stage="instructions",
        kind="audit",
        sha256=input_sha256(audit_path),
    )
    stage.detail["audit_verdict"] = audit_payload["verdict"]
    if audit_payload["verdict"] != "certified" and context.record.verdicts.get(
        "buildable"
    ):
        # A buildable model with an incomplete stable/insertion-safe
        # order is a warned partial (exit 3); an unbuildable result
        # keeps its own exit 2.
        context.exit_override = PARTIAL
        stage.status = "partial"
    if render_mode == "off":
        stage.detail["booklet"] = "omitted (--render=off)"
        return
    _publish_booklet(context, stage, instructions_dir)


def _publish_booklet(
    context: _BundleContext,
    stage: StageRecord,
    instructions_dir: Path,
) -> None:
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415
    from legolization.instructions.booklet import (  # noqa: PLC0415
        BookletConfig,
        ModelStats,
        booklet_html,
        build_booklet,
        default_page_size,
        write_booklet_pdf,
    )
    from legolization.instructions.render import (  # noqa: PLC0415
        detect_renderer,
        render_step_images,
    )

    renderer = detect_renderer()
    if renderer is None:
        stage.detail["booklet"] = "omitted (no renderer available)"
        if context.request.render == "required":
            context.exit_override = PARTIAL
            stage.status = "partial"
        return
    result = context.ensure_result()
    plan = result.plan
    if plan is None:  # pragma: no cover - publication plans always exist
        stage.detail["booklet"] = "omitted (no instruction plan)"
        return
    images = render_step_images(context.directory / "model" / "model.mpd", plan)
    if not any(png is not None for png in images.images):
        stage.detail["booklet"] = "omitted (rendering failed for every step)"
        for warning in images.warnings:
            if warning not in context.record.warnings:
                context.record.warnings.append(warning)
        context.exit_override = PARTIAL
        stage.status = "partial"
        return
    booklet = build_booklet(
        plan,
        ModelStats(
            name=context.request.input_path.stem,
            brick_count=result.brick_count,
            mass_g=result.mass_g,
            step_count=result.step_count,
            stable=result.stability.stable,
            buildable=result.buildable,
            component_count=result.component_count,
            floating_count=result.floating_count,
        ),
        images,
        config=BookletConfig(
            page_size=default_page_size(),
            title=context.request.input_path.stem,
        ),
    )
    html_path = instructions_dir / "instructions.html"
    html_path.write_text(booklet_html(booklet), encoding="utf-8")
    pdf_path = instructions_dir / "instructions.pdf"
    write_booklet_pdf(booklet, pdf_path)
    for name, kind in (
        ("instructions.html", "booklet"),
        ("instructions.pdf", "booklet"),
    ):
        context.record.record_artifact(
            path=f"instructions/{name}",
            stage="instructions",
            kind=kind,
            sha256=input_sha256(instructions_dir / name),
        )
    if booklet.missing_steps:
        listed = ", ".join(str(index) for index in booklet.missing_steps)
        message = f"booklet has explicit missing-step markers for step(s) {listed}"
        if message not in context.record.warnings:
            context.record.warnings.append(message)
        context.exit_override = PARTIAL
        stage.status = "partial"
    stage.detail["missing_steps"] = list(booklet.missing_steps)


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
    if context.exit_override is not None:
        context.record.status = "partial"
        context.record.exit_code = context.exit_override
    elif buildable:
        context.record.status = "complete"
        context.record.exit_code = COMPLETE
    else:
        context.record.status = "unbuildable"
        context.record.exit_code = UNBUILDABLE
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
    from legolization.inspection import resolve_prepared_input  # noqa: PLC0415

    if resolve_prepared_input(request.input_path) is not None:
        if request.retile:
            msg = "--retile applies only to .ldr/.mpd inputs"
            raise ConfigurationError(msg)
        return
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
