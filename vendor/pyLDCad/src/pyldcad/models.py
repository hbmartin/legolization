"""Immutable public value objects for LDraw connectivity analysis."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields, is_dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Self

if TYPE_CHECKING:
    from collections.abc import Mapping


class ConnectionKind(str):
    """Extensible namespaced connection identifier."""

    __slots__ = ()

    STUD: ClassVar[ConnectionKind]
    TECHNIC_PIN: ClassVar[ConnectionKind]
    AXLE: ClassVar[ConnectionKind]
    BAR_CLIP: ClassVar[ConnectionKind]
    HINGE: ClassVar[ConnectionKind]
    BALL_JOINT: ClassVar[ConnectionKind]
    TURNTABLE: ClassVar[ConnectionKind]
    TYRE_RIM: ClassVar[ConnectionKind]
    SURFACE_CONTACT: ClassVar[ConnectionKind]

    def __new__(cls, value: str) -> Self:
        """Validate and normalize one namespaced kind."""
        normalized = value.strip().casefold()
        if ":" not in normalized or normalized.startswith(":"):
            msg = "connection kinds must be non-empty namespaced identifiers"
            raise ValueError(msg)
        return str.__new__(cls, normalized)


ConnectionKind.STUD = ConnectionKind("ldraw:stud")
ConnectionKind.TECHNIC_PIN = ConnectionKind("technic:pin")
ConnectionKind.AXLE = ConnectionKind("technic:axle")
ConnectionKind.BAR_CLIP = ConnectionKind("ldraw:bar_clip")
ConnectionKind.HINGE = ConnectionKind("ldraw:hinge")
ConnectionKind.BALL_JOINT = ConnectionKind("ldraw:ball_joint")
ConnectionKind.TURNTABLE = ConnectionKind("ldraw:turntable")
ConnectionKind.TYRE_RIM = ConnectionKind("ldraw:tyre_rim")
ConnectionKind.SURFACE_CONTACT = ConnectionKind("geometry:surface_contact")


class ConnectorRole(StrEnum):
    """Mechanical role used to reject incompatible endpoint pairs."""

    MALE = "male"
    FEMALE = "female"
    BIDIRECTIONAL = "bidirectional"


class ConnectionStatus(StrEnum):
    """Whether an edge is authoritative or merely plausible."""

    CONFIRMED = "confirmed"
    POTENTIAL = "potential"


class EvidenceStrength(StrEnum):
    """Origin class for connection and metadata evidence."""

    EXACT = "exact"
    CURATED = "curated"
    IMPORTED = "imported"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class CoverageLevel(StrEnum):
    """Connector metadata coverage for one occurrence."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class Vector3:
    """A dependency-neutral three-dimensional vector in LDraw units."""

    x: float
    y: float
    z: float

    def __post_init__(self) -> None:
        if not all(math.isfinite(value) for value in (self.x, self.y, self.z)):
            msg = "vector components must be finite"
            raise ValueError(msg)

    @property
    def length(self) -> float:
        """Return the Euclidean vector length."""
        return math.sqrt(self.x**2 + self.y**2 + self.z**2)

    def normalized(self) -> Vector3:
        """Return a unit vector, rejecting a zero-length axis."""
        if (length := self.length) == 0:
            msg = "connector axis must be non-zero"
            raise ValueError(msg)
        return Vector3(self.x / length, self.y / length, self.z / length)

    def dot(self, other: Vector3) -> float:
        """Return the vector dot product."""
        return self.x * other.x + self.y * other.y + self.z * other.z

    def distance_to(self, other: Vector3) -> float:
        """Return the Euclidean distance to another point."""
        return Vector3(self.x - other.x, self.y - other.y, self.z - other.z).length

    def scaled(self, factor: float) -> Vector3:
        """Scale every vector component."""
        return Vector3(self.x * factor, self.y * factor, self.z * factor)

    def plus(self, other: Vector3) -> Vector3:
        """Add another vector."""
        return Vector3(self.x + other.x, self.y + other.y, self.z + other.z)

    def to_tuple(self) -> tuple[float, float, float]:
        """Return a stable tuple representation."""
        return (self.x, self.y, self.z)


@dataclass(frozen=True, slots=True)
class DegreesOfFreedom:
    """Allowed endpoint-relative translations and rotations in local axes."""

    translations: tuple[bool, bool, bool] = (False, False, False)
    rotations: tuple[bool, bool, bool] = (False, False, False)


@dataclass(frozen=True, slots=True)
class ConnectionCapacity:
    """Directional connection limits; ``None`` means genuinely unknown."""

    pull_n: float | None = None
    compression_n: float | None = None
    shear_n: float | None = None
    torque_nm: float | None = None
    friction_coefficient: float | None = None

    def __post_init__(self) -> None:
        for value in asdict(self).values():
            if value is not None and (not math.isfinite(value) or value < 0):
                msg = "connection capacities must be finite and non-negative"
                raise ValueError(msg)

    @property
    def complete(self) -> bool:
        """Whether every generic capacity dimension is known."""
        return all(value is not None for value in asdict(self).values())


@dataclass(frozen=True, slots=True)
class ConnectionEvidence:
    """One source supporting an endpoint or connection conclusion."""

    provider: str
    strength: EvidenceStrength
    detail: str
    source: str | None = None


@dataclass(frozen=True, slots=True)
class SourceReference:
    """Full source provenance for one flattened occurrence."""

    model_path: tuple[str, ...] = ()
    reference_path: tuple[str, ...] = ()
    source_line_path: tuple[int | None, ...] = ()
    local_step_path: tuple[int | None, ...] = ()
    effective_step_path: tuple[int | None, ...] = ()
    page_path: tuple[int | None, ...] = ()

    @property
    def source_model(self) -> str | None:
        """Return the model section directly containing the leaf placement."""
        return self.model_path[-1] if self.model_path else None

    @property
    def source_line(self) -> int | None:
        """Return the line directly placing the leaf part."""
        return self.source_line_path[-1] if self.source_line_path else None

    @property
    def source_page(self) -> int | None:
        """Return the page directly placing the leaf part."""
        return self.page_path[-1] if self.page_path else None


@dataclass(frozen=True, slots=True)
class ConnectorDefinition:
    """One part-local connector declaration."""

    connector_id: str
    kind: ConnectionKind
    role: ConnectorRole
    point_ldu: Vector3
    axis: Vector3
    degrees_of_freedom: DegreesOfFreedom = DegreesOfFreedom()
    compatible_kinds: tuple[ConnectionKind, ...] = ()
    capacity: ConnectionCapacity = ConnectionCapacity()
    evidence: tuple[ConnectionEvidence, ...] = ()
    confidence: float = 1.0
    tags: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not self.connector_id.strip():
            msg = "connector_id must be non-empty"
            raise ValueError(msg)
        if not 0 <= self.confidence <= 1:
            msg = "connector confidence must be between zero and one"
            raise ValueError(msg)
        object.__setattr__(self, "axis", self.axis.normalized())

    def accepts(self, other: ConnectorDefinition) -> bool:
        """Return whether kind and endpoint roles can mate."""
        compatible = self.compatible_kinds or (self.kind,)
        other_compatible = other.compatible_kinds or (other.kind,)
        return (
            other.kind in compatible
            and self.kind in other_compatible
            and _roles_compatible(self.role, other.role)
        )


@dataclass(frozen=True, slots=True)
class ConnectorEndpoint:
    """A connector definition transformed onto one model occurrence."""

    occurrence_id: int
    part_id: str
    connector_id: str
    kind: ConnectionKind
    role: ConnectorRole
    point_ldu: Vector3
    axis: Vector3
    degrees_of_freedom: DegreesOfFreedom
    compatible_kinds: tuple[ConnectionKind, ...]
    capacity: ConnectionCapacity
    evidence: tuple[ConnectionEvidence, ...]
    confidence: float
    source: SourceReference
    synthetic: bool = False

    def __post_init__(self) -> None:
        if self.occurrence_id < 1:
            msg = "public occurrence IDs are one-based"
            raise ValueError(msg)
        object.__setattr__(self, "axis", self.axis.normalized())

    @property
    def key(self) -> str:
        """Return a deterministic model-wide endpoint key."""
        return f"{self.occurrence_id}:{self.connector_id}"


@dataclass(frozen=True, slots=True)
class Connection:
    """One mechanical relationship between two occurrence endpoints."""

    connection_id: str
    endpoint_a: ConnectorEndpoint
    endpoint_b: ConnectorEndpoint
    point_ldu: Vector3
    axis: Vector3
    kind: ConnectionKind
    degrees_of_freedom: DegreesOfFreedom
    capacity: ConnectionCapacity
    evidence: tuple[ConnectionEvidence, ...]
    confidence: float
    status: ConnectionStatus

    def __post_init__(self) -> None:
        if self.endpoint_a.occurrence_id == self.endpoint_b.occurrence_id:
            msg = "a connection must join distinct occurrences"
            raise ValueError(msg)
        object.__setattr__(self, "axis", self.axis.normalized())

    @property
    def occurrence_ids(self) -> tuple[int, int]:
        """Return sorted occurrence IDs joined by this connection."""
        return (
            min(self.endpoint_a.occurrence_id, self.endpoint_b.occurrence_id),
            max(self.endpoint_a.occurrence_id, self.endpoint_b.occurrence_id),
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
class ConnectivityDiagnostic:
    """Non-fatal normalized metadata or topology diagnostic."""

    code: str
    message: str
    provider: str | None = None
    part_id: str | None = None
    connector_id: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectivityAnalysis:
    """Confirmed and optimistic topology for one materialized model."""

    occurrence_count: int
    endpoints: tuple[ConnectorEndpoint, ...]
    connections: tuple[Connection, ...]
    unmatched_endpoints: tuple[str, ...]
    coverage: tuple[OccurrenceCoverage, ...]
    confirmed_components: tuple[tuple[int, ...], ...]
    optimistic_components: tuple[tuple[int, ...], ...]
    diagnostics: tuple[ConnectivityDiagnostic, ...] = ()

    @property
    def confirmed_connections(self) -> tuple[Connection, ...]:
        """Return authoritative connections only."""
        return tuple(
            edge
            for edge in self.connections
            if edge.status is ConnectionStatus.CONFIRMED
        )

    @property
    def potential_connections(self) -> tuple[Connection, ...]:
        """Return inferential connections only."""
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
        return _json_value(self)


def _roles_compatible(left: ConnectorRole, right: ConnectorRole) -> bool:
    return (
        left is ConnectorRole.BIDIRECTIONAL
        or right is ConnectorRole.BIDIRECTIONAL
        or {left, right} == {ConnectorRole.MALE, ConnectorRole.FEMALE}
    )


def _json_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, ConnectionKind):
        return str(value)
    if isinstance(value, frozenset | set):
        return sorted(_json_value(item) for item in value)
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return value


def metadata_payload(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize arbitrary provider metadata for report serialization."""
    return {str(key): _json_value(value) for key, value in metadata.items()}

