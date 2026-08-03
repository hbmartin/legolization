"""Versioned native connector registry and deterministic fragment merging."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pyldcad.models import (
    ConnectionCapacity,
    ConnectionEvidence,
    ConnectionKind,
    ConnectivityDiagnostic,
    ConnectorDefinition,
    ConnectorRole,
    DegreesOfFreedom,
    EvidenceStrength,
    Vector3,
)

REGISTRY_SCHEMA = 1


@dataclass(frozen=True, slots=True)
class PartMetadata:
    """Connectivity and optional physical metadata for one LDraw part."""

    part_id: str
    connectors: tuple[ConnectorDefinition, ...] = ()
    mass_g: float | None = None
    center_of_mass_ldu: Vector3 | None = None
    inertia_g_ldu2: Vector3 | None = None
    collision_proxy: tuple[Vector3, Vector3] | None = None
    tags: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class RegistryFragment:
    """One precedence-ordered provider contribution."""

    provider: str
    priority: int
    parts: tuple[PartMetadata, ...]
    diagnostics: tuple[ConnectivityDiagnostic, ...] = ()
    source: str | None = None


@dataclass(frozen=True, slots=True)
class ConnectivityRegistry:
    """Merged part metadata indexed by normalized part reference."""

    parts: dict[str, PartMetadata]
    diagnostics: tuple[ConnectivityDiagnostic, ...] = ()

    def get(self, part_id: str) -> PartMetadata | None:
        """Return metadata using case- and suffix-insensitive lookup."""
        return self.parts.get(normalize_part_id(part_id))


def normalize_part_id(value: str) -> str:
    """Normalize LDraw part references for registry lookup."""
    normalized = value.replace("\\", "/").rsplit("/", maxsplit=1)[-1].casefold()
    return normalized.removesuffix(".dat").removesuffix(".ldr")


def empty_registry() -> ConnectivityRegistry:
    """Return an empty registry."""
    return ConnectivityRegistry(parts={})


def load_native_fragment(path: Path, *, priority: int = 50) -> RegistryFragment:
    """Load and validate one schema-1 native JSON registry."""
    document = json.loads(path.read_text())
    if not isinstance(document, dict) or document.get("schema") != REGISTRY_SCHEMA:
        msg = f"{path}: expected pyLDCad registry schema {REGISTRY_SCHEMA}"
        raise ValueError(msg)
    rows = document.get("parts", [])
    if not isinstance(rows, list):
        msg = f"{path}: parts must be a list"
        raise TypeError(msg)
    parts = tuple(_parse_part(row, source=path) for row in rows)
    return RegistryFragment(
        provider="native",
        priority=priority,
        parts=parts,
        source=str(path),
    )


def merge_fragments(*fragments: RegistryFragment) -> ConnectivityRegistry:
    """Merge fragments from low to high priority with conflict diagnostics."""
    parts: dict[str, PartMetadata] = {}
    owners: dict[tuple[str, str], str] = {}
    diagnostics: list[ConnectivityDiagnostic] = []
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
                        ConnectivityDiagnostic(
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
                        )
                    )
                by_id[connector.connector_id] = connector
                owners[key] = fragment.provider
            parts[part_id] = PartMetadata(
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
    return ConnectivityRegistry(parts=parts, diagnostics=tuple(diagnostics))


def _parse_part(value: Any, *, source: Path) -> PartMetadata:
    if not isinstance(value, dict):
        msg = f"{source}: every part must be an object"
        raise TypeError(msg)
    part_id = normalize_part_id(_required_string(value, "part_id", source=source))
    connectors_value = value.get("connectors", [])
    if not isinstance(connectors_value, list):
        msg = f"{source}: connectors for {part_id} must be a list"
        raise TypeError(msg)
    return PartMetadata(
        part_id=part_id,
        connectors=tuple(
            _parse_connector(item, part_id=part_id, source=source)
            for item in connectors_value
        ),
        mass_g=_optional_number(value.get("mass_g"), label="mass_g", source=source),
        center_of_mass_ldu=_optional_vector(value.get("center_of_mass_ldu"), source),
        inertia_g_ldu2=_optional_vector(value.get("inertia_g_ldu2"), source),
        collision_proxy=_optional_proxy(value.get("collision_proxy"), source),
        tags=frozenset(_string_list(value.get("tags", []), source=source)),
    )


def _parse_connector(value: Any, *, part_id: str, source: Path) -> ConnectorDefinition:
    if not isinstance(value, dict):
        msg = f"{source}: connectors for {part_id} must be objects"
        raise TypeError(msg)
    connector_id = _required_string(value, "id", source=source)
    kind = ConnectionKind(_required_string(value, "kind", source=source))
    provider = str(value.get("provider", "native"))
    return ConnectorDefinition(
        connector_id=connector_id,
        kind=kind,
        role=ConnectorRole(str(value.get("role", "bidirectional"))),
        point_ldu=_vector(value.get("point_ldu"), label="point_ldu", source=source),
        axis=_vector(value.get("axis"), label="axis", source=source),
        degrees_of_freedom=_parse_dof(value.get("degrees_of_freedom"), source),
        compatible_kinds=tuple(
            ConnectionKind(item)
            for item in _string_list(value.get("compatible_kinds", []), source=source)
        ),
        capacity=_parse_capacity(value.get("capacity"), source),
        confidence=float(value.get("confidence", 1.0)),
        evidence=(
            ConnectionEvidence(
                provider=provider,
                strength=EvidenceStrength.CURATED,
                detail=f"native registry connector {part_id}:{connector_id}",
                source=str(source),
            ),
        ),
        tags=frozenset(_string_list(value.get("tags", []), source=source)),
    )


def _parse_dof(value: Any, source: Path) -> DegreesOfFreedom:
    if value is None:
        return DegreesOfFreedom()
    if not isinstance(value, dict):
        msg = f"{source}: degrees_of_freedom must be an object"
        raise TypeError(msg)
    return DegreesOfFreedom(
        translations=_bool_triplet(value.get("translations", [False] * 3), source),
        rotations=_bool_triplet(value.get("rotations", [False] * 3), source),
    )


def _parse_capacity(value: Any, source: Path) -> ConnectionCapacity:
    if value is None:
        return ConnectionCapacity()
    if not isinstance(value, dict):
        msg = f"{source}: capacity must be an object"
        raise TypeError(msg)
    return ConnectionCapacity(
        pull_n=_optional_number(value.get("pull_n"), label="pull_n", source=source),
        compression_n=_optional_number(
            value.get("compression_n"),
            label="compression_n",
            source=source,
        ),
        shear_n=_optional_number(value.get("shear_n"), label="shear_n", source=source),
        torque_nm=_optional_number(
            value.get("torque_nm"),
            label="torque_nm",
            source=source,
        ),
        friction_coefficient=_optional_number(
            value.get("friction_coefficient"),
            label="friction_coefficient",
            source=source,
        ),
    )


def _required_string(value: dict[str, Any], key: str, *, source: Path) -> str:
    candidate = value.get(key)
    if not isinstance(candidate, str) or not candidate.strip():
        msg = f"{source}: {key} must be a non-empty string"
        raise TypeError(msg)
    return candidate


def _string_list(value: Any, *, source: Path) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        msg = f"{source}: expected a list of strings"
        raise TypeError(msg)
    return value


def _optional_number(value: Any, *, label: str, source: Path) -> float | None:
    if value is None:
        return None
    if not isinstance(value, int | float):
        msg = f"{source}: {label} must be numeric"
        raise TypeError(msg)
    return float(value)


def _vector(value: Any, *, label: str, source: Path) -> Vector3:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(isinstance(item, int | float) for item in value)
    ):
        msg = f"{source}: {label} must be a numeric three-vector"
        raise TypeError(msg)
    return Vector3(*(float(item) for item in value))


def _optional_vector(value: Any, source: Path) -> Vector3 | None:
    return None if value is None else _vector(value, label="vector", source=source)


def _optional_proxy(value: Any, source: Path) -> tuple[Vector3, Vector3] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        msg = f"{source}: collision_proxy must be an object"
        raise TypeError(msg)
    return (
        _vector(value.get("min"), label="collision_proxy.min", source=source),
        _vector(value.get("max"), label="collision_proxy.max", source=source),
    )


def _bool_triplet(value: Any, source: Path) -> tuple[bool, bool, bool]:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(isinstance(item, bool) for item in value)
    ):
        msg = f"{source}: DOF values must be boolean three-vectors"
        raise TypeError(msg)
    return (value[0], value[1], value[2])

