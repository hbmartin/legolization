"""JSON-safe projections of pyldraw3's report-oriented analysis APIs."""

from __future__ import annotations

from dataclasses import asdict
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from ldraw import (
    CatalogPreparationResult,
    Diagnostic,
    DiagnosticCode,
    InstructionDocument,
    InstructionIssue,
    InstructionSection,
    InstructionStep,
    LDrawCapability,
    ModelAnalysis,
    ModelInspection,
    Severity,
    iter_instruction_issues,
    prepare_catalog,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ldraw import BoundingBox, LDrawState, OccurrenceAttribution, Vector
    from ldraw.parts import Parts

    from legolization.ldraw_in import ImportedLdrawModel

_CONTACT_OCCURRENCE_LIMIT = 1_000


@lru_cache(maxsize=1)
def prepare_analysis_catalog() -> CatalogPreparationResult:
    """Prepare the two pyldraw capabilities legolization actually imports."""
    return prepare_catalog(
        capabilities=(
            LDrawCapability.CATALOG,
            LDrawCapability.GENERATED_MODULES,
        )
    )


def catalog_error(result: CatalogPreparationResult) -> str | None:
    """Return the actionable fatal error when preparation is unusable."""
    errors = tuple(
        diagnostic
        for diagnostic in result.diagnostics
        if diagnostic.severity is Severity.ERROR
    )
    if result.parts is not None and all(
        diagnostic.code is DiagnosticCode.CATALOG_PERSIST_FAILED
        for diagnostic in errors
    ):
        return None
    detail = next(
        (diagnostic.message for diagnostic in errors),
        "configured LDraw parts catalog is unavailable",
    )
    return f"{detail}; run `ldraw download --yes` and try again"


def catalog_degraded(result: CatalogPreparationResult) -> bool:
    """Whether usable parts could not be persisted for the next process."""
    return result.parts is not None and any(
        diagnostic.code is DiagnosticCode.CATALOG_PERSIST_FAILED
        for diagnostic in result.diagnostics
    )


def catalog_payload(result: CatalogPreparationResult) -> dict[str, Any]:
    """Serialize catalog readiness, work performed, and diagnostics."""
    fingerprint = result.report.fingerprint
    return {
        "initial_state": _state_payload(result.initial_state),
        "final_state": _state_payload(result.final_state),
        "report": {
            "library_root": str(result.report.library_root),
            "index_path": str(result.report.index_path),
            "entry_count": result.report.entry_count,
            "outcome": str(result.report.outcome),
            "persisted": result.report.persisted,
            "fingerprint": (
                {
                    "parts_lst_md5": fingerprint.parts_lst_md5,
                    "tree_fingerprint": fingerprint.tree_fingerprint,
                    "generation_fingerprint": fingerprint.generation_fingerprint,
                }
                if fingerprint is not None
                else None
            ),
        },
        "complete": result.complete,
        "degraded": catalog_degraded(result),
        "parts_available": result.parts is not None,
        "diagnostics": [
            diagnostic_payload(diagnostic) for diagnostic in result.diagnostics
        ],
    }


def _state_payload(state: LDrawState) -> dict[str, Any]:
    paths = state.paths
    return {
        "ready": state.ready,
        "reasons": [str(reason) for reason in state.reasons],
        "capabilities": sorted(str(capability) for capability in state.capabilities),
        "paths": {
            "library": str(paths.library_path),
            "ldraw": str(paths.ldraw_path),
            "parts_list": str(paths.parts_lst),
            "generated": str(paths.generated_path),
            "generated_library": str(paths.generated_library),
            "generation_hash": str(paths.generation_hash),
            "catalog_index": str(paths.catalog_db),
        },
    }


def diagnostic_payload(diagnostic: Diagnostic) -> dict[str, Any]:
    """Use pyldraw's stable JSON-safe diagnostic representation."""
    return diagnostic.to_dict()


def build_ldraw_payload(
    imported: ImportedLdrawModel,
    catalog_result: CatalogPreparationResult,
    *,
    section_physics: Mapping[str, tuple[dict[str, Any], ...]],
    issues: tuple[InstructionIssue, ...],
) -> dict[str, Any]:
    """Build the schema-v2 pyldraw evidence block from reused analyses."""
    model_analysis = imported.model_analysis
    document = imported.instruction_document
    if model_analysis is None or document is None:
        return {
            "catalog": catalog_payload(catalog_result),
            "load": {
                "complete": imported.load_complete,
                "diagnostics": [
                    diagnostic_payload(item) for item in imported.diagnostics
                ],
            },
        }
    return {
        "catalog": catalog_payload(catalog_result),
        "load": {
            "complete": imported.load_complete,
            "diagnostics": [diagnostic_payload(item) for item in imported.diagnostics],
        },
        "summary": _summary_payload(model_analysis),
        "bom": [row.to_dict() for row in model_analysis.bom],
        "geometry": _geometry_payload(model_analysis.inspection),
        "instructions": _instruction_payload(
            document,
            section_physics=section_physics,
            issues=issues,
        ),
    }


def _summary_payload(analysis: ModelAnalysis) -> dict[str, Any]:
    summary = analysis.summary
    return {
        "occurrence_count": summary.occurrence_count,
        "part_counts": dict(sorted(summary.part_counts.items())),
        "colour_usage": [
            {"colour": colour, "count": count}
            for colour, count in sorted(
                summary.colour_usage.items(),
                key=lambda item: (type(item[0]).__name__, str(item[0])),
            )
        ],
        "bounds_ldu": _bounds_payload(summary.bounds),
        "origin_bounds_ldu": _bounds_payload(summary.origin_bounds),
        "size_ldu": _vector_payload(summary.size_ldu),
        "size_mm": _vector_payload(summary.size_mm),
        "skipped_geometry": [asdict(item) for item in summary.skipped_geometry],
    }


def _geometry_payload(inspection: ModelInspection | None) -> dict[str, Any] | None:
    if inspection is None:
        return None
    occurrences = [
        {
            "occurrence": item.index + 1,
            "part": item.occurrence.part_code,
            "colour": item.occurrence.colour.code,
            "bounds_ldu": _bounds_payload(item.bounds),
            "stud_count": len(item.studs),
            "provenance": _attribution_payload(item.attribution),
        }
        for item in inspection.occurrences
    ]
    skipped = [
        {
            "occurrence": item.attribution.index + 1,
            "part": item.attribution.occurrence.part_code,
            "provenance": _attribution_payload(item.attribution),
            "diagnostic": diagnostic_payload(item.diagnostic),
        }
        for item in inspection.skipped_geometry
    ]
    return {
        "complete": inspection.complete,
        "occurrence_count": inspection.occurrence_count,
        "resolved_count": len(occurrences),
        "bounds_ldu": _bounds_payload(inspection.bounds),
        "occurrences": occurrences,
        "skipped": skipped,
        "diagnostics": [
            diagnostic_payload(diagnostic) for diagnostic in inspection.diagnostics
        ],
        "contacts": _contacts_payload(inspection),
    }


def _contacts_payload(inspection: ModelInspection) -> dict[str, Any]:
    if inspection.occurrence_count > _CONTACT_OCCURRENCE_LIMIT:
        return {
            "status": "skipped",
            "occurrence_limit": _CONTACT_OCCURRENCE_LIMIT,
            "reason": (
                f"{inspection.occurrence_count:_} occurrences exceeds the "
                f"{_CONTACT_OCCURRENCE_LIMIT:_}-occurrence safety limit"
            ),
            "stud_contacts": [],
            "aabb_gaps": [],
        }
    stud_contacts = inspection.stud_contacts()
    gaps = inspection.contact_gaps()
    return {
        "status": "complete",
        "occurrence_limit": _CONTACT_OCCURRENCE_LIMIT,
        "stud_contacts": [
            {
                "stud_occurrence": contact.stud_occurrence.index + 1,
                "supported_occurrence": contact.supported_occurrence.index + 1,
                "stud": contact.stud.name,
                "description": contact.stud.description,
                "position_ldu": _vector_payload(contact.position),
            }
            for contact in stud_contacts
        ],
        "aabb_gaps": [
            {
                "subject_occurrence": contact.subject.index + 1,
                "nearest_occurrence": contact.nearest.index + 1,
                "axes_ldu": _vector_payload(contact.gap.axes),
                "distance_ldu": contact.gap.distance,
            }
            for contact in gaps
        ],
    }


def _attribution_payload(attribution: OccurrenceAttribution) -> dict[str, Any]:
    return {
        "model_path": list(attribution.model_path),
        "reference_path": list(attribution.reference_path),
        "source_line_path": list(attribution.source_line_path),
        "local_step_path": list(attribution.local_step_path),
        "effective_step_path": list(attribution.effective_step_path),
        "page_path": list(attribution.page_path),
    }


def _instruction_payload(
    document: InstructionDocument,
    *,
    section_physics: Mapping[str, tuple[dict[str, Any], ...]],
    issues: tuple[InstructionIssue, ...],
) -> dict[str, Any]:
    error_sections = {
        issue.section for issue in issues if issue.severity is Severity.ERROR
    }
    return {
        "root": document.root.name,
        "sections": [
            _section_payload(
                section,
                physics=section_physics.get(section.name, ()),
                reliable=section.name not in error_sections,
                analysis_status=(
                    "complete" if section.name in section_physics else "skipped"
                ),
                analysis_reason=(
                    None
                    if section.name in section_physics
                    else "section has instruction validation errors"
                    if section.name in error_sections
                    else "section-prefix physics was not produced"
                ),
                parts=document.parts,
            )
            for section in document.sections
        ],
        "orphan_sections": [
            _section_payload(
                section,
                physics=(),
                reliable=section.name not in error_sections,
                analysis_status="not_applicable",
                analysis_reason="orphan sections are not reachable build prefixes",
                parts=document.parts,
            )
            for section in document.orphan_sections
        ],
        "issues": [_instruction_issue_payload(issue) for issue in issues],
    }


def instruction_issues(document: InstructionDocument) -> tuple[InstructionIssue, ...]:
    """Materialize instruction issues once for prefix-analysis policy."""
    return tuple(iter_instruction_issues(document))


def _section_payload(  # noqa: PLR0913 - report fields stay explicit
    section: InstructionSection,
    *,
    physics: tuple[dict[str, Any], ...],
    reliable: bool,
    analysis_status: str,
    analysis_reason: str | None,
    parts: Parts | None,
) -> dict[str, Any]:
    physics_by_step = {int(row["step"]): row for row in physics}
    return {
        "name": section.name,
        "is_root": section.is_root,
        "reachable": section.reachable,
        "references": list(section.references),
        "analysis_status": analysis_status,
        "analysis_reason": analysis_reason,
        "steps": [
            _step_payload(
                step,
                physics=physics_by_step.get(step.number),
                reliable=reliable,
                parts=parts,
            )
            for step in section.steps
        ],
    }


def _step_payload(
    step: InstructionStep,
    *,
    physics: dict[str, Any] | None,
    reliable: bool,
    parts: Parts | None,
) -> dict[str, Any]:
    inventory = (
        _step_inventory_payload(step, parts=parts)
        if reliable
        else {
            "status": "skipped",
            "reason": "section has instruction validation errors",
        }
    )
    return {
        "number": step.number,
        "source_start_line": step.source_start_line,
        "source_end_line": step.source_end_line,
        "direct_placements": [piece.reference for piece in step.added_pieces],
        "directives": [directive.to_dict() for directive in step.directives],
        "rotation": _rotation_payload(step),
        "camera": asdict(step.camera),
        "callouts": [
            {
                "mode": str(callout.mode),
                "references": list(callout.references),
                "source_line": callout.source_line,
            }
            for callout in step.callouts
        ],
        "multi_step_group": step.multi_step_group,
        "page_break_before": step.page_break_before,
        "suppressed": step.suppressed,
        "inventory": inventory,
        "physics": physics,
    }


def _step_inventory_payload(
    step: InstructionStep,
    *,
    parts: Parts | None,
) -> dict[str, Any]:
    try:
        added_occurrences = step.added_occurrences()
        cumulative_occurrences = step.cumulative_occurrences()
        return {
            "status": "complete",
            "added_occurrence_count": len(added_occurrences),
            "cumulative_occurrence_count": len(cumulative_occurrences),
            "added_bom": [
                row.to_dict() for row in step.added_bill_of_materials(parts=parts)
            ],
            "cumulative_bom": [
                row.to_dict() for row in step.cumulative_bill_of_materials(parts=parts)
            ],
        }
    except (RecursionError, RuntimeError, ValueError) as error:
        return {
            "status": "error",
            "reason": f"{type(error).__name__}: {error}",
        }


def _rotation_payload(step: InstructionStep) -> dict[str, Any] | None:
    rotation = step.rotation
    if rotation is None:
        return None
    return {
        "mode": str(rotation.mode),
        "angles": list(rotation.angles) if rotation.angles is not None else None,
        "command_matrix": [list(row) for row in rotation.command_matrix.rows],
        "effective_matrix": [list(row) for row in rotation.effective_matrix.rows],
        "source_line": rotation.source_line,
    }


def _instruction_issue_payload(issue: InstructionIssue) -> dict[str, Any]:
    return {
        "section": issue.section,
        "line_number": issue.line_number,
        "code": issue.code,
        "message": issue.message,
        "severity": str(issue.severity),
    }


def _bounds_payload(bounds: BoundingBox | None) -> dict[str, list[float]] | None:
    if bounds is None:
        return None
    return {
        "minimum": _vector_payload(bounds.min) or [],
        "maximum": _vector_payload(bounds.max) or [],
        "size": _vector_payload(bounds.size) or [],
    }


def _vector_payload(vector: Vector | None) -> list[float] | None:
    if vector is None:
        return None
    return [float(vector.x), float(vector.y), float(vector.z)]
