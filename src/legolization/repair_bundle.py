"""Effort-tiered repair engine writing a portable ``-repair`` bundle.

The engine is deliberately search-agnostic: the counterfactual and
redesign searches arrive as injectable callables so policy (budgets,
escalation, artifact layout, exit codes) stays fast to test. Repair
never touches the source input; every output lands inside the bundle
directory, which is rejected outright when it would contain the input.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from legolization.bundle.paths import default_bundle_dir, numbered_sibling
from legolization.bundle.record import (
    BundleRecord,
    StageRecord,
    read_record,
    source_payload,
    versions_payload,
    write_record,
)
from legolization.cli.exit_codes import (
    COMPLETE,
    OPERATIONAL_ERROR,
    PARTIAL,
    UNBUILDABLE,
)
from legolization.errors import ConfigurationError, ManifestError

if TYPE_CHECKING:
    from collections.abc import Callable

    from legolization.assembly_counterfactual import CounterfactualSearchResult
    from legolization.bundle.record import BundleStatus, StageStatus
    from legolization.configuration import ProjectConfig
    from legolization.ldraw_in import LdrawSourceRef
    from legolization.redesign import RejectedCandidate, RepairSearchResult

REPAIR_SCHEMA = "legolization.repair/v1"

type RepairEffort = Literal["fast", "balanced", "exhaustive"]
type RepairDefect = Literal["none", "topological", "physical"]
type CounterfactualSearch = Callable[[float], CounterfactualSearchResult]
type RedesignSearch = Callable[[float], RepairSearchResult]
type AfterAnalysis = Callable[[Path], dict[str, Any]]

EFFORT_BUDGETS: dict[str, float] = {"fast": 60.0, "balanced": 300.0}
_COUNTERFACTUAL_SLICE_CAP_S = 60.0
_COUNTERFACTUAL_SLICE_FRACTION = 0.25

_BUNDLE_STATUS_BY_EXIT: dict[int, BundleStatus] = {
    COMPLETE: "complete",
    PARTIAL: "partial",
    UNBUILDABLE: "unbuildable",
}
_STAGE_STATUS_BY_EXIT: dict[int, StageStatus] = {
    COMPLETE: "complete",
    PARTIAL: "partial",
}


def effort_budget(effort: RepairEffort, time_budget_s: float | None) -> float:
    """Resolve the hard repair budget for one effort tier.

    An explicit ``--time-budget`` always wins; ``exhaustive`` refuses to
    run without one.
    """
    if time_budget_s is not None:
        return time_budget_s
    if effort == "exhaustive":
        msg = "--effort exhaustive requires an explicit --time-budget"
        raise ConfigurationError(msg)
    return EFFORT_BUDGETS[effort]


def counterfactual_slice_s(time_budget_s: float) -> float:
    """Return the counterfactual-first slice of one repair budget."""
    return min(
        _COUNTERFACTUAL_SLICE_CAP_S,
        _COUNTERFACTUAL_SLICE_FRACTION * time_budget_s,
    )


def ensure_never_overwrites(input_path: Path, directory: Path) -> None:
    """Reject a repair destination that is or contains the input."""
    resolved_input = input_path.resolve()
    resolved_dir = directory.resolve()
    if resolved_dir == resolved_input or resolved_dir in resolved_input.parents:
        msg = f"--repair-output must not contain the analyze input: {directory}"
        raise ConfigurationError(msg)


def resolve_repair_dir(
    input_path: Path,
    explicit: Path | None,
    *,
    input_sha: str,
) -> Path:
    """Pick the ``-repair`` sibling bundle directory for one input.

    The unnumbered sibling is reused only when it already holds a
    ``bundle.json`` for the same input bytes; anything else gets the
    first free numbered sibling. An explicit directory is honored as-is
    (after the never-overwrite guard).
    """
    if explicit is not None:
        ensure_never_overwrites(input_path, explicit)
        return explicit
    base = default_bundle_dir(input_path, "repair")
    if not base.exists():
        return base
    record = read_record(base)
    if record is not None and record.identity.input_sha256 == input_sha:
        return base
    return numbered_sibling(base)


def repair_bundle_record(
    input_path: Path,
    bundle_dir: Path,
    *,
    input_sha: str,
    project: ProjectConfig,
    catalog_sha256: str,
) -> BundleRecord:
    """Build the initial ``bundle.json`` record for a direct bundle."""
    from legolization.bundle.identity import (  # noqa: PLC0415 - lazy heavy import
        BundleIdentity,
        result_affecting_config,
    )
    from legolization.configuration import mapping_hash  # noqa: PLC0415
    from legolization.version import package_version  # noqa: PLC0415

    values = project.to_dict()
    identity = BundleIdentity(
        input_sha256=input_sha,
        config_sha256=mapping_hash(result_affecting_config(project)),
        legolization_version=package_version(),
        catalog_sha256=catalog_sha256,
    )
    return BundleRecord(
        identity=identity,
        source=source_payload(input_path, bundle_dir=bundle_dir, sha256=input_sha),
        configuration={"sha256": mapping_hash(values), "values": values},
        versions=versions_payload(),
        quality="direct",
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RepairPlan:
    """Resolved effort tier and hard budget for one repair run."""

    effort: RepairEffort
    time_budget_s: float

    @property
    def counterfactual_slice_s(self) -> float:
        """Budget slice granted to the counterfactual-first phase."""
        return counterfactual_slice_s(self.time_budget_s)


@dataclass(frozen=True, slots=True, kw_only=True)
class RepairRequest:
    """Everything one repair engine run needs, searches included."""

    input_path: Path
    plan: RepairPlan
    bundle_dir: Path
    record: BundleRecord
    defect: RepairDefect
    before_payload: dict[str, Any]
    counterfactual_search: CounterfactualSearch
    redesign_search: RedesignSearch
    after_analysis: AfterAnalysis | None = None
    catalog_section: dict[str, Any] | None = None
    source_refs: dict[int, LdrawSourceRef] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, kw_only=True)
class RepairRunResult:
    """Machine-readable outcome of one repair engine run."""

    exit_code: int
    status: str
    explanation: str
    artifacts: dict[str, str]
    warnings: tuple[str, ...]
    payload: dict[str, Any]


@dataclass(slots=True, kw_only=True)
class _EngineState:
    """Mutable accounting shared by the engine phases."""

    started: float
    artifacts: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "error"
    explanation: str = ""
    exit_code: int = OPERATIONAL_ERROR


def run_repair(request: RepairRequest) -> RepairRunResult:
    """Run the counterfactual-first, redesign-escalating repair policy."""
    ensure_never_overwrites(request.input_path, request.bundle_dir)
    state = _EngineState(started=time.monotonic())
    state.payload = {
        "schema": REPAIR_SCHEMA,
        "tier": request.plan.effort,
        "timings": {
            "time_budget_s": request.plan.time_budget_s,
            "counterfactual_slice_s": request.plan.counterfactual_slice_s,
        },
    }
    if request.catalog_section:
        state.payload["catalog"] = dict(request.catalog_section)
    _write_json(
        request.bundle_dir / "analysis" / "before.json",
        request.before_payload,
        state=state,
        key="repair_before",
    )
    try:
        _run_phases(request, state)
    except OSError as error:
        state.status = "error"
        state.explanation = f"repair artifact write failed: {error}"
        state.exit_code = OPERATIONAL_ERROR
    state.payload["status"] = state.status
    state.payload["explanation"] = state.explanation
    state.payload["timings"]["total_elapsed_s"] = round(
        time.monotonic() - state.started,
        6,
    )
    _write_json(
        request.bundle_dir / "repair.json",
        state.payload,
        state=state,
        key="repair_report",
    )
    _write_bundle_record(request, state)
    return RepairRunResult(
        exit_code=state.exit_code,
        status=state.status,
        explanation=state.explanation,
        artifacts=dict(state.artifacts),
        warnings=tuple(state.warnings),
        payload=dict(state.payload),
    )


def _run_phases(request: RepairRequest, state: _EngineState) -> None:
    """Execute the not-needed, counterfactual, and redesign phases."""
    if request.defect == "none":
        state.status = "not_needed"
        state.explanation = "the analysis found no definite defect to repair"
        state.exit_code = COMPLETE
        return
    counterfactual = request.counterfactual_search(request.plan.counterfactual_slice_s)
    state.payload["counterfactual"] = _counterfactual_summary(counterfactual)
    state.payload["timings"]["counterfactual_elapsed_s"] = counterfactual.elapsed_s
    candidate = counterfactual.candidate
    if (
        request.defect == "topological"
        and candidate is not None
        and candidate.after_component_count <= 1
    ):
        _accept_counterfactual(request, state, model_text=candidate.model_text)
        state.explanation = (
            f"a BOM-preserving counterfactual edit repairs the model "
            f"({candidate.description})"
        )
        return
    remaining = max(
        request.plan.time_budget_s - (time.monotonic() - state.started),
        0.001,
    )
    redesign = request.redesign_search(remaining)
    state.payload["redesign"] = redesign.to_report(request.source_refs)
    state.payload["timings"]["redesign_elapsed_s"] = redesign.elapsed_seconds
    _conclude_redesign(request, state, redesign)


def _accept_counterfactual(
    request: RepairRequest,
    state: _EngineState,
    *,
    model_text: str,
) -> None:
    model_path = _repaired_model_path(request)
    model_path.parent.mkdir(parents=True, exist_ok=True)
    model_path.write_text(model_text, encoding="utf-8")
    state.artifacts["repaired_model"] = str(model_path)
    state.status = "repaired"
    state.exit_code = COMPLETE
    state.payload["strategy"] = "counterfactual"
    state.payload["metrics"] = dict(
        state.payload["counterfactual"].get("candidate") or {}
    )
    _write_after_analysis(request, state, model_path)


def _conclude_redesign(
    request: RepairRequest,
    state: _EngineState,
    redesign: RepairSearchResult,
) -> None:
    # The terminal redesign outcomes map one-to-one onto exit codes.
    if redesign.candidate is not None and redesign.layout is not None:
        from legolization.redesign import write_repair_model  # noqa: PLC0415

        model_path = _repaired_model_path(request)
        model_path.parent.mkdir(parents=True, exist_ok=True)
        write_repair_model(redesign.layout, model_path)
        state.artifacts["repaired_model"] = str(model_path)
        state.status = "repaired"
        state.exit_code = COMPLETE
        state.payload["strategy"] = "redesign"
        state.payload["metrics"] = {
            "strict_capacity_n": redesign.candidate.strict_capacity_n,
            "parity_max_score": redesign.candidate.parity_max_score,
            "strict_max_score": redesign.candidate.strict_max_score,
            "added_cells": redesign.candidate.added_cells,
            "visible_added_cells": redesign.candidate.visible_added_cells,
        }
        state.explanation = (
            f"redesign found a validated {redesign.candidate.tier} candidate"
        )
        _write_after_analysis(request, state, model_path)
        return
    _retain_best_rejected(request, state, redesign.best_rejected)
    if redesign.error is not None and not redesign.timed_out:
        state.status = "error"
        state.exit_code = OPERATIONAL_ERROR
        state.explanation = f"the redesign search failed: {redesign.error}"
        return
    if redesign.timed_out:
        state.status = "timed_out"
        state.exit_code = PARTIAL
        retained = (
            "; the best rejected candidate is retained under diagnostics/"
            if redesign.best_rejected is not None
            else ""
        )
        state.explanation = (
            f"the {request.plan.time_budget_s:g}s budget expired before a "
            f"validated repair{retained}"
        )
        return
    state.status = "exhausted"
    state.exit_code = UNBUILDABLE
    state.explanation = (
        "the bounded search exhausted every repair tier without a candidate "
        "passing the physics gates"
    )


def _retain_best_rejected(
    request: RepairRequest,
    state: _EngineState,
    rejected: RejectedCandidate | None,
) -> None:
    if rejected is None:
        return
    from legolization.redesign import write_repair_model  # noqa: PLC0415

    diagnostics = request.bundle_dir / "diagnostics"
    model_path = diagnostics / "best-rejected.mpd"
    try:
        diagnostics.mkdir(parents=True, exist_ok=True)
        write_repair_model(rejected.layout, model_path)
    except OSError as error:
        state.warnings.append(f"best-rejected model write failed: {error}")
    else:
        state.artifacts["best_rejected_model"] = str(model_path)
    _write_json(
        diagnostics / "best-rejected.json",
        rejected.to_payload(),
        state=state,
        key="best_rejected_report",
    )
    state.payload["best_rejected"] = rejected.to_payload()


def _write_after_analysis(
    request: RepairRequest,
    state: _EngineState,
    model_path: Path,
) -> None:
    if request.after_analysis is None:
        return
    try:
        after = request.after_analysis(model_path)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        state.warnings.append(f"after-repair analysis failed: {error}")
        return
    verdict = str(after.get("verdict", ""))
    validated = verdict in {"feasible", "connected"}
    state.payload["verification"] = "physics-validated" if validated else "unverified"
    state.payload["verification_reason"] = (
        f"re-analysis of the repaired model returned {verdict!r}"
        if verdict
        else "re-analysis of the repaired model returned no verdict"
    )
    _write_json(
        request.bundle_dir / "analysis" / "after.json",
        after,
        state=state,
        key="repair_after",
    )


def _repaired_model_path(request: RepairRequest) -> Path:
    return request.bundle_dir / "model" / f"{request.input_path.stem}.repaired.mpd"


def _counterfactual_summary(result: CounterfactualSearchResult) -> dict[str, Any]:
    candidate = result.candidate
    return {
        "status": result.status,
        "tested_candidates": result.tested_candidates,
        "timed_out": result.timed_out,
        "elapsed_s": result.elapsed_s,
        "message": result.message,
        "candidate": (
            {
                "occurrence_id": candidate.occurrence_id,
                "source_model": candidate.source_model,
                "source_line": candidate.source_line,
                "part_id": candidate.part_id,
                "kind": candidate.kind,
                "description": candidate.description,
                "after_component_count": candidate.after_component_count,
                "confirmed_connection_gain": candidate.confirmed_connection_gain,
                "component_reduction": candidate.component_reduction,
                "confidence": candidate.confidence,
            }
            if candidate is not None
            else None
        ),
    }


def _write_json(
    path: Path,
    payload: dict[str, Any],
    *,
    state: _EngineState,
    key: str,
) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, TypeError, ValueError) as error:
        state.warnings.append(f"{key} write failed: {error}")
    else:
        state.artifacts[key] = str(path)


def _write_bundle_record(request: RepairRequest, state: _EngineState) -> None:
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415

    record = request.record
    record.status = _BUNDLE_STATUS_BY_EXIT.get(state.exit_code, "error")
    record.exit_code = state.exit_code
    stage = StageRecord(
        status=_STAGE_STATUS_BY_EXIT.get(state.exit_code, "failed"),
        warnings=list(state.warnings),
        detail={"status": state.status, "explanation": state.explanation},
    )
    if request.catalog_section:
        stage.detail["catalog"] = dict(request.catalog_section)
    record.stages["repair"] = stage
    record.verdicts["repair"] = state.status
    for kind, raw_path in sorted(state.artifacts.items()):
        artifact = Path(raw_path)
        try:
            relative = artifact.relative_to(request.bundle_dir)
            sha256 = input_sha256(artifact)
        except (OSError, ValueError):
            continue
        record.record_artifact(
            path=relative.as_posix(),
            stage="repair",
            kind=kind,
            sha256=sha256,
        )
    try:
        write_record(record, request.bundle_dir)
    except (ManifestError, OSError) as error:
        state.warnings.append(f"repair bundle record write failed: {error}")
    else:
        state.artifacts["repair_bundle"] = str(request.bundle_dir / "bundle.json")
