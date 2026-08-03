"""Connectivity metadata providers and adapter normalization."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from importlib.resources import as_file, files
from pathlib import Path
from typing import Any, Protocol

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
from pyldcad.registry import (
    PartMetadata,
    RegistryFragment,
    load_native_fragment,
    normalize_part_id,
)

_LDCAD_META = re.compile(r"^\s*0\s+!LDCAD\s+(SNAP_[A-Z]+)\s*(.*)$", re.IGNORECASE)
_LDCAD_FIELD = re.compile(r"\[([A-Za-z0-9_]+)=([^\]]*)\]")


class MetadataProvider(Protocol):
    """Protocol implemented by registry-file adapters."""

    def load(self, path: Path, *, priority: int) -> RegistryFragment:
        """Normalize one metadata source into a registry fragment."""


@dataclass(frozen=True, slots=True)
class NativeProvider:
    """Loader for pyLDCad's versioned native JSON schema."""

    def load(self, path: Path, *, priority: int) -> RegistryFragment:
        """Load a native registry fragment."""
        return load_native_fragment(path, priority=priority)


@dataclass(frozen=True, slots=True)
class LDCadProvider:
    """Best-effort normalizer for LDCad ``!LDCAD SNAP_*`` records."""

    def load(self, path: Path, *, priority: int) -> RegistryFragment:
        """Load a part file or directory of part files."""
        paths = tuple(sorted(path.rglob("*.dat"))) if path.is_dir() else (path,)
        parts: list[PartMetadata] = []
        diagnostics: list[ConnectivityDiagnostic] = []
        for candidate in paths:
            connectors, candidate_diagnostics = _load_ldcad_file(candidate)
            diagnostics.extend(candidate_diagnostics)
            if connectors:
                parts.append(
                    PartMetadata(
                        part_id=normalize_part_id(candidate.name),
                        connectors=tuple(connectors),
                    )
                )
        return RegistryFragment(
            provider="ldcad",
            priority=priority,
            parts=tuple(parts),
            diagnostics=tuple(diagnostics),
            source=str(path),
        )


@dataclass(frozen=True, slots=True)
class StudioProvider:
    """Normalizer for exported Studio connectivity JSON records."""

    def load(self, path: Path, *, priority: int) -> RegistryFragment:
        """Load a supported Studio export, retaining unsupported records."""
        diagnostics: list[ConnectivityDiagnostic] = []
        try:
            document = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as error:
            return RegistryFragment(
                provider="studio",
                priority=priority,
                parts=(),
                diagnostics=(
                    ConnectivityDiagnostic(
                        code="studio.unsupported_document",
                        message=f"could not read Studio connectivity JSON: {error}",
                        provider="studio",
                        source=str(path),
                    ),
                ),
                source=str(path),
            )
        rows = document.get("parts", []) if isinstance(document, dict) else []
        if not isinstance(rows, list):
            rows = []
            diagnostics.append(
                ConnectivityDiagnostic(
                    code="studio.unsupported_document",
                    message="Studio export does not contain a parts list",
                    provider="studio",
                    source=str(path),
                )
            )
        parts: list[PartMetadata] = []
        for row in rows:
            part = _studio_part(row, path=path, diagnostics=diagnostics)
            if part is not None:
                parts.append(part)
        return RegistryFragment(
            provider="studio",
            priority=priority,
            parts=tuple(parts),
            diagnostics=tuple(diagnostics),
            source=str(path),
        )


def builtin_fragment() -> RegistryFragment:
    """Load the bundled curated registry at built-in precedence."""
    resource = files("pyldcad.data").joinpath("builtin.json")
    with as_file(resource) as path:
        fragment = load_native_fragment(path, priority=20)
    return replace(fragment, provider="builtin", source="pyldcad:data/builtin.json")


def _load_ldcad_file(
    path: Path,
) -> tuple[list[ConnectorDefinition], list[ConnectivityDiagnostic]]:
    connectors: list[ConnectorDefinition] = []
    diagnostics: list[ConnectivityDiagnostic] = []
    for line_number, line in enumerate(
        path.read_text(errors="replace").splitlines(), 1
    ):
        if (match := _LDCAD_META.match(line)) is None:
            continue
        record_type, body = match.groups()
        fields_by_name = {
            key.casefold(): value.strip() for key, value in _LDCAD_FIELD.findall(body)
        }
        try:
            connector = _ldcad_connector(
                record_type.upper(),
                fields_by_name,
                index=len(connectors),
                source=f"{path}:{line_number}",
            )
        except (TypeError, ValueError) as error:
            diagnostics.append(
                ConnectivityDiagnostic(
                    code="ldcad.unsupported_record",
                    message=f"{record_type} record ignored: {error}",
                    provider="ldcad",
                    part_id=normalize_part_id(path.name),
                    source=f"{path}:{line_number}",
                )
            )
        else:
            connectors.append(connector)
    return connectors, diagnostics


def _ldcad_connector(
    record_type: str,
    values: dict[str, str],
    *,
    index: int,
    source: str,
) -> ConnectorDefinition:
    kind, dof = _ldcad_kind(record_type, values)
    position = _numbers(values.get("pos", "0 0 0"), count=3)
    orientation = _numbers(values.get("ori", "1 0 0 0 1 0 0 0 1"), count=9)
    axis = Vector3(-orientation[1], -orientation[4], -orientation[7])
    gender = values.get("gender", values.get("g", "b")).casefold()
    role = {
        "m": ConnectorRole.MALE,
        "male": ConnectorRole.MALE,
        "f": ConnectorRole.FEMALE,
        "female": ConnectorRole.FEMALE,
    }.get(gender, ConnectorRole.BIDIRECTIONAL)
    connector_id = values.get("id", f"{record_type.casefold()}:{index}")
    return ConnectorDefinition(
        connector_id=connector_id,
        kind=kind,
        role=role,
        point_ldu=Vector3(*position),
        axis=axis,
        degrees_of_freedom=dof,
        capacity=ConnectionCapacity(),
        confidence=0.95,
        evidence=(
            ConnectionEvidence(
                provider="ldcad",
                strength=EvidenceStrength.IMPORTED,
                detail=record_type,
                source=source,
            ),
        ),
    )


def _ldcad_kind(
    record_type: str,
    values: dict[str, str],
) -> tuple[ConnectionKind, DegreesOfFreedom]:
    match record_type:
        case "SNAP_CYL":
            subtype = f"{values.get('secs', '')} {values.get('type', '')}".casefold()
            kind = (
                ConnectionKind.AXLE
                if "axle" in subtype or "4" in subtype
                else ConnectionKind.TECHNIC_PIN
            )
            return kind, DegreesOfFreedom(
                translations=(False, False, True),
                rotations=(False, False, True),
            )
        case "SNAP_CLP" | "SNAP_FGR":
            return ConnectionKind.BAR_CLIP, DegreesOfFreedom(
                rotations=(False, False, True),
            )
        case "SNAP_GEN":
            return ConnectionKind("ldcad:generic"), DegreesOfFreedom()
        case _:
            msg = "record type is not supported"
            raise ValueError(msg)


def _studio_part(
    value: Any,
    *,
    path: Path,
    diagnostics: list[ConnectivityDiagnostic],
) -> PartMetadata | None:
    if not isinstance(value, dict) or not isinstance(value.get("part_id"), str):
        diagnostics.append(
            ConnectivityDiagnostic(
                code="studio.unsupported_part",
                message="Studio part record has no part_id",
                provider="studio",
                source=str(path),
            )
        )
        return None
    part_id = normalize_part_id(value["part_id"])
    rows = value.get("connections", [])
    connectors: list[ConnectorDefinition] = []
    if not isinstance(rows, list):
        rows = []
    for index, row in enumerate(rows):
        try:
            connectors.append(_studio_connector(row, index=index, source=str(path)))
        except (TypeError, ValueError) as error:
            diagnostics.append(
                ConnectivityDiagnostic(
                    code="studio.unsupported_connection",
                    message=f"Studio connection ignored: {error}",
                    provider="studio",
                    part_id=part_id,
                    source=str(path),
                )
            )
    return PartMetadata(
        part_id=part_id,
        connectors=tuple(connectors),
        mass_g=_json_number(value.get("mass_g")),
        tags=frozenset(item for item in value.get("tags", []) if isinstance(item, str)),
    )


def _studio_connector(value: Any, *, index: int, source: str) -> ConnectorDefinition:
    if not isinstance(value, dict):
        msg = "connection must be an object"
        raise TypeError(msg)
    kind = _studio_kind(str(value.get("type", "")))
    position = _json_vector(value.get("position"), label="position")
    axis = _json_vector(value.get("axis"), label="axis")
    role = ConnectorRole(str(value.get("gender", "bidirectional")).casefold())
    return ConnectorDefinition(
        connector_id=str(value.get("id", f"studio:{index}")),
        kind=kind,
        role=role,
        point_ldu=position,
        axis=axis,
        degrees_of_freedom=_default_dof(kind),
        confidence=0.95,
        evidence=(
            ConnectionEvidence(
                provider="studio",
                strength=EvidenceStrength.IMPORTED,
                detail=f"Studio {value.get('type', 'connection')}",
                source=source,
            ),
        ),
    )


def _studio_kind(value: str) -> ConnectionKind:
    normalized = value.strip().casefold().replace("-", "_")
    known = {
        "stud": ConnectionKind.STUD,
        "pin": ConnectionKind.TECHNIC_PIN,
        "technic_pin": ConnectionKind.TECHNIC_PIN,
        "axle": ConnectionKind.AXLE,
        "bar": ConnectionKind.BAR_CLIP,
        "clip": ConnectionKind.BAR_CLIP,
        "hinge": ConnectionKind.HINGE,
        "ball": ConnectionKind.BALL_JOINT,
        "ball_joint": ConnectionKind.BALL_JOINT,
        "turntable": ConnectionKind.TURNTABLE,
        "tyre_rim": ConnectionKind.TYRE_RIM,
    }
    if (kind := known.get(normalized)) is None:
        msg = f"unsupported connection type {value!r}"
        raise ValueError(msg)
    return kind


def _default_dof(kind: ConnectionKind) -> DegreesOfFreedom:
    if kind in {ConnectionKind.HINGE, ConnectionKind.TURNTABLE}:
        return DegreesOfFreedom(rotations=(False, False, True))
    if kind == ConnectionKind.BALL_JOINT:
        return DegreesOfFreedom(rotations=(True, True, True))
    if kind in {ConnectionKind.TECHNIC_PIN, ConnectionKind.AXLE}:
        return DegreesOfFreedom(
            translations=(False, False, True),
            rotations=(False, False, True),
        )
    return DegreesOfFreedom()


def _numbers(value: str, *, count: int) -> tuple[float, ...]:
    fields = value.replace(",", " ").split()
    if len(fields) != count:
        msg = f"expected {count} numeric values"
        raise ValueError(msg)
    return tuple(float(item) for item in fields)


def _json_vector(value: Any, *, label: str) -> Vector3:
    if (
        not isinstance(value, list)
        or len(value) != 3
        or not all(isinstance(item, int | float) for item in value)
    ):
        msg = f"{label} must be a numeric three-vector"
        raise TypeError(msg)
    return Vector3(*(float(item) for item in value))


def _json_number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) else None

