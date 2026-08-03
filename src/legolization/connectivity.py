"""Normalization helpers for external connector-analysis evidence.

LDraw parts can describe the same physical clutch through overlapping
primitives (for example one broad ``stud4`` tube plus inferred per-stud
receptacles).  ``pyldcad`` deliberately reports all evidence.  The
legolization domain, however, reasons about physical connector instances, so
parallel primitive matches must be collapsed before computing capacities or
minimum cuts.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyldcad import (
        Connection,
        ConnectionEvidence,
        ConnectivityAnalysis,
        ConnectorEndpoint,
    )


def normalize_connectivity(raw: ConnectivityAnalysis) -> ConnectivityAnalysis:
    """Collapse duplicate evidence for the same physical connector instance.

    The male endpoint is the stable physical identity for knob connections:
    broad female primitives and inferred receptacles can both match it.  For
    connector families without a male/female distinction, the two endpoint
    positions form the identity instead.  Component membership is unchanged
    because normalization only removes parallel edges.
    """
    grouped: dict[tuple[object, ...], list[Connection]] = {}
    for connection in raw.connections:
        grouped.setdefault(_physical_key(connection), []).append(connection)

    normalized = tuple(
        _merge_connections(matches)
        for _, matches in sorted(grouped.items(), key=lambda item: repr(item[0]))
    )
    return replace(raw, connections=normalized)


def _physical_key(connection: Connection) -> tuple[object, ...]:
    endpoints = (connection.endpoint_a, connection.endpoint_b)
    male = next(
        (endpoint for endpoint in endpoints if _enum_value(endpoint.role) == "male"),
        None,
    )
    pair = tuple(sorted(endpoint.occurrence_id for endpoint in endpoints))
    if male is not None:
        return (
            pair,
            str(connection.kind),
            _point_key(male),
            _axis_key(male),
        )
    endpoint_keys = tuple(
        sorted((_point_key(endpoint), _axis_key(endpoint)) for endpoint in endpoints)
    )
    return (pair, str(connection.kind), endpoint_keys)


def _merge_connections(matches: list[Connection]) -> Connection:
    preferred = max(matches, key=_preference)
    evidence = _unique_evidence(
        evidence for connection in matches for evidence in connection.evidence
    )
    raw_ids = tuple(sorted(connection.connection_id for connection in matches))
    canonical_id = "physical:" + "|".join(raw_ids)
    return replace(preferred, connection_id=canonical_id, evidence=evidence)


def _preference(connection: Connection) -> tuple[int, float, str]:
    endpoints = (connection.endpoint_a, connection.endpoint_b)
    inferred = any(
        endpoint.connector_id.startswith("synthetic:receptacle")
        for endpoint in endpoints
    )
    confirmed = _enum_value(connection.status) == "confirmed"
    return (
        int(confirmed) + int(inferred),
        connection.confidence,
        connection.connection_id,
    )


def _unique_evidence(
    evidence_items: Iterable[ConnectionEvidence],
) -> tuple[ConnectionEvidence, ...]:
    unique: dict[tuple[str, str, str, str], ConnectionEvidence] = {}
    for evidence in evidence_items:
        key = (
            evidence.provider,
            _enum_value(evidence.strength),
            evidence.detail,
            repr(evidence.source),
        )
        unique.setdefault(key, evidence)
    return tuple(unique[key] for key in sorted(unique))


def _point_key(endpoint: ConnectorEndpoint) -> tuple[float, float, float]:
    point = endpoint.point_ldu
    return (round(point.x, 6), round(point.y, 6), round(point.z, 6))


def _axis_key(endpoint: ConnectorEndpoint) -> tuple[float, float, float]:
    axis = endpoint.axis
    values = (round(axis.x, 6), round(axis.y, 6), round(axis.z, 6))
    first_nonzero = next((value for value in values if value), 1.0)
    if first_nonzero < 0:
        return (-values[0], -values[1], -values[2])
    return values


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))
