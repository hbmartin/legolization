"""Canonical deterministic assembly manifest and action-graph codec."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Self, cast

from jsonschema import Draft202012Validator

from legolization.errors import ManifestError
from legolization.graph import GROUND_ID, ConnectionGraph
from legolization.instructions.blocking import directional_blockers
from legolization.instructions.bom import bill_of_materials
from legolization.ldraw_out import piece_for
from legolization.repetition import RepeatedComponent, repeated_components

if TYPE_CHECKING:
    from pyldcad import ConnectivityAnalysis

    from legolization.assembly import AssemblyAnalysisResult
    from legolization.graph import KnobContact
    from legolization.instructions.sequencer import InstructionPlan
    from legolization.layout import Layout
    from legolization.pipeline import PipelineResult

MANIFEST_SCHEMA = "legolization.assembly-manifest"
MANIFEST_VERSION = 1
MANIFEST_SCHEMA_PATH = (
    Path(__file__).with_name("data") / "assembly-manifest-v1.schema.json"
)

type ManifestStatus = Literal["complete", "partial", "error"]


@dataclass(frozen=True, slots=True, kw_only=True)
class AssemblyManifest:
    """Versioned JSON-safe record of geometry, evidence, and build actions."""

    status: ManifestStatus
    source: dict[str, Any]
    configuration: dict[str, Any]
    coordinate_system: dict[str, Any]
    parts: tuple[dict[str, Any], ...]
    occurrences: tuple[dict[str, Any], ...]
    contacts: tuple[dict[str, Any], ...]
    action_graph: dict[str, Any]
    support: dict[str, Any]
    load_cases: tuple[dict[str, Any], ...]
    stability: dict[str, Any]
    capabilities: dict[str, str]
    exactness: dict[str, Any]
    templates: tuple[dict[str, Any], ...] = ()
    bom: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    schema: str = MANIFEST_SCHEMA
    version: int = MANIFEST_VERSION

    def to_dict(self) -> dict[str, Any]:
        """Return the canonical JSON-compatible representation."""
        return asdict(self)

    def to_json(self) -> str:
        """Serialize with deterministic keys and whitespace."""
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> Self:
        """Validate and decode a manifest mapping."""
        allowed = {item.name for item in cls.__dataclass_fields__.values()}
        if unknown := sorted(set(payload) - allowed):
            msg = f"unknown manifest keys: {', '.join(unknown)}"
            raise ManifestError(msg)
        if payload.get("schema") != MANIFEST_SCHEMA:
            msg = f"unsupported manifest schema {payload.get('schema')!r}"
            raise ManifestError(msg)
        if payload.get("version") != MANIFEST_VERSION:
            msg = f"unsupported manifest version {payload.get('version')!r}"
            raise ManifestError(msg)
        required = allowed - {"templates", "bom", "artifacts", "warnings", "errors"}
        if missing := sorted(required - set(payload)):
            msg = f"missing manifest keys: {', '.join(missing)}"
            raise ManifestError(msg)
        _validate_json_schema(payload)
        converted = dict(payload)
        for key in (
            "parts",
            "occurrences",
            "contacts",
            "load_cases",
            "templates",
            "warnings",
            "errors",
        ):
            converted[key] = tuple(converted.get(key, ()))
        manifest = cls(**converted)
        _validate_references(manifest)
        return manifest


@lru_cache(maxsize=1)
def manifest_json_schema() -> dict[str, Any]:
    """Load the packaged Draft 2020-12 schema for manifest version 1."""
    return cast("dict[str, Any]", json.loads(MANIFEST_SCHEMA_PATH.read_text()))


def _validate_json_schema(payload: dict[str, Any]) -> None:
    errors = sorted(
        Draft202012Validator(manifest_json_schema()).iter_errors(payload),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if not errors:
        return
    first = errors[0]
    location = ".".join(str(item) for item in first.absolute_path) or "manifest"
    msg = f"invalid manifest at {location}: {first.message}"
    raise ManifestError(msg)


@dataclass(frozen=True, slots=True, kw_only=True)
class BuildManifestMetadata:
    """Run metadata kept separate from the pipeline's domain result."""

    source_path: Path
    configuration: dict[str, Any]
    selected_strategy: str
    support_mode: Literal["baseplate", "table"] = "baseplate"
    exactness: dict[str, Any] | None = None
    artifacts: dict[str, str] | None = None


def manifest_for_build(
    result: PipelineResult,
    metadata: BuildManifestMetadata,
) -> AssemblyManifest:
    """Create the canonical record for one generated layout."""
    layout = result.layout
    graph = ConnectionGraph.from_layout(layout)
    support_ids = frozenset(result.support_ids)
    contacts = tuple(_layout_contact(item) for item in graph.knob_contacts)
    plan = result.plan
    repeated = repeated_components(result.grid) if result.grid is not None else ()
    bom = json.loads(
        bill_of_materials(layout).to_json(model_name=metadata.source_path.stem)
    )
    return AssemblyManifest(
        status="complete",
        source=_source_payload(metadata.source_path),
        configuration=metadata.configuration,
        coordinate_system=_coordinate_system(),
        parts=_build_parts(layout),
        occurrences=_build_occurrences(layout, support_ids=support_ids),
        contacts=contacts,
        action_graph=_build_action_graph(
            layout,
            graph=graph,
            plan=plan,
            repeated=repeated,
            contacts=contacts,
        ),
        support={
            "mode": metadata.support_mode,
            "physical_parts_emitted": bool(support_ids),
            "occurrence_ids": sorted(support_ids),
        },
        load_cases=({"name": "rest", "gravity_g": 1.0},),
        stability=_build_stability(result),
        capabilities={
            "geometry": "complete",
            "connectivity": "complete",
            "physics": "complete",
            "action_plan": "complete" if plan is not None else "not_requested",
        },
        exactness=metadata.exactness
        or {"strategy": metadata.selected_strategy, "status": "heuristic"},
        templates=tuple(_template_payload(item) for item in repeated),
        bom=cast("dict[str, Any]", bom),
        artifacts=metadata.artifacts or {},
        warnings=plan.warnings if plan is not None else (),
    )


def _build_parts(layout: Layout) -> tuple[dict[str, Any], ...]:
    unique = {layout.part_of(brick).key: layout.part_of(brick) for brick in layout}
    return tuple(
        {
            "key": part.key,
            "ldraw_part": part.ldraw_part,
            "category": part.category.value,
            "mass_g": part.mass_g,
        }
        for part in sorted(unique.values(), key=lambda item: item.key)
    )


def _build_occurrences(
    layout: Layout,
    *,
    support_ids: frozenset[int],
) -> tuple[dict[str, Any], ...]:
    target_layer_offset = 1 if support_ids else 0
    return tuple(
        _layout_occurrence(
            layout,
            brick_id,
            support_ids=support_ids,
            target_layer_offset=target_layer_offset,
        )
        for brick_id in sorted(layout.bricks)
    )


def _build_action_graph(
    layout: Layout,
    *,
    graph: ConnectionGraph,
    plan: InstructionPlan | None,
    repeated: tuple[RepeatedComponent, ...],
    contacts: tuple[dict[str, Any], ...],
) -> dict[str, Any]:
    blockers = directional_blockers(layout)
    edges: list[dict[str, Any]] = [
        {
            "kind": "connector",
            "source": _node_id(int(contact["occurrence_ids"][0])),
            "target": _node_id(int(contact["occurrence_ids"][1])),
            "contact": contact["id"],
        }
        for contact in contacts
    ]
    edges.extend(
        {
            "kind": "support",
            "source": _node_id(below),
            "target": _node_id(above),
        }
        for below, above in graph.support_edges()
    )
    edges.extend(
        {
            "kind": f"blocks-{direction}",
            "source": _node_id(blocker),
            "target": _node_id(blocked),
        }
        for direction, blocker_map in blockers.items()
        for blocked, blocked_by in sorted(blocker_map.items())
        for blocker in sorted(blocked_by)
    )
    if plan is not None:
        order = plan.order
        edges.extend(
            {"kind": "ordering", "source": _node_id(left), "target": _node_id(right)}
            for left, right in pairwise(order)
        )
        edges.extend(
            {
                "kind": "subassembly-member",
                "source": f"subassembly:{subassembly.name}",
                "target": _node_id(brick_id),
            }
            for subassembly in plan.subassemblies
            for brick_id in subassembly.brick_ids
        )
    edges.extend(_template_edges(layout, repeated))
    return {
        "nodes": [
            *(_node_id(brick_id) for brick_id in sorted(layout.bricks)),
            *(f"template:{item.signature}" for item in repeated),
            *(
                f"subassembly:{item.name}"
                for item in (() if plan is None else plan.subassemblies)
            ),
        ],
        "edges": sorted(
            edges,
            key=lambda item: (item["kind"], item["source"], item["target"]),
        ),
        "steps": _steps_payload(plan),
        "subassemblies": _subassemblies_payload(plan),
    }


def _build_stability(result: PipelineResult) -> dict[str, Any]:
    return {
        "stable": result.stability.stable,
        "status": result.stability.status,
        "objective": result.stability.objective,
        "max_score": result.stability.max_score,
        "minimum_capacity_n": result.stability.min_capacity,
        "weakest_pair": list(result.stability.weakest_pair)
        if result.stability.weakest_pair is not None
        else None,
        "component_count": result.component_count,
        "floating_count": result.floating_count,
        "interface_forces": [
            {
                "occurrence_ids": list(pair),
                "normal_n": forces[0],
                "drag_n": forces[1],
            }
            for pair, forces in sorted(result.stability.interface_forces.items())
        ],
        "instruction_certification": _instruction_certification_payload(result),
    }


def manifest_for_analysis(
    result: AssemblyAnalysisResult,
    *,
    configuration: dict[str, Any],
    artifacts: dict[str, str] | None = None,
) -> AssemblyManifest:
    """Create a canonical manifest from tolerant arbitrary-LDraw analysis."""
    report = result.report
    connectivity = result.connectivity
    occurrences = tuple(report.occurrences)
    part_ids = sorted({str(item["part_id"]) for item in occurrences})
    status: ManifestStatus = report.status
    contacts = _analysis_contacts(connectivity)
    action_edges = tuple(
        {
            "kind": "connector",
            "source": f"part:{item['occurrence_ids'][0]}",
            "target": f"part:{item['occurrence_ids'][1]}",
            "contact": item["id"],
        }
        for item in contacts
    )
    physics_rows = tuple(
        cast("dict[str, Any]", item) for item in report.physics.get("scenarios", ())
    )
    capabilities = {
        "geometry": "complete" if occurrences else "error",
        "connectivity": "complete" if connectivity is not None else "error",
        "physics": "not_requested"
        if report.physics_verdict == "not_run"
        else ("complete" if report.physics_verdict != "indeterminate" else "partial"),
        "action_plan": "not_requested",
    }
    if "partial" in capabilities.values() and status == "complete":
        status = "partial"
    return AssemblyManifest(
        status=status,
        source=dict(report.input),
        configuration=configuration,
        coordinate_system=_coordinate_system(),
        parts=tuple({"part_id": part_id} for part_id in part_ids),
        occurrences=occurrences,
        contacts=contacts,
        action_graph={
            "nodes": [f"part:{item['occurrence_id']}" for item in occurrences],
            "edges": list(action_edges),
            "steps": [],
            "subassemblies": [],
        },
        support=dict(report.support),
        load_cases=physics_rows,
        stability=dict(report.physics),
        capabilities=capabilities,
        exactness={"strategy": "analysis", "status": "not_applicable"},
        artifacts=artifacts or dict(report.artifacts),
        warnings=tuple(report.warnings),
        errors=tuple(report.errors),
    )


def read_manifest(path: Path) -> AssemblyManifest:
    """Read and validate a manifest file."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        msg = f"cannot read manifest {path}: {error}"
        raise ManifestError(msg) from error
    if not isinstance(payload, dict):
        msg = "manifest root must be a JSON object"
        raise ManifestError(msg)
    return AssemblyManifest.from_dict(cast("dict[str, Any]", payload))


def write_manifest(manifest: AssemblyManifest, path: Path) -> None:
    """Atomically write one deterministic manifest."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(manifest.to_json())
    temporary.replace(path)


def validate_manifest(
    manifest: AssemblyManifest,
    *,
    against: Path | None = None,
) -> tuple[str, ...]:
    """Return semantic/source-integrity validation errors."""
    errors = list(_reference_errors(manifest))
    if against is not None:
        expected = manifest.source.get("sha256")
        if expected is None:
            errors.append("manifest source has no sha256")
        elif not against.is_file():
            errors.append(f"comparison model does not exist: {against}")
        elif _sha256(against) != expected:
            errors.append("comparison model sha256 does not match manifest source")
    return tuple(errors)


def _layout_occurrence(
    layout: Layout,
    brick_id: int,
    *,
    support_ids: frozenset[int],
    target_layer_offset: int,
) -> dict[str, Any]:
    brick = layout.bricks[brick_id]
    part = layout.part_of(brick)
    piece = piece_for(layout, brick)
    matrix = [float(value) for value in piece.matrix.flatten()]
    is_support = brick_id in support_ids
    target_cells = (
        []
        if is_support
        else [
            [x, y, z - target_layer_offset]
            for x, y, z in sorted(layout.filled_cells_of(brick))
        ]
    )
    return {
        "occurrence_id": brick_id,
        "node_id": _node_id(brick_id),
        "part_key": brick.part_key,
        "part_id": part.ldraw_part,
        "colour_code": brick.colour_code,
        "position_ldu": [
            float(piece.position.x),
            float(piece.position.y),
            float(piece.position.z),
        ],
        "matrix": matrix,
        "yaw": brick.yaw,
        "role": "support" if is_support else "model",
        "target_cells": target_cells,
        "collision_cells": [list(cell) for cell in sorted(layout.cells_of(brick))],
        "collision_volumes_ldu": [
            {
                "minimum": list(box.minimum),
                "maximum": list(box.maximum),
            }
            for box in layout.collision_boxes_of(brick)
        ],
    }


def _layout_contact(contact: KnobContact) -> dict[str, Any]:
    return {
        "id": (
            f"contact:{contact.below_id}:{contact.above_id}:"
            f"{contact.x}:{contact.y}:{contact.interface_layer}"
        ),
        "occurrence_ids": [contact.below_id, contact.above_id],
        "kind": "stud" if contact.normal == (0, 0, 1) else "side-stud",
        "point_ldu": list(contact.point_ldu) if contact.point_ldu is not None else None,
        "point_grid": [contact.x, contact.y, contact.interface_layer],
        "normal": list(contact.normal),
        "status": "confirmed",
        "raw_evidence_ids": [],
    }


def _analysis_contacts(
    connectivity: ConnectivityAnalysis | None,
) -> tuple[dict[str, Any], ...]:
    if connectivity is None:
        return ()
    return tuple(
        {
            "id": connection.connection_id,
            "occurrence_ids": list(connection.occurrence_ids),
            "kind": str(connection.kind),
            "point_ldu": list(connection.point_ldu.to_tuple()),
            "normal": list(connection.axis.to_tuple()),
            "status": str(getattr(connection.status, "value", connection.status)),
            "confidence": connection.confidence,
            "raw_evidence_ids": connection.connection_id.removeprefix(
                "physical:"
            ).split("|"),
            "evidence": [
                {
                    "provider": evidence.provider,
                    "strength": str(
                        getattr(evidence.strength, "value", evidence.strength)
                    ),
                    "detail": evidence.detail,
                }
                for evidence in connection.evidence
            ],
        }
        for connection in connectivity.connections
    )


def _steps_payload(plan: InstructionPlan | None) -> list[dict[str, Any]]:
    if plan is None:
        return []
    return [
        {
            "index": step.index,
            "occurrence_ids": list(step.brick_ids),
            "prefix_stable": step.prefix_stable,
            "prefix_max_score": step.prefix_max_score,
            "submodel": step.submodel,
            "attaches": step.attaches,
            "insertion_fragile": step.insertion_fragile,
        }
        for step in plan.steps
    ]


def _instruction_certification_payload(result: PipelineResult) -> dict[str, Any]:
    certification = result.instruction_certification
    if certification is None:
        return {"status": "not_requested", "cold_prefix_count": 0}
    failure = certification.earliest_failure
    return {
        "status": "complete" if certification.valid else "error",
        "cold_prefix_count": certification.cold_prefix_count,
        "violations": list(certification.violations),
        "earliest_failure": (
            None
            if failure is None
            else {
                "step_index": failure.step_index,
                "action": failure.action,
                "occurrence_ids": list(failure.occurrence_ids),
                "unstable_ids": list(failure.unstable_ids),
                "max_score": failure.max_score,
            }
        ),
    }


def _subassemblies_payload(plan: InstructionPlan | None) -> list[dict[str, Any]]:
    if plan is None:
        return []
    return [
        {
            "name": item.name,
            "occurrence_ids": list(item.brick_ids),
            "anchor_layer": item.anchor_layer,
        }
        for item in plan.subassemblies
    ]


def _template_payload(item: RepeatedComponent) -> dict[str, Any]:
    return {
        "signature": item.signature,
        "canonical_cells": [list(cell) for cell in item.canonical_cells],
        "instances": [
            {
                "origin": list(instance.origin),
                "canonical_yaw": instance.canonical_yaw,
                "colour_mapping": [list(pair) for pair in instance.colour_mapping],
            }
            for instance in item.instances
        ],
    }


def _template_edges(
    layout: Layout,
    repeated: tuple[RepeatedComponent, ...],
) -> list[dict[str, Any]]:
    edges: list[dict[str, Any]] = []
    for item in repeated:
        for instance_index, instance in enumerate(item.instances):
            edges.extend(
                {
                    "kind": "template-instance",
                    "source": f"template:{item.signature}",
                    "target": _node_id(brick.brick_id),
                    "instance": instance_index,
                }
                for brick in layout
                if set(layout.filled_cells_of(brick)) <= instance.cells
            )
    return edges


def _node_id(brick_id: int) -> str:
    return "ground" if brick_id == GROUND_ID else f"part:{brick_id}"


def _coordinate_system() -> dict[str, Any]:
    return {
        "units": "LDU",
        "handedness": "LDraw",
        "axes": {"x": "right", "y": "down", "z": "forward"},
        "stud_pitch_ldu": 20,
        "plate_height_ldu": 8,
        "generated_orientations": "yaw-orthogonal",
    }


def _source_payload(path: Path) -> dict[str, Any]:
    return {
        "name": path.name,
        "format": path.suffix.lower(),
        "sha256": _sha256(path),
        "size_bytes": path.stat().st_size,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_references(manifest: AssemblyManifest) -> None:
    if errors := _reference_errors(manifest):
        raise ManifestError("; ".join(errors))


def _reference_errors(manifest: AssemblyManifest) -> tuple[str, ...]:
    nodes = {str(item) for item in manifest.action_graph.get("nodes", ())}
    errors: list[str] = []
    if len(nodes) != len(manifest.action_graph.get("nodes", ())):
        errors.append("action graph contains duplicate node ids")
    for edge in manifest.action_graph.get("edges", ()):
        if not isinstance(edge, dict):
            errors.append("action graph edge must be an object")
            continue
        for endpoint in ("source", "target"):
            value = str(edge.get(endpoint))
            if value != "ground" and value not in nodes:
                errors.append(f"action graph edge references unknown node {value!r}")
    occurrence_ids = [item.get("occurrence_id") for item in manifest.occurrences]
    if len(set(occurrence_ids)) != len(occurrence_ids):
        errors.append("manifest contains duplicate occurrence ids")
    return tuple(errors)
