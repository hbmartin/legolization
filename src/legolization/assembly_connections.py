"""Typed connection analysis over pyldraw3 contacts plus registry connectors.

pyldraw3 1.6 supplies the connection features and the strict geometric
matching (``ModelInspection.connection_contacts``). This layer adds what the
assembly pipeline needs on top: force capacities and degrees of freedom for
the equilibrium solver, catalog-declared connectors with user-defined kinds,
inferred surface contacts, connected components, per-occurrence coverage,
and one deterministic JSON projection.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from typing import TYPE_CHECKING, Any

from ldraw import (
    ConnectionFreedom,
    ConnectionRole,
    ConnectionSource,
    ConnectionStatus,
)
from ldraw.geometry import Vector

from legolization.assembly_registry import (
    STUD_CAPACITY,
    ConnectionCapacity,
    ConnectionDiagnostic,
    ConnectionEvidence,
    ConnectorRegistry,
    CoverageLevel,
    DegreesOfFreedom,
    EvidenceStrength,
    normalize_part_id,
    roles_compatible,
)

if TYPE_CHECKING:
    from ldraw import (
        BoundingBox,
        ConnectionFeature,
        ConnectionResidual,
        ModelInspection,
        OccurrenceGeometry,
    )

SURFACE_CONTACT_KIND = "surface_contact"
_EDGE_KINDS = {
    frozenset({"stud", "stud_receptacle"}): "stud",
    frozenset({"pin", "pin_hole"}): "pin",
    frozenset({"axle", "axle_hole"}): "axle",
    frozenset({"bar", "clip"}): "bar_clip",
    frozenset({"rim_seat", "tyre_bead"}): "tyre_rim",
}
_STUD_KINDS = frozenset({"stud", "stud_receptacle"})


@dataclass(frozen=True, slots=True)
class ConnectionConfig:
    """Geometric matching tolerances for assembly connection analysis."""

    point_tolerance_ldu: float = 0.25
    face_tolerance_ldu: float = 0.25
    axis_alignment: float = 0.98
    infer_surface_contacts: bool = True

    def __post_init__(self) -> None:
        if self.point_tolerance_ldu < 0 or self.face_tolerance_ldu < 0:
            msg = "connection tolerances must be non-negative"
            raise ValueError(msg)
        if not 0 <= self.axis_alignment <= 1:
            msg = "axis_alignment must be between zero and one"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ConnectionEndpoint:
    """One world-placed mating interface on one model occurrence."""

    key: str
    occurrence_id: int
    part_id: str
    kind: str
    role: ConnectionRole
    point_ldu: Vector
    axis: Vector
    degrees_of_freedom: DegreesOfFreedom
    capacity: ConnectionCapacity
    evidence: tuple[ConnectionEvidence, ...]
    confidence: float
    compatible_kinds: tuple[str, ...] = ()
    catalog: bool = False

    def accepts(self, other: ConnectionEndpoint) -> bool:
        """Return whether kind vocabularies and mating roles can pair."""
        compatible = self.compatible_kinds or (self.kind,)
        other_compatible = other.compatible_kinds or (other.kind,)
        return (
            other.kind in compatible
            and self.kind in other_compatible
            and roles_compatible(self.role, other.role)
        )


@dataclass(frozen=True, slots=True)
class ConnectorEdge:
    """One mechanical relationship between two occurrence endpoints."""

    edge_id: str
    first: ConnectionEndpoint
    second: ConnectionEndpoint
    kind: str
    status: ConnectionStatus
    point_ldu: Vector
    axis: Vector
    degrees_of_freedom: DegreesOfFreedom
    capacity: ConnectionCapacity
    evidence: tuple[ConnectionEvidence, ...]
    confidence: float
    residual: ConnectionResidual | None = None

    @property
    def occurrence_ids(self) -> tuple[int, int]:
        """Return sorted occurrence IDs joined by this edge."""
        return (
            min(self.first.occurrence_id, self.second.occurrence_id),
            max(self.first.occurrence_id, self.second.occurrence_id),
        )


@dataclass(frozen=True, slots=True)
class OccurrenceCoverage:
    """Connector metadata coverage for one occurrence."""

    occurrence_id: int
    level: CoverageLevel
    endpoint_count: int
    geometry_complete: bool
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectionAnalysis:
    """Confirmed and optimistic topology for one materialized model."""

    occurrence_count: int
    endpoints: tuple[ConnectionEndpoint, ...]
    connections: tuple[ConnectorEdge, ...]
    unmatched_endpoints: tuple[str, ...]
    coverage: tuple[OccurrenceCoverage, ...]
    confirmed_components: tuple[tuple[int, ...], ...]
    optimistic_components: tuple[tuple[int, ...], ...]
    diagnostics: tuple[ConnectionDiagnostic, ...] = ()

    @property
    def confirmed_connections(self) -> tuple[ConnectorEdge, ...]:
        """Return authoritative edges only."""
        return tuple(
            edge
            for edge in self.connections
            if edge.status is ConnectionStatus.CONFIRMED
        )

    @property
    def potential_connections(self) -> tuple[ConnectorEdge, ...]:
        """Return inferential edges only."""
        return tuple(
            edge
            for edge in self.connections
            if edge.status is ConnectionStatus.POTENTIAL
        )

    @property
    def confirmed_component_count(self) -> int:
        """Return the pessimistic component count."""
        return len(self.confirmed_components)

    @property
    def optimistic_component_count(self) -> int:
        """Return the component count if every potential edge exists."""
        return len(self.optimistic_components)

    @property
    def component_interval(self) -> tuple[int, int]:
        """Return ``(minimum_possible, confirmed)`` component counts."""
        return (self.optimistic_component_count, self.confirmed_component_count)

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-safe projection."""
        return {
            "occurrence_count": self.occurrence_count,
            "endpoints": [_endpoint_payload(item) for item in self.endpoints],
            "connections": [_edge_payload(item) for item in self.connections],
            "unmatched_endpoints": list(self.unmatched_endpoints),
            "coverage": [
                {
                    "occurrence_id": item.occurrence_id,
                    "level": item.level.value,
                    "endpoint_count": item.endpoint_count,
                    "geometry_complete": item.geometry_complete,
                    "reason": item.reason,
                }
                for item in self.coverage
            ],
            "confirmed_components": [list(item) for item in self.confirmed_components],
            "optimistic_components": [
                list(item) for item in self.optimistic_components
            ],
            "diagnostics": [
                {
                    "code": item.code,
                    "message": item.message,
                    "provider": item.provider,
                    "part_id": item.part_id,
                    "connector_id": item.connector_id,
                    "source": item.source,
                }
                for item in self.diagnostics
            ],
        }


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


def analyze_connections(
    inspection: ModelInspection,
    *,
    registry: ConnectorRegistry,
    config: ConnectionConfig | None = None,
) -> ConnectionAnalysis:
    """Build confirmed and optimistic topology from typed contact evidence."""
    # One deterministic pipeline composes endpoint, edge, and component phases.
    # lizard forgives(cyclomatic_complexity)
    config = config or ConnectionConfig()
    diagnostics = list(registry.diagnostics)
    endpoints: list[ConnectionEndpoint] = []
    by_feature: dict[int, ConnectionEndpoint] = {}
    endpoints_by_occurrence: dict[int, list[ConnectionEndpoint]] = {
        item: [] for item in range(1, inspection.occurrence_count + 1)
    }
    geometry_by_id = {item.index + 1: item for item in inspection.occurrences}
    for geometry in inspection.occurrences:
        occurrence_id = geometry.index + 1
        for feature, endpoint in _feature_endpoints(geometry):
            by_feature[id(feature)] = endpoint
            endpoints.append(endpoint)
            endpoints_by_occurrence[occurrence_id].append(endpoint)
        for endpoint in _catalog_endpoints(geometry, registry):
            endpoints.append(endpoint)
            endpoints_by_occurrence[occurrence_id].append(endpoint)
    edges = list(_contact_edges(inspection, by_feature=by_feature, config=config))
    edges.extend(_catalog_edges(endpoints, config=config))
    if config.infer_surface_contacts:
        edges.extend(
            _surface_edges(
                inspection.occurrences,
                existing_pairs={edge.occurrence_ids for edge in edges},
                tolerance=config.face_tolerance_ldu,
            ),
        )
    edges.sort(key=lambda edge: edge.edge_id)
    matched = {endpoint.key for edge in edges for endpoint in (edge.first, edge.second)}
    endpoints.sort(key=lambda endpoint: endpoint.key)
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
    for edge in edges:
        left, right = edge.occurrence_ids
        optimistic.union(left, right)
        if edge.status is ConnectionStatus.CONFIRMED:
            confirmed.union(left, right)
    diagnostics.extend(
        ConnectionDiagnostic(
            code="geometry.unresolved_occurrence",
            message=item.reason,
            part_id=normalize_part_id(item.attribution.occurrence.part_code),
            source=(
                item.attribution.model_path[-1] if item.attribution.model_path else None
            ),
        )
        for item in inspection.skipped_geometry
    )
    return ConnectionAnalysis(
        occurrence_count=inspection.occurrence_count,
        endpoints=tuple(endpoints),
        connections=tuple(edges),
        unmatched_endpoints=tuple(
            endpoint.key for endpoint in endpoints if endpoint.key not in matched
        ),
        coverage=coverage,
        confirmed_components=confirmed.components(),
        optimistic_components=optimistic.components(),
        diagnostics=tuple(diagnostics),
    )


def _feature_endpoints(
    geometry: OccurrenceGeometry,
) -> tuple[tuple[ConnectionFeature, ConnectionEndpoint], ...]:
    part_id = normalize_part_id(geometry.occurrence.part_code)
    occurrence_id = geometry.index + 1
    pairs: list[tuple[ConnectionFeature, ConnectionEndpoint]] = []
    for index, feature in enumerate(geometry.connections):
        if abs(feature.axis) == 0 or feature.confidence <= 0:
            continue
        kind = feature.kind.value
        evidence = ConnectionEvidence(
            provider=f"pyldraw3:{feature.source.value}",
            strength=_strength_of(feature.source),
            detail=(f"{kind} feature{f' {feature.name}' if feature.name else ''}"),
            source=" > ".join(feature.provenance) or None,
        )
        pairs.append(
            (
                feature,
                ConnectionEndpoint(
                    key=(f"{occurrence_id}:{index}:{feature.feature_id or kind}"),
                    occurrence_id=occurrence_id,
                    part_id=part_id,
                    kind=kind,
                    role=feature.role,
                    point_ldu=feature.position,
                    axis=feature.axis.normalized(),
                    degrees_of_freedom=_feature_dof(feature),
                    capacity=(
                        STUD_CAPACITY if kind in _STUD_KINDS else ConnectionCapacity()
                    ),
                    evidence=(evidence,),
                    confidence=feature.confidence,
                ),
            ),
        )
    return tuple(pairs)


def _catalog_endpoints(
    geometry: OccurrenceGeometry,
    registry: ConnectorRegistry,
) -> tuple[ConnectionEndpoint, ...]:
    metadata = registry.get(geometry.occurrence.part_code)
    if metadata is None:
        return ()
    occurrence = geometry.occurrence
    occurrence_id = geometry.index + 1
    return tuple(
        ConnectionEndpoint(
            key=f"{occurrence_id}:catalog:{record.connector_id}",
            occurrence_id=occurrence_id,
            part_id=metadata.part_id,
            kind=record.kind,
            role=record.role,
            point_ldu=occurrence.position + occurrence.matrix * record.point_ldu,
            axis=(occurrence.matrix * record.axis).normalized(),
            degrees_of_freedom=record.degrees_of_freedom,
            capacity=record.capacity,
            evidence=record.evidence,
            confidence=record.confidence,
            compatible_kinds=record.compatible_kinds,
            catalog=True,
        )
        for record in metadata.connectors
    )


def _contact_edges(
    inspection: ModelInspection,
    *,
    by_feature: dict[int, ConnectionEndpoint],
    config: ConnectionConfig,
) -> tuple[ConnectorEdge, ...]:
    angular_tolerance = math.degrees(math.acos(config.axis_alignment))
    edges: list[ConnectorEdge] = []
    for contact in inspection.connection_contacts(
        tolerance=config.point_tolerance_ldu,
        angular_tolerance=angular_tolerance,
    ):
        left = by_feature.get(id(contact.first))
        right = by_feature.get(id(contact.second))
        if left is None or right is None:
            continue
        residual = contact.residual
        edges.append(
            _edge(
                left,
                right,
                status=contact.status,
                detail=(
                    "pyldraw3 profile match "
                    f"(distance={residual.distance:.4g}, "
                    f"axial_gap={residual.axial_gap:.4g})"
                ),
                residual=residual,
            ),
        )
    return tuple(edges)


def _catalog_edges(
    endpoints: list[ConnectionEndpoint],
    *,
    config: ConnectionConfig,
) -> tuple[ConnectorEdge, ...]:
    catalog = [endpoint for endpoint in endpoints if endpoint.catalog]
    edges: list[ConnectorEdge] = []
    seen: set[frozenset[str]] = set()
    for left in catalog:
        for right in endpoints:
            if right.occurrence_id == left.occurrence_id:
                continue
            pair = frozenset((left.key, right.key))
            if pair in seen:
                continue
            if abs(left.point_ldu - right.point_ldu) > config.point_tolerance_ldu:
                continue
            if not left.accepts(right):
                continue
            if left.axis.dot(right.axis) > -config.axis_alignment:
                continue
            seen.add(pair)
            edges.append(
                _edge(
                    left,
                    right,
                    status=_evidence_status(left, right),
                    detail="matched connector records",
                ),
            )
    return tuple(edges)


def _surface_edges(
    occurrences: tuple[OccurrenceGeometry, ...],
    *,
    existing_pairs: set[tuple[int, int]],
    tolerance: float,
) -> tuple[ConnectorEdge, ...]:
    edges: list[ConnectorEdge] = []
    for left, right in combinations(occurrences, 2):
        pair = (left.index + 1, right.index + 1)
        if pair in existing_pairs:
            continue
        if (contact := _face_contact(left.bounds, right.bounds, tolerance)) is None:
            continue
        point, axis = contact
        first = _surface_endpoint(left, point=point, axis=axis)
        second = _surface_endpoint(right, point=point, axis=-1 * axis)
        edges.append(
            _edge(
                first,
                second,
                status=ConnectionStatus.POTENTIAL,
                detail="inferred surface contact",
            ),
        )
    return tuple(edges)


def _surface_endpoint(
    geometry: OccurrenceGeometry,
    *,
    point: Vector,
    axis: Vector,
) -> ConnectionEndpoint:
    return ConnectionEndpoint(
        key=(f"{geometry.index + 1}:surface:{point.x:.3f}:{point.y:.3f}:{point.z:.3f}"),
        occurrence_id=geometry.index + 1,
        part_id=normalize_part_id(geometry.occurrence.part_code),
        kind=SURFACE_CONTACT_KIND,
        role=ConnectionRole.NEUTRAL,
        point_ldu=point,
        axis=axis,
        degrees_of_freedom=DegreesOfFreedom(
            translations=(True, True, False),
            rotations=(True, True, True),
        ),
        capacity=ConnectionCapacity(),
        evidence=(
            ConnectionEvidence(
                provider="geometry-inference",
                strength=EvidenceStrength.INFERRED,
                detail="opposed AABB faces touch with positive overlap area",
            ),
        ),
        confidence=0.5,
    )


def _edge(
    left: ConnectionEndpoint,
    right: ConnectionEndpoint,
    *,
    status: ConnectionStatus,
    detail: str,
    residual: ConnectionResidual | None = None,
) -> ConnectorEdge:
    first, second = _ordered(left, right)
    evidence = (
        *first.evidence,
        *second.evidence,
        ConnectionEvidence(
            provider="legolization",
            strength=(
                EvidenceStrength.EXACT
                if status is ConnectionStatus.CONFIRMED
                else EvidenceStrength.INFERRED
            ),
            detail=detail,
        ),
    )
    return ConnectorEdge(
        edge_id=f"{first.key}<->{second.key}",
        first=first,
        second=second,
        kind=_edge_kind(first, second),
        status=status,
        point_ldu=0.5 * (first.point_ldu + second.point_ldu),
        axis=first.axis,
        degrees_of_freedom=_combined_dof(first, second),
        capacity=_combined_capacity(first.capacity, second.capacity),
        evidence=evidence,
        confidence=min(first.confidence, second.confidence),
        residual=residual,
    )


def _ordered(
    left: ConnectionEndpoint,
    right: ConnectionEndpoint,
) -> tuple[ConnectionEndpoint, ConnectionEndpoint]:
    """Pin the male endpoint first so axial force signs never flip."""
    left_male = left.role is ConnectionRole.MALE
    right_male = right.role is ConnectionRole.MALE
    if left_male and not right_male:
        return left, right
    if right_male and not left_male:
        return right, left
    return (left, right) if left.key <= right.key else (right, left)


def _edge_kind(first: ConnectionEndpoint, second: ConnectionEndpoint) -> str:
    return _EDGE_KINDS.get(frozenset({first.kind, second.kind}), first.kind)


def _evidence_status(
    left: ConnectionEndpoint,
    right: ConnectionEndpoint,
) -> ConnectionStatus:
    strengths = {
        evidence.strength
        for endpoint in (left, right)
        for evidence in endpoint.evidence
    }
    return (
        ConnectionStatus.POTENTIAL
        if EvidenceStrength.INFERRED in strengths
        else ConnectionStatus.CONFIRMED
    )


def _feature_dof(feature: ConnectionFeature) -> DegreesOfFreedom:
    slide = ConnectionFreedom.SLIDE in feature.freedoms
    rotate = bool(
        {
            ConnectionFreedom.ROTATE,
            ConnectionFreedom.DISCRETE_ROTATE,
            ConnectionFreedom.FREE_ROTATE,
        }
        & feature.freedoms,
    )
    return DegreesOfFreedom(
        translations=(False, False, slide),
        rotations=(False, False, rotate),
    )


def _strength_of(source: ConnectionSource) -> EvidenceStrength:
    match source:
        case ConnectionSource.HEURISTIC:
            return EvidenceStrength.INFERRED
        case (
            ConnectionSource.LDCAD_INLINE
            | ConnectionSource.LDCAD_SHADOW
            | ConnectionSource.STUDIO
        ):
            return EvidenceStrength.IMPORTED
        case ConnectionSource.OVERRIDE:
            return EvidenceStrength.CURATED
        case ConnectionSource.PRIMITIVE | ConnectionSource.SHORTCUT:
            return EvidenceStrength.EXACT
        case _:
            return EvidenceStrength.UNKNOWN


def _combined_dof(
    left: ConnectionEndpoint,
    right: ConnectionEndpoint,
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


def _coverage(
    occurrence_id: int,
    *,
    geometry: OccurrenceGeometry | None,
    endpoints: list[ConnectionEndpoint],
    registry: ConnectorRegistry,
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
        reason = "only inferred connection features are available"
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


def _face_contact(
    left: BoundingBox,
    right: BoundingBox,
    tolerance: float,
) -> tuple[Vector, Vector] | None:
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
        return Vector(*values), Vector(*axis)
    return None


def _endpoint_payload(endpoint: ConnectionEndpoint) -> dict[str, Any]:
    return {
        "key": endpoint.key,
        "occurrence_id": endpoint.occurrence_id,
        "part_id": endpoint.part_id,
        "kind": endpoint.kind,
        "role": endpoint.role.value,
        "point_ldu": _vector_payload(endpoint.point_ldu),
        "axis": _vector_payload(endpoint.axis),
        "confidence": endpoint.confidence,
        "catalog": endpoint.catalog,
    }


def _edge_payload(edge: ConnectorEdge) -> dict[str, Any]:
    return {
        "id": edge.edge_id,
        "occurrence_ids": list(edge.occurrence_ids),
        "endpoints": [edge.first.key, edge.second.key],
        "kind": edge.kind,
        "status": edge.status.value,
        "point_ldu": _vector_payload(edge.point_ldu),
        "axis": _vector_payload(edge.axis),
        "degrees_of_freedom": {
            "translations": list(edge.degrees_of_freedom.translations),
            "rotations": list(edge.degrees_of_freedom.rotations),
        },
        "capacity": {
            "pull_n": edge.capacity.pull_n,
            "compression_n": edge.capacity.compression_n,
            "shear_n": edge.capacity.shear_n,
            "torque_nm": edge.capacity.torque_nm,
            "friction_coefficient": edge.capacity.friction_coefficient,
        },
        "confidence": edge.confidence,
        "evidence": [
            {
                "provider": item.provider,
                "strength": item.strength.value,
                "detail": item.detail,
                "source": item.source,
            }
            for item in edge.evidence
        ],
    }


def _vector_payload(value: Vector) -> list[float]:
    return [float(value.x), float(value.y), float(value.z)]
