"""Connector-catalog registry: physical part metadata pyldraw3 does not carry.

pyldraw3 1.6 owns connection-feature inference and metadata (LDCad shadows,
Studio exports, overrides), but has no notion of mass, centre of mass,
inertia, collision proxies, region tags, or force capacities. Those live
here: a builtin curated table plus optional user schema-1 JSON catalogs,
merged deterministically by priority.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from jsonschema import Draft202012Validator
from ldraw import ConnectionRole
from ldraw.geometry import Vector

if TYPE_CHECKING:
    from collections.abc import Iterable

REGISTRY_SCHEMA = 1
CONNECTOR_CATALOG_SCHEMA_PATH = (
    Path(__file__).parent / "data" / "connector-catalog-v1.schema.json"
)
_BUILTIN_SOURCE = "legolization:builtin-connectors"
_ROLES = {
    "male": ConnectionRole.MALE,
    "female": ConnectionRole.FEMALE,
    "bidirectional": ConnectionRole.NEUTRAL,
}


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
class ConnectionCapacity:
    """Directional connection limits; ``None`` means genuinely unknown."""

    pull_n: float | None = None
    compression_n: float | None = None
    shear_n: float | None = None
    torque_nm: float | None = None
    friction_coefficient: float | None = None

    def __post_init__(self) -> None:
        for value in (
            self.pull_n,
            self.compression_n,
            self.shear_n,
            self.torque_nm,
            self.friction_coefficient,
        ):
            if value is not None and (not math.isfinite(value) or value < 0):
                msg = "connection capacities must be finite and non-negative"
                raise ValueError(msg)


# StableLego's measured 100 g per-contact capacity remains the conservative
# project default. Torque is transmitted by separated studs, not one stud.
STUD_CAPACITY = ConnectionCapacity(
    pull_n=0.98,
    compression_n=0.98,
    shear_n=0.98,
    torque_nm=0.0,
    friction_coefficient=1.0,
)


@dataclass(frozen=True, slots=True)
class DegreesOfFreedom:
    """Allowed endpoint-relative translations and rotations in local axes."""

    translations: tuple[bool, bool, bool] = (False, False, False)
    rotations: tuple[bool, bool, bool] = (False, False, False)


@dataclass(frozen=True, slots=True)
class ConnectionEvidence:
    """One source supporting an endpoint or connection conclusion."""

    provider: str
    strength: EvidenceStrength
    detail: str
    source: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectionDiagnostic:
    """Non-fatal normalized registry or topology diagnostic."""

    code: str
    message: str
    provider: str | None = None
    part_id: str | None = None
    connector_id: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectorRecord:
    """One part-local connector declaration from a catalog."""

    connector_id: str
    kind: str
    role: ConnectionRole
    point_ldu: Vector
    axis: Vector
    degrees_of_freedom: DegreesOfFreedom = DegreesOfFreedom()
    compatible_kinds: tuple[str, ...] = ()
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

    def accepts(self, other: ConnectorRecord) -> bool:
        """Return whether kind and endpoint roles can mate."""
        compatible = self.compatible_kinds or (self.kind,)
        other_compatible = other.compatible_kinds or (other.kind,)
        return (
            other.kind in compatible
            and self.kind in other_compatible
            and roles_compatible(self.role, other.role)
        )


@dataclass(frozen=True, slots=True)
class PartRecord:
    """Connectivity and optional physical metadata for one LDraw part."""

    part_id: str
    connectors: tuple[ConnectorRecord, ...] = ()
    mass_g: float | None = None
    center_of_mass_ldu: Vector | None = None
    inertia_g_ldu2: Vector | None = None
    collision_proxy: tuple[Vector, Vector] | None = None
    tags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RegistryFragment:
    """One precedence-ordered catalog contribution."""

    provider: str
    priority: int
    parts: tuple[PartRecord, ...]
    diagnostics: tuple[ConnectionDiagnostic, ...] = ()
    source: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectorRegistry:
    """Merged part metadata indexed by normalized part reference."""

    parts: dict[str, PartRecord] = field(default_factory=dict)
    diagnostics: tuple[ConnectionDiagnostic, ...] = ()

    def get(self, part_id: str) -> PartRecord | None:
        """Return metadata using case- and suffix-insensitive lookup."""
        return self.parts.get(normalize_part_id(part_id))


def roles_compatible(left: ConnectionRole, right: ConnectionRole) -> bool:
    """Return whether two mating roles can pair (neutral mates anything)."""
    return (
        left is ConnectionRole.NEUTRAL
        or right is ConnectionRole.NEUTRAL
        or {left, right} == {ConnectionRole.MALE, ConnectionRole.FEMALE}
    )


def normalize_part_id(value: str) -> str:
    """Normalize LDraw part references for registry lookup."""
    normalized = value.replace("\\", "/").rsplit("/", maxsplit=1)[-1].casefold()
    return normalized.removesuffix(".dat").removesuffix(".ldr")


@lru_cache(maxsize=1)
def _catalog_json_schema() -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        json.loads(CONNECTOR_CATALOG_SCHEMA_PATH.read_text(encoding="utf-8")),
    )


def load_connector_catalog(path: Path, *, priority: int = 50) -> RegistryFragment:
    """Load and validate one schema-1 native JSON connector catalog."""
    document = json.loads(path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(_catalog_json_schema()).iter_errors(document),
        key=lambda error: tuple(str(item) for item in error.absolute_path),
    )
    if errors:
        first = errors[0]
        location = ".".join(str(item) for item in first.absolute_path) or "catalog"
        msg = f"{path}: invalid connector catalog at {location}: {first.message}"
        raise ValueError(msg)
    parts = tuple(_part_record(row, source=path) for row in document.get("parts", []))
    return RegistryFragment(
        provider="native",
        priority=priority,
        parts=parts,
        source=str(path),
    )


def merge_registries(fragments: Iterable[RegistryFragment]) -> ConnectorRegistry:
    """Merge fragments from low to high priority with conflict diagnostics."""
    parts: dict[str, PartRecord] = {}
    owners: dict[tuple[str, str], str] = {}
    diagnostics: list[ConnectionDiagnostic] = []
    for fragment in sorted(
        fragments,
        key=lambda item: (item.priority, item.provider, item.source or ""),
    ):
        diagnostics.extend(fragment.diagnostics)
        for incoming in fragment.parts:
            part_id = normalize_part_id(incoming.part_id)
            if (existing := parts.get(part_id)) is None:
                parts[part_id] = replace(incoming, part_id=part_id)
                for connector in incoming.connectors:
                    owners[(part_id, connector.connector_id)] = fragment.provider
                continue
            by_id = {item.connector_id: item for item in existing.connectors}
            for connector in incoming.connectors:
                key = (part_id, connector.connector_id)
                if connector.connector_id in by_id:
                    diagnostics.append(
                        ConnectionDiagnostic(
                            code="registry.connector_override",
                            message=(
                                f"{fragment.provider} overrides connector "
                                f"{part_id}:{connector.connector_id} from "
                                f"{owners.get(key, 'an earlier provider')}"
                            ),
                            provider=fragment.provider,
                            part_id=part_id,
                            connector_id=connector.connector_id,
                            source=fragment.source,
                        ),
                    )
                by_id[connector.connector_id] = connector
                owners[key] = fragment.provider
            parts[part_id] = PartRecord(
                part_id=part_id,
                connectors=tuple(by_id[key] for key in sorted(by_id)),
                mass_g=(
                    incoming.mass_g if incoming.mass_g is not None else existing.mass_g
                ),
                center_of_mass_ldu=(
                    incoming.center_of_mass_ldu or existing.center_of_mass_ldu
                ),
                inertia_g_ldu2=incoming.inertia_g_ldu2 or existing.inertia_g_ldu2,
                collision_proxy=incoming.collision_proxy or existing.collision_proxy,
                tags=existing.tags | incoming.tags,
            )
    return ConnectorRegistry(parts=parts, diagnostics=tuple(diagnostics))


def build_registry(catalog_paths: Iterable[Path] = ()) -> ConnectorRegistry:
    """Merge the builtin table with user catalogs in registration order."""
    return merge_registries(
        (
            builtin_registry(),
            *(
                load_connector_catalog(path, priority=50 + index)
                for index, path in enumerate(catalog_paths)
            ),
        ),
    )


def builtin_registry() -> RegistryFragment:
    """Return the curated builtin part table at builtin precedence."""
    tyre_rim = _builtin_connectors()
    tagged = {
        "18976": frozenset({"wheel", "rim"}),
        "18978b": frozenset({"wheel", "rim"}),
        "35578": frozenset({"wheel", "tyre"}),
        "3700": frozenset({"technic", "chassis"}),
        "3707": frozenset({"axle", "chassis"}),
        "3937": frozenset({"hinge"}),
        "3938": frozenset({"hinge"}),
        "11476": frozenset({"clip"}),
        "15712": frozenset({"clip"}),
        "23443": frozenset({"bar"}),
        "32828": frozenset({"bar"}),
        "37762": frozenset({"bar"}),
        "44861": frozenset({"clip"}),
        "48729b": frozenset({"bar", "clip"}),
        "60470b": frozenset({"clip"}),
        "61252": frozenset({"clip"}),
        "63868": frozenset({"clip"}),
        "78258": frozenset({"bar"}),
        "79194": frozenset({"bar"}),
        "88072": frozenset({"bar"}),
    }
    return RegistryFragment(
        provider="builtin",
        priority=20,
        parts=tuple(
            PartRecord(
                part_id=part_id,
                connectors=tyre_rim.get(part_id, ()),
                tags=tags,
            )
            for part_id, tags in tagged.items()
        ),
        source=_BUILTIN_SOURCE,
    )


def _builtin_connectors() -> dict[str, tuple[ConnectorRecord, ...]]:
    def connector(
        part_id: str,
        connector_id: str,
        role: ConnectionRole,
        point: tuple[float, float, float],
        axis: tuple[float, float, float],
    ) -> ConnectorRecord:
        return ConnectorRecord(
            connector_id=connector_id,
            kind="tyre_rim",
            role=role,
            point_ldu=Vector(*point),
            axis=Vector(*axis),
            evidence=(
                ConnectionEvidence(
                    provider="builtin",
                    strength=EvidenceStrength.CURATED,
                    detail=f"builtin connector {part_id}:{connector_id}",
                    source=_BUILTIN_SOURCE,
                ),
            ),
        )

    return {
        "18976": (
            connector("18976", "tyre", ConnectionRole.MALE, (0, 0, 0), (1, 0, 0)),
            connector(
                "18976", "outer-rim", ConnectionRole.MALE, (0, 0, -4), (0, 0, -1)
            ),
        ),
        "18978b": (
            connector(
                "18978b", "inner-rim", ConnectionRole.FEMALE, (0, 0, 4), (0, 0, 1)
            ),
        ),
        "35578": (
            connector("35578", "rim", ConnectionRole.FEMALE, (0, 0, 0), (-1, 0, 0)),
        ),
    }


def _part_record(row: dict[str, Any], *, source: Path) -> PartRecord:
    return PartRecord(
        part_id=normalize_part_id(row["part_id"]),
        connectors=tuple(
            _connector_record(item, part_id=row["part_id"], source=source)
            for item in row.get("connectors", [])
        ),
        mass_g=_optional_float(row.get("mass_g")),
        center_of_mass_ldu=_optional_vector(row.get("center_of_mass_ldu")),
        inertia_g_ldu2=_optional_vector(row.get("inertia_g_ldu2")),
        collision_proxy=(
            (
                Vector(*row["collision_proxy"]["min"]),
                Vector(*row["collision_proxy"]["max"]),
            )
            if row.get("collision_proxy") is not None
            else None
        ),
        tags=frozenset(row.get("tags", [])),
    )


def _connector_record(
    row: dict[str, Any],
    *,
    part_id: str,
    source: Path,
) -> ConnectorRecord:
    capacity = row.get("capacity") or {}
    freedom = row.get("degrees_of_freedom") or {}
    return ConnectorRecord(
        connector_id=row["id"],
        kind=row["kind"],
        role=_ROLES[row.get("role", "bidirectional")],
        point_ldu=Vector(*row["point_ldu"]),
        axis=Vector(*row["axis"]),
        degrees_of_freedom=DegreesOfFreedom(
            translations=_triplet(freedom.get("translations")),
            rotations=_triplet(freedom.get("rotations")),
        ),
        compatible_kinds=tuple(row.get("compatible_kinds", [])),
        capacity=ConnectionCapacity(
            pull_n=_optional_float(capacity.get("pull_n")),
            compression_n=_optional_float(capacity.get("compression_n")),
            shear_n=_optional_float(capacity.get("shear_n")),
            torque_nm=_optional_float(capacity.get("torque_nm")),
            friction_coefficient=_optional_float(
                capacity.get("friction_coefficient"),
            ),
        ),
        evidence=(
            ConnectionEvidence(
                provider=str(row.get("provider", "native")),
                strength=EvidenceStrength.CURATED,
                detail=f"native registry connector {part_id}:{row['id']}",
                source=str(source),
            ),
        ),
        confidence=float(row.get("confidence", 1.0)),
        tags=frozenset(row.get("tags", [])),
    )


def _optional_float(value: float | None) -> float | None:
    return None if value is None else float(value)


def _optional_vector(value: list[float] | None) -> Vector | None:
    return None if value is None else Vector(*value)


def _triplet(value: list[bool] | None) -> tuple[bool, bool, bool]:
    if value is None:
        return (False, False, False)
    return (value[0], value[1], value[2])
