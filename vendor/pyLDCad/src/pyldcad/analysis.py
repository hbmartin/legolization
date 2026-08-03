"""Geometry-first connectivity analysis over pyldraw3 inspection results."""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import TYPE_CHECKING

from ldraw import (
    ConnectionFeature as LDrawConnectionFeature,
)
from ldraw import (
    ConnectionFreedom as LDrawConnectionFreedom,
)
from ldraw import (
    ConnectionKind as LDrawConnectionKind,
)
from ldraw import (
    ConnectionRole as LDrawConnectionRole,
)
from ldraw import (
    ConnectionSource as LDrawConnectionSource,
)
from ldraw.geometry import Vector

from pyldcad.models import (
    Connection,
    ConnectionCapacity,
    ConnectionEvidence,
    ConnectionKind,
    ConnectionStatus,
    ConnectivityAnalysis,
    ConnectivityDiagnostic,
    ConnectorDefinition,
    ConnectorEndpoint,
    ConnectorRole,
    CoverageLevel,
    DegreesOfFreedom,
    EvidenceStrength,
    OccurrenceCoverage,
    SourceReference,
    Vector3,
)
from pyldcad.providers import (
    LDCadProvider,
    NativeProvider,
    StudioProvider,
    builtin_fragment,
)
from pyldcad.registry import (
    ConnectivityRegistry,
    RegistryFragment,
    merge_fragments,
    normalize_part_id,
)

if TYPE_CHECKING:
    from ldraw import BoundingBox, ModelAnalysis, OccurrenceGeometry


@dataclass(frozen=True, slots=True)
class ConnectivityConfig:
    """Provider inputs and geometric matching tolerances."""

    native_catalog_paths: tuple[Path, ...] = ()
    ldcad_metadata_paths: tuple[Path, ...] = ()
    studio_metadata_paths: tuple[Path, ...] = ()
    point_tolerance_ldu: float = 0.25
    face_tolerance_ldu: float = 0.25
    axis_alignment: float = 0.98
    infer_surface_contacts: bool = True
    registry: ConnectivityRegistry | None = None

    def __post_init__(self) -> None:
        if self.point_tolerance_ldu < 0 or self.face_tolerance_ldu < 0:
            msg = "connection tolerances must be non-negative"
            raise ValueError(msg)
        if not 0 <= self.axis_alignment <= 1:
            msg = "axis_alignment must be between zero and one"
            raise ValueError(msg)


@dataclass(slots=True)
class _UnionFind:
    parent: dict[int, int]

    @classmethod
    def create(cls, occurrence_count: int) -> _UnionFind:
        return cls(parent={item: item for item in range(1, occurrence_count + 1)})

    def find(self, item: int) -> int:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parent[max(left_root, right_root)] = min(left_root, right_root)

    def components(self) -> tuple[tuple[int, ...], ...]:
        grouped: dict[int, list[int]] = {}
        for item in sorted(self.parent):
            grouped.setdefault(self.find(item), []).append(item)
        return tuple(tuple(items) for _, items in sorted(grouped.items()))


def analyze_connectivity(
    model_analysis: ModelAnalysis,
    config: ConnectivityConfig | None = None,
) -> ConnectivityAnalysis:
    """Build confirmed and optimistic topology without re-parsing a model."""
    config = config or ConnectivityConfig()
    inspection = model_analysis.inspection
    if inspection is None:
        msg = "model analysis has no inspection; analyze it with a parts catalog"
        raise ValueError(msg)
    registry = config.registry or build_registry(config)
    diagnostics = list(registry.diagnostics)
    endpoints: list[ConnectorEndpoint] = []
    feature_endpoints: dict[int, ConnectorEndpoint] = {}
    endpoints_by_occurrence: dict[int, list[ConnectorEndpoint]] = {
        item: [] for item in range(1, inspection.occurrence_count + 1)
    }
    geometry_by_id = {item.index + 1: item for item in inspection.occurrences}
    for geometry in inspection.occurrences:
        occurrence_id = geometry.index + 1
        primitive = _primitive_endpoints(geometry)
        inspected_features = _feature_endpoints(geometry)
        registered = _registered_endpoints(geometry, registry)
        combined = _deduplicate_endpoints(
            (*primitive, *(item[1] for item in inspected_features), *registered)
        )
        by_signature = {_endpoint_signature(item): item for item in combined}
        feature_endpoints.update(
            {
                id(feature): by_signature[_endpoint_signature(endpoint)]
                for feature, endpoint in inspected_features
            }
        )
        endpoints.extend(combined)
        endpoints_by_occurrence[occurrence_id].extend(combined)
    connections = list(
        _stud_connections(
            inspection.occurrences,
            endpoints_by_occurrence=endpoints_by_occurrence,
            face_tolerance=config.face_tolerance_ldu,
        )
    )
    connections.extend(
        _inspection_connections(
            inspection,
            feature_endpoints=feature_endpoints,
            tolerance=config.point_tolerance_ldu,
            axis_alignment=config.axis_alignment,
        )
    )
    connected_endpoint_keys = {
        endpoint.key
        for connection in connections
        for endpoint in (connection.endpoint_a, connection.endpoint_b)
    }
    explicit_connections = _match_explicit_endpoints(
        endpoints,
        point_tolerance=config.point_tolerance_ldu,
        axis_alignment=config.axis_alignment,
        skip_keys=connected_endpoint_keys,
    )
    connections.extend(explicit_connections)
    if config.infer_surface_contacts:
        connections.extend(
            _surface_connections(
                inspection.occurrences,
                existing_pairs={edge.occurrence_ids for edge in connections},
                tolerance=config.face_tolerance_ldu,
            )
        )
    connections = sorted(
        _deduplicate_connections(connections),
        key=lambda edge: edge.connection_id,
    )
    matched = {
        endpoint.key
        for connection in connections
        for endpoint in (connection.endpoint_a, connection.endpoint_b)
        if not endpoint.synthetic
    }
    coverage = tuple(
        _coverage(
            occurrence_id,
            geometry=geometry_by_id.get(occurrence_id),
            endpoints=endpoints_by_occurrence[occurrence_id],
            registry=registry,
        )
        for occurrence_id in range(1, inspection.occurrence_count + 1)
    )
    confirmed = _UnionFind.create(inspection.occurrence_count)
    optimistic = _UnionFind.create(inspection.occurrence_count)
    for connection in connections:
        left, right = connection.occurrence_ids
        optimistic.union(left, right)
        if connection.status is ConnectionStatus.CONFIRMED:
            confirmed.union(left, right)
    diagnostics.extend(
        ConnectivityDiagnostic(
            code="geometry.unresolved_occurrence",
            message=item.reason,
            part_id=normalize_part_id(item.attribution.occurrence.part_code),
            source=item.attribution.model_path[-1]
            if item.attribution.model_path
            else None,
        )
        for item in inspection.skipped_geometry
    )
    return ConnectivityAnalysis(
        occurrence_count=inspection.occurrence_count,
        endpoints=tuple(sorted(endpoints, key=lambda endpoint: endpoint.key)),
        connections=tuple(connections),
        unmatched_endpoints=tuple(
            endpoint.key
            for endpoint in sorted(endpoints, key=lambda item: item.key)
            if endpoint.key not in matched
        ),
        coverage=coverage,
        confirmed_components=confirmed.components(),
        optimistic_components=optimistic.components(),
        diagnostics=tuple(diagnostics),
    )


def build_registry(config: ConnectivityConfig | None = None) -> ConnectivityRegistry:
    """Load and merge every configured provider without analyzing a model."""
    config = config or ConnectivityConfig()
    fragments: list[RegistryFragment] = [builtin_fragment()]
    providers = (
        (LDCadProvider(), config.ldcad_metadata_paths, 30),
        (StudioProvider(), config.studio_metadata_paths, 40),
        (NativeProvider(), config.native_catalog_paths, 50),
    )
    for provider, paths, priority in providers:
        fragments.extend(
            provider.load(path, priority=priority + index)
            for index, path in enumerate(paths)
        )
    return merge_fragments(*fragments)


def _primitive_endpoints(geometry: OccurrenceGeometry) -> tuple[ConnectorEndpoint, ...]:
    source = _source_reference(geometry)
    endpoints: list[ConnectorEndpoint] = []
    for index, stud in enumerate(geometry.studs):
        if stud.is_placeholder or abs(stud.up) == 0:
            continue
        role = ConnectorRole.FEMALE if stud.is_receptacle else ConnectorRole.MALE
        evidence = ConnectionEvidence(
            provider="ldraw-primitives",
            strength=EvidenceStrength.EXACT,
            detail=f"{stud.name}: {stud.description}",
        )
        endpoints.append(
            ConnectorEndpoint(
                occurrence_id=geometry.index + 1,
                part_id=normalize_part_id(geometry.occurrence.part_code),
                connector_id=f"primitive:{index}:{stud.name}",
                kind=ConnectionKind.STUD,
                role=role,
                point_ldu=_vector3(stud.position),
                axis=_vector3(stud.up).normalized(),
                degrees_of_freedom=DegreesOfFreedom(),
                compatible_kinds=(ConnectionKind.STUD,),
                capacity=_stud_capacity(),
                evidence=(evidence,),
                confidence=1.0,
                source=source,
            )
        )
    return tuple(endpoints)


def _feature_endpoints(
    geometry: OccurrenceGeometry,
) -> tuple[tuple[LDrawConnectionFeature, ConnectorEndpoint], ...]:
    """Adapt public pyldraw3 connection features into normalized endpoints."""
    source = _source_reference(geometry)
    endpoints: list[tuple[LDrawConnectionFeature, ConnectorEndpoint]] = []
    features: tuple[LDrawConnectionFeature, ...] = getattr(
        geometry,
        "connections",
        (),
    )
    for index, feature in enumerate(features):
        if abs(feature.axis) == 0 or feature.confidence <= 0:
            continue
        kind = _feature_kind(feature)
        evidence = ConnectionEvidence(
            provider=f"pyldraw3:{feature.source.value}",
            strength=_feature_strength(feature.source),
            detail=(
                f"{feature.kind.value} feature"
                f"{f' {feature.name}' if feature.name else ''}"
            ),
            source=" > ".join(feature.provenance) or None,
        )
        connector_id = feature.feature_id or feature.name or feature.kind.value
        endpoints.append(
            (
                feature,
                ConnectorEndpoint(
                    occurrence_id=geometry.index + 1,
                    part_id=normalize_part_id(geometry.occurrence.part_code),
                    connector_id=f"pyldraw:{index}:{connector_id}",
                    kind=kind,
                    role=_feature_role(feature.role),
                    point_ldu=_vector3(feature.position),
                    axis=_vector3(feature.axis).normalized(),
                    degrees_of_freedom=_feature_dof(feature),
                    compatible_kinds=(kind,),
                    capacity=ConnectionCapacity(),
                    evidence=(evidence,),
                    confidence=feature.confidence,
                    source=source,
                ),
            )
        )
    return tuple(endpoints)


def _inspection_connections(
    inspection: object,
    *,
    feature_endpoints: dict[int, ConnectorEndpoint],
    tolerance: float,
    axis_alignment: float,
) -> tuple[Connection, ...]:
    """Use pyldraw3's profile-aware feature mating results when available."""
    contacts_method = getattr(inspection, "connection_contacts", None)
    if contacts_method is None:
        return ()
    angular_tolerance = math.degrees(math.acos(axis_alignment))
    edges: list[Connection] = []
    for contact in contacts_method(
        tolerance=tolerance,
        angular_tolerance=angular_tolerance,
    ):
        left = feature_endpoints.get(id(contact.first))
        right = feature_endpoints.get(id(contact.second))
        if left is None or right is None:
            continue
        strengths = {
            evidence.strength
            for endpoint in (left, right)
            for evidence in endpoint.evidence
        }
        status = (
            ConnectionStatus.POTENTIAL
            if EvidenceStrength.INFERRED in strengths
            else ConnectionStatus.CONFIRMED
        )
        edges.append(
            _connection(
                left,
                right,
                status=status,
                detail=(
                    "pyldraw3 profile match "
                    f"(distance={contact.residual.distance:.4g}, "
                    f"axial_gap={contact.residual.axial_gap:.4g})"
                ),
            )
        )
    return tuple(edges)


def _feature_kind(feature: LDrawConnectionFeature) -> ConnectionKind:
    match feature.kind:
        case LDrawConnectionKind.STUD | LDrawConnectionKind.STUD_RECEPTACLE:
            return ConnectionKind.STUD
        case LDrawConnectionKind.BAR | LDrawConnectionKind.CLIP:
            return ConnectionKind.BAR_CLIP
        case LDrawConnectionKind.PIN | LDrawConnectionKind.PIN_HOLE:
            return ConnectionKind.TECHNIC_PIN
        case LDrawConnectionKind.AXLE | LDrawConnectionKind.AXLE_HOLE:
            return ConnectionKind.AXLE
        case LDrawConnectionKind.HINGE:
            return ConnectionKind.HINGE
        case LDrawConnectionKind.RIM_SEAT | LDrawConnectionKind.TYRE_BEAD:
            return ConnectionKind.TYRE_RIM
        case LDrawConnectionKind.GENERIC:
            label = (feature.group or feature.name or "generic").casefold()
            if "ball" in label:
                return ConnectionKind.BALL_JOINT
            if "turntable" in label:
                return ConnectionKind.TURNTABLE
            normalized = "_".join(label.replace(":", " ").split())
            return ConnectionKind(f"ldraw:{normalized or 'generic'}")


def _feature_role(role: LDrawConnectionRole) -> ConnectorRole:
    match role:
        case LDrawConnectionRole.MALE:
            return ConnectorRole.MALE
        case LDrawConnectionRole.FEMALE:
            return ConnectorRole.FEMALE
        case LDrawConnectionRole.NEUTRAL:
            return ConnectorRole.BIDIRECTIONAL


def _feature_dof(feature: LDrawConnectionFeature) -> DegreesOfFreedom:
    slide = LDrawConnectionFreedom.SLIDE in feature.freedoms
    rotate = bool(
        {
            LDrawConnectionFreedom.ROTATE,
            LDrawConnectionFreedom.DISCRETE_ROTATE,
        }
        & feature.freedoms
    )
    return DegreesOfFreedom(
        translations=(False, False, slide),
        rotations=(False, False, rotate),
    )


def _feature_strength(source: LDrawConnectionSource) -> EvidenceStrength:
    match source:
        case LDrawConnectionSource.HEURISTIC:
            return EvidenceStrength.INFERRED
        case (
            LDrawConnectionSource.LDCAD_INLINE
            | LDrawConnectionSource.LDCAD_SHADOW
            | LDrawConnectionSource.STUDIO
        ):
            return EvidenceStrength.IMPORTED
        case LDrawConnectionSource.OVERRIDE:
            return EvidenceStrength.CURATED
        case LDrawConnectionSource.PRIMITIVE | LDrawConnectionSource.SHORTCUT:
            return EvidenceStrength.EXACT


def _registered_endpoints(
    geometry: OccurrenceGeometry,
    registry: ConnectivityRegistry,
) -> tuple[ConnectorEndpoint, ...]:
    metadata = registry.get(geometry.occurrence.part_code)
    if metadata is None:
        return ()
    source = _source_reference(geometry)
    occurrence = geometry.occurrence
    endpoints: list[ConnectorEndpoint] = []
    for definition in metadata.connectors:
        local_point = Vector(*definition.point_ldu.to_tuple())
        local_axis = Vector(*definition.axis.to_tuple())
        endpoints.append(
            ConnectorEndpoint(
                occurrence_id=geometry.index + 1,
                part_id=metadata.part_id,
                connector_id=definition.connector_id,
                kind=definition.kind,
                role=definition.role,
                point_ldu=_vector3(
                    occurrence.position + occurrence.matrix * local_point
                ),
                axis=_vector3(occurrence.matrix * local_axis).normalized(),
                degrees_of_freedom=definition.degrees_of_freedom,
                compatible_kinds=definition.compatible_kinds,
                capacity=definition.capacity,
                evidence=definition.evidence,
                confidence=definition.confidence,
                source=source,
            )
        )
    return tuple(endpoints)


def _stud_connections(
    occurrences: tuple[OccurrenceGeometry, ...],
    *,
    endpoints_by_occurrence: dict[int, list[ConnectorEndpoint]],
    face_tolerance: float,
) -> tuple[Connection, ...]:
    by_index = {item.index: item for item in occurrences}
    edges: list[Connection] = []
    for stud_geometry in occurrences:
        male_endpoints = [
            endpoint
            for endpoint in endpoints_by_occurrence[stud_geometry.index + 1]
            if endpoint.kind == ConnectionKind.STUD
            and endpoint.role is ConnectorRole.MALE
            and endpoint.connector_id.startswith("primitive:")
        ]
        for endpoint in male_endpoints:
            probe = endpoint.point_ldu.plus(endpoint.axis.scaled(0.1))
            for candidate in occurrences:
                if candidate.index == stud_geometry.index:
                    continue
                receptacles = (
                    item
                    for item in endpoints_by_occurrence[candidate.index + 1]
                    if item.kind == ConnectionKind.STUD
                    and item.role is ConnectorRole.FEMALE
                )
                if not any(
                    item.axis.dot(endpoint.axis) <= -0.9 for item in receptacles
                ):
                    continue
                if not _box_contains(
                    candidate.bounds, endpoint.point_ldu, face_tolerance
                ):
                    continue
                if not _box_contains(candidate.bounds, probe, face_tolerance):
                    continue
                if not _on_entry_face(
                    candidate.bounds,
                    endpoint.point_ldu,
                    endpoint.axis,
                    face_tolerance,
                ):
                    continue
                supported_id = candidate.index + 1
                synthetic = ConnectorEndpoint(
                    occurrence_id=supported_id,
                    part_id=normalize_part_id(candidate.occurrence.part_code),
                    connector_id=f"synthetic:receptacle:{endpoint.key}",
                    kind=ConnectionKind.STUD,
                    role=ConnectorRole.FEMALE,
                    point_ldu=endpoint.point_ldu,
                    axis=endpoint.axis.scaled(-1),
                    degrees_of_freedom=DegreesOfFreedom(),
                    compatible_kinds=(ConnectionKind.STUD,),
                    capacity=_stud_capacity(),
                    evidence=(
                        ConnectionEvidence(
                            provider="ldraw-geometry",
                            strength=EvidenceStrength.EXACT,
                            detail=(
                                "stud base lies on the supported occurrence entry face"
                            ),
                        ),
                    ),
                    confidence=1.0,
                    source=_source_reference(by_index[candidate.index]),
                    synthetic=True,
                )
                edges.append(
                    _connection(
                        endpoint,
                        synthetic,
                        status=ConnectionStatus.CONFIRMED,
                        detail="refined transformed stud contact",
                    )
                )
    return tuple(edges)


def _match_explicit_endpoints(
    endpoints: list[ConnectorEndpoint],
    *,
    point_tolerance: float,
    axis_alignment: float,
    skip_keys: set[str],
) -> tuple[Connection, ...]:
    edges: list[Connection] = []
    for left, right in combinations(endpoints, 2):
        if left.occurrence_id == right.occurrence_id:
            continue
        if left.key in skip_keys or right.key in skip_keys:
            continue
        if left.point_ldu.distance_to(right.point_ldu) > point_tolerance:
            continue
        left_definition = _definition(left)
        right_definition = _definition(right)
        if not left_definition.accepts(right_definition):
            continue
        if left.axis.dot(right.axis) > -axis_alignment:
            continue
        strengths = {
            evidence.strength
            for endpoint in (left, right)
            for evidence in endpoint.evidence
        }
        status = (
            ConnectionStatus.POTENTIAL
            if EvidenceStrength.INFERRED in strengths
            else ConnectionStatus.CONFIRMED
        )
        edges.append(
            _connection(
                left, right, status=status, detail="matched connector endpoints"
            )
        )
    return tuple(edges)


def _surface_connections(
    occurrences: tuple[OccurrenceGeometry, ...],
    *,
    existing_pairs: set[tuple[int, int]],
    tolerance: float,
) -> tuple[Connection, ...]:
    edges: list[Connection] = []
    for left, right in combinations(occurrences, 2):
        pair = (left.index + 1, right.index + 1)
        if pair in existing_pairs:
            continue
        if (contact := _face_contact(left.bounds, right.bounds, tolerance)) is None:
            continue
        point, axis = contact
        evidence = ConnectionEvidence(
            provider="geometry-inference",
            strength=EvidenceStrength.INFERRED,
            detail="opposed AABB faces touch with positive overlap area",
        )
        first = _surface_endpoint(left, point=point, axis=axis, evidence=evidence)
        second = _surface_endpoint(
            right,
            point=point,
            axis=axis.scaled(-1),
            evidence=evidence,
        )
        edges.append(
            _connection(
                first,
                second,
                status=ConnectionStatus.POTENTIAL,
                detail="inferred surface contact",
            )
        )
    return tuple(edges)


def _surface_endpoint(
    geometry: OccurrenceGeometry,
    *,
    point: Vector3,
    axis: Vector3,
    evidence: ConnectionEvidence,
) -> ConnectorEndpoint:
    other_dof = (True, True, False)
    return ConnectorEndpoint(
        occurrence_id=geometry.index + 1,
        part_id=normalize_part_id(geometry.occurrence.part_code),
        connector_id=f"inferred:surface:{point.x:.3f}:{point.y:.3f}:{point.z:.3f}",
        kind=ConnectionKind.SURFACE_CONTACT,
        role=ConnectorRole.BIDIRECTIONAL,
        point_ldu=point,
        axis=axis,
        degrees_of_freedom=DegreesOfFreedom(
            translations=other_dof,
            rotations=(True, True, True),
        ),
        compatible_kinds=(ConnectionKind.SURFACE_CONTACT,),
        capacity=ConnectionCapacity(friction_coefficient=None),
        evidence=(evidence,),
        confidence=0.5,
        source=_source_reference(geometry),
        synthetic=True,
    )


def _connection(
    left: ConnectorEndpoint,
    right: ConnectorEndpoint,
    *,
    status: ConnectionStatus,
    detail: str,
) -> Connection:
    first, second = sorted((left, right), key=lambda item: item.key)
    kind = first.kind
    evidence = (
        *first.evidence,
        *second.evidence,
        ConnectionEvidence(
            provider="pyldcad",
            strength=(
                EvidenceStrength.EXACT
                if status is ConnectionStatus.CONFIRMED
                else EvidenceStrength.INFERRED
            ),
            detail=detail,
        ),
    )
    return Connection(
        connection_id=f"{first.key}<->{second.key}",
        endpoint_a=first,
        endpoint_b=second,
        point_ldu=Vector3(
            (first.point_ldu.x + second.point_ldu.x) / 2,
            (first.point_ldu.y + second.point_ldu.y) / 2,
            (first.point_ldu.z + second.point_ldu.z) / 2,
        ),
        axis=first.axis,
        kind=kind,
        degrees_of_freedom=_combined_dof(first, second),
        capacity=_combined_capacity(first.capacity, second.capacity),
        evidence=evidence,
        confidence=min(first.confidence, second.confidence),
        status=status,
    )


def _definition(endpoint: ConnectorEndpoint) -> ConnectorDefinition:
    return ConnectorDefinition(
        connector_id=endpoint.connector_id,
        kind=endpoint.kind,
        role=endpoint.role,
        point_ldu=endpoint.point_ldu,
        axis=endpoint.axis,
        degrees_of_freedom=endpoint.degrees_of_freedom,
        compatible_kinds=endpoint.compatible_kinds,
        capacity=endpoint.capacity,
        evidence=endpoint.evidence,
        confidence=endpoint.confidence,
    )


def _combined_dof(
    left: ConnectorEndpoint,
    right: ConnectorEndpoint,
) -> DegreesOfFreedom:
    left_translations = left.degrees_of_freedom.translations
    right_translations = right.degrees_of_freedom.translations
    left_rotations = left.degrees_of_freedom.rotations
    right_rotations = right.degrees_of_freedom.rotations
    return DegreesOfFreedom(
        translations=(
            left_translations[0] or right_translations[0],
            left_translations[1] or right_translations[1],
            left_translations[2] or right_translations[2],
        ),
        rotations=(
            left_rotations[0] or right_rotations[0],
            left_rotations[1] or right_rotations[1],
            left_rotations[2] or right_rotations[2],
        ),
    )


def _combined_capacity(
    left: ConnectionCapacity,
    right: ConnectionCapacity,
) -> ConnectionCapacity:
    def minimum(first: float | None, second: float | None) -> float | None:
        if first is None or second is None:
            return None
        return min(first, second)

    return ConnectionCapacity(
        pull_n=minimum(left.pull_n, right.pull_n),
        compression_n=minimum(left.compression_n, right.compression_n),
        shear_n=minimum(left.shear_n, right.shear_n),
        torque_nm=minimum(left.torque_nm, right.torque_nm),
        friction_coefficient=minimum(
            left.friction_coefficient,
            right.friction_coefficient,
        ),
    )


def _stud_capacity() -> ConnectionCapacity:
    # StableLego's measured 100 g per-contact capacity remains the existing
    # conservative project default. Torque is transmitted by separated studs.
    return ConnectionCapacity(
        pull_n=0.98,
        compression_n=0.98,
        shear_n=0.98,
        torque_nm=0.0,
        friction_coefficient=1.0,
    )


def _source_reference(geometry: OccurrenceGeometry) -> SourceReference:
    attribution = geometry.attribution
    return SourceReference(
        model_path=attribution.model_path,
        reference_path=attribution.reference_path,
        source_line_path=attribution.source_line_path,
        local_step_path=attribution.local_step_path,
        effective_step_path=attribution.effective_step_path,
        page_path=attribution.page_path,
    )


def _coverage(
    occurrence_id: int,
    *,
    geometry: OccurrenceGeometry | None,
    endpoints: list[ConnectorEndpoint],
    registry: ConnectivityRegistry,
) -> OccurrenceCoverage:
    if geometry is None:
        return OccurrenceCoverage(
            occurrence_id=occurrence_id,
            level=CoverageLevel.NONE,
            endpoint_count=0,
            geometry_complete=False,
            reason="geometry could not be resolved",
        )
    registered = registry.get(geometry.occurrence.part_code)
    geometry_complete = geometry.local.complete
    if registered is not None and registered.connectors:
        level = CoverageLevel.COMPLETE if geometry_complete else CoverageLevel.PARTIAL
        reason = None if geometry_complete else "part geometry is incomplete"
    elif endpoints:
        level = CoverageLevel.PARTIAL
        reason = "only LDraw connector primitives are available"
    else:
        level = CoverageLevel.NONE
        reason = "no connector metadata is available"
    return OccurrenceCoverage(
        occurrence_id=occurrence_id,
        level=level,
        endpoint_count=len(endpoints),
        geometry_complete=geometry_complete,
        reason=reason,
    )


def _deduplicate_endpoints(
    endpoints: tuple[ConnectorEndpoint, ...],
) -> tuple[ConnectorEndpoint, ...]:
    by_signature: dict[tuple[object, ...], ConnectorEndpoint] = {}
    for endpoint in endpoints:
        signature = _endpoint_signature(endpoint)
        existing = by_signature.get(signature)
        if existing is None or endpoint.confidence > existing.confidence:
            by_signature[signature] = endpoint
    return tuple(sorted(by_signature.values(), key=lambda item: item.key))


def _endpoint_signature(endpoint: ConnectorEndpoint) -> tuple[object, ...]:
    return (
        endpoint.kind,
        endpoint.role,
        *(round(value, 4) for value in endpoint.point_ldu.to_tuple()),
        *(round(value, 4) for value in endpoint.axis.to_tuple()),
    )


def _deduplicate_connections(connections: list[Connection]) -> tuple[Connection, ...]:
    by_signature: dict[tuple[object, ...], Connection] = {}
    for edge in connections:
        signature = (
            edge.occurrence_ids,
            edge.kind,
            *(round(value, 3) for value in edge.point_ldu.to_tuple()),
        )
        existing = by_signature.get(signature)
        if existing is None or (
            existing.status is ConnectionStatus.POTENTIAL
            and edge.status is ConnectionStatus.CONFIRMED
        ):
            by_signature[signature] = edge
    return tuple(by_signature.values())


def _box_contains(box: BoundingBox, point: Vector3, tolerance: float) -> bool:
    return (
        box.min.x - tolerance <= point.x <= box.max.x + tolerance
        and box.min.y - tolerance <= point.y <= box.max.y + tolerance
        and box.min.z - tolerance <= point.z <= box.max.z + tolerance
    )


def _on_entry_face(
    box: BoundingBox,
    point: Vector3,
    axis: Vector3,
    tolerance: float,
) -> bool:
    components = axis.to_tuple()
    dominant = max(range(3), key=lambda index: abs(components[index]))
    if abs(components[dominant]) < 0.9:
        return False
    point_values = point.to_tuple()
    minimum = (box.min.x, box.min.y, box.min.z)[dominant]
    maximum = (box.max.x, box.max.y, box.max.z)[dominant]
    entry = minimum if components[dominant] > 0 else maximum
    return math.isclose(point_values[dominant], entry, abs_tol=tolerance)


def _face_contact(
    left: BoundingBox,
    right: BoundingBox,
    tolerance: float,
) -> tuple[Vector3, Vector3] | None:
    left_min = (left.min.x, left.min.y, left.min.z)
    left_max = (left.max.x, left.max.y, left.max.z)
    right_min = (right.min.x, right.min.y, right.min.z)
    right_max = (right.max.x, right.max.y, right.max.z)
    for axis_index in range(3):
        forward_gap = right_min[axis_index] - left_max[axis_index]
        backward_gap = left_min[axis_index] - right_max[axis_index]
        if min(abs(forward_gap), abs(backward_gap)) > tolerance:
            continue
        overlap_axes = [index for index in range(3) if index != axis_index]
        overlaps = [
            min(left_max[index], right_max[index])
            - max(left_min[index], right_min[index])
            for index in overlap_axes
        ]
        if any(overlap <= tolerance for overlap in overlaps):
            continue
        coordinate = (
            (left_max[axis_index] + right_min[axis_index]) / 2
            if abs(forward_gap) <= abs(backward_gap)
            else (left_min[axis_index] + right_max[axis_index]) / 2
        )
        values = [0.0, 0.0, 0.0]
        values[axis_index] = coordinate
        for other in overlap_axes:
            values[other] = (
                max(left_min[other], right_min[other])
                + min(left_max[other], right_max[other])
            ) / 2
        direction = 1.0 if abs(forward_gap) <= abs(backward_gap) else -1.0
        axis = [0.0, 0.0, 0.0]
        axis[axis_index] = direction
        return Vector3(*values), Vector3(*axis)
    return None


def _vector3(value: Vector) -> Vector3:
    return Vector3(float(value.x), float(value.y), float(value.z))
