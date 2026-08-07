"""Write the ``<key>-legolization-support`` bundle for one inferred part.

Layout::

    bundle.json               authoritative BundleRecord (quality "direct")
    catalog-extension.json    schema-2 explicit part spec, mass omitted
    draft-estimates.json      legolization.catalog-estimates/v1 sidecar
    sources.json              every source-ladder rung, fully cited
    validation.json           the five activation gates
    geometry/<id>.dat         copy of the resolved LDraw part file
    geometry/occupancy.json   inferred cells, connectors, and confidence

``resolve_catalog`` activates the bundle as an overlay only while
``validation.json`` shows every gate passed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from legolization.bundle.identity import BundleIdentity
from legolization.bundle.paths import numbered_sibling
from legolization.bundle.record import (
    BundleRecord,
    read_record,
    source_payload,
    versions_payload,
    write_record,
)
from legolization.catalog import (
    CATALOG_ESTIMATES_SCHEMA,
    SUPPORT_BUNDLE_SUFFIX,
    SUPPORT_ESTIMATES_FILENAME,
    SUPPORT_EXTENSION_FILENAME,
    SUPPORT_VALIDATION_FILENAME,
)
from legolization.catalog_infer.validate import (
    all_gates_passed,
    validate_extension,
    validation_payload,
)
from legolization.eval_artifacts import atomic_json

if TYPE_CHECKING:
    from legolization.catalog_infer.estimates import DraftEstimate
    from legolization.catalog_infer.geometry import InferredConnector, InferredGeometry
    from legolization.catalog_infer.sources import SourceReport
    from legolization.catalog_infer.validate import GateResult

OCCUPANCY_SCHEMA = "legolization.catalog-occupancy/v1"

_GEOMETRY_DIRNAME = "geometry"
_OCCUPANCY_FILENAME = "occupancy.json"


@dataclass(frozen=True, slots=True, kw_only=True)
class SupportBundle:
    """One written support bundle plus everything a caller reports."""

    directory: Path
    key: str
    part_id: str
    gates: tuple[GateResult, ...]
    estimate: DraftEstimate
    sources: SourceReport
    geometry: InferredGeometry
    artifacts: tuple[tuple[str, str], ...]
    """``(bundle-relative path, kind)`` for every written artifact."""

    warnings: tuple[str, ...]

    @property
    def validated(self) -> bool:
        """Whether every activation gate passed."""
        return all_gates_passed(self.gates)


def support_dir_name(key: str) -> str:
    """Return the support bundle directory name for one part key."""
    return f"{key}{SUPPORT_BUNDLE_SUFFIX}"


def resolve_support_dir(
    key: str,
    *,
    output: Path | None,
    identity_key: str,
) -> Path:
    """Pick the bundle directory, reusing only an identity-matched one."""
    parent = output if output is not None else Path.cwd()
    base = parent / support_dir_name(key)
    if not base.exists():
        return base
    record = read_record(base)
    if record is not None and record.identity.key() == identity_key:
        return base
    return numbered_sibling(base)


def infer_identity(part_id: str, key: str, dat_sha256: str) -> BundleIdentity:
    """Compute the bundle identity for one inference invocation."""
    from legolization.catalog import catalog_hash  # noqa: PLC0415
    from legolization.configuration import mapping_hash  # noqa: PLC0415
    from legolization.version import package_version  # noqa: PLC0415

    return BundleIdentity(
        input_sha256=dat_sha256,
        config_sha256=mapping_hash(
            {"command": "catalog infer", "part_id": part_id, "key": key}
        ),
        legolization_version=package_version(),
        catalog_sha256=catalog_hash(),
    )


def write_support_bundle(  # noqa: PLR0913 - the bundle's full input set
    directory: Path,
    *,
    key: str,
    geometry: InferredGeometry,
    estimate: DraftEstimate,
    sources: SourceReport,
    offline: bool,
    now: str | None = None,
) -> SupportBundle:
    """Write every bundle artifact, run the gates, and record the result."""
    # lizard forgives(parameter_count)
    from legolization.configuration import mapping_hash  # noqa: PLC0415

    stamp = now if now is not None else datetime.now(UTC).isoformat()
    directory.mkdir(parents=True, exist_ok=True)
    dat_bytes = geometry.source_path.read_bytes()
    dat_sha = hashlib.sha256(dat_bytes).hexdigest()

    extension_path = directory / SUPPORT_EXTENSION_FILENAME
    atomic_json(extension_path, extension_payload(geometry, key))
    sidecar_path = directory / SUPPORT_ESTIMATES_FILENAME
    atomic_json(sidecar_path, estimates_payload(estimate))
    atomic_json(directory / "sources.json", sources.to_dict())
    geometry_dir = directory / _GEOMETRY_DIRNAME
    geometry_dir.mkdir(exist_ok=True)
    (geometry_dir / f"{geometry.part_id}.dat").write_bytes(dat_bytes)
    atomic_json(geometry_dir / _OCCUPANCY_FILENAME, occupancy_payload(geometry))

    gates = validate_extension(extension_path, (sidecar_path,))
    atomic_json(
        directory / SUPPORT_VALIDATION_FILENAME,
        validation_payload(
            gates,
            extension=SUPPORT_EXTENSION_FILENAME,
            generated_at=stamp,
        ),
    )

    warnings = (*geometry.notes, *estimate.warnings)
    validated = all_gates_passed(gates)
    complete = validated and estimate.measured and geometry.confident
    invocation = {
        "command": "catalog infer",
        "part_id": geometry.part_id,
        "key": key,
        "offline": offline,
    }
    record = BundleRecord(
        identity=infer_identity(geometry.part_id, key, dat_sha),
        source=source_payload(
            geometry.source_path,
            bundle_dir=directory,
            sha256=dat_sha,
        ),
        configuration={"sha256": mapping_hash(invocation), "values": invocation},
        versions=versions_payload(),
        quality="direct",
        status="complete" if complete else "partial",
        exit_code=0 if complete else 3,
        warnings=list(warnings),
        verdicts={
            "validated": validated,
            "confident_geometry": geometry.confident,
            "estimate_method": estimate.method,
        },
    )
    artifacts = _record_artifacts(record, geometry)
    _record_stages(record, geometry=geometry, estimate=estimate, gates=gates)
    write_record(record, directory)
    return SupportBundle(
        directory=directory,
        key=key,
        part_id=geometry.part_id,
        gates=gates,
        estimate=estimate,
        sources=sources,
        geometry=geometry,
        artifacts=artifacts,
        warnings=warnings,
    )


def _record_artifacts(
    record: BundleRecord,
    geometry: InferredGeometry,
) -> tuple[tuple[str, str], ...]:
    entries = (
        (SUPPORT_EXTENSION_FILENAME, "geometry", "catalog-extension"),
        (SUPPORT_ESTIMATES_FILENAME, "estimates", "catalog-estimates"),
        ("sources.json", "sources", "catalog-sources"),
        (SUPPORT_VALIDATION_FILENAME, "validation", "catalog-validation"),
        (f"{_GEOMETRY_DIRNAME}/{geometry.part_id}.dat", "geometry", "ldraw-part"),
        (f"{_GEOMETRY_DIRNAME}/{_OCCUPANCY_FILENAME}", "geometry", "occupancy"),
    )
    for path, stage, kind in entries:
        record.record_artifact(path=path, stage=stage, kind=kind)
    return tuple((path, kind) for path, _, kind in entries)


def _record_stages(
    record: BundleRecord,
    *,
    geometry: InferredGeometry,
    estimate: DraftEstimate,
    gates: tuple[GateResult, ...],
) -> None:
    geometry_stage = record.stage("geometry")
    geometry_stage.status = "complete" if geometry.confident else "partial"
    geometry_stage.warnings = list(geometry.notes)
    geometry_stage.detail = {
        "confident": geometry.confident,
        "cell_count": geometry.cell_count,
        "height_plates": geometry.height_plates,
    }
    record.stage("sources").status = "complete"
    estimates_stage = record.stage("estimates")
    estimates_stage.status = "complete" if estimate.measured else "partial"
    estimates_stage.warnings = list(estimate.warnings)
    estimates_stage.detail = {
        "method": estimate.method,
        "mass_g": estimate.mass_g,
        "volumetric_mass_g": estimate.volumetric_mass_g,
    }
    validation_stage = record.stage("validation")
    validation_stage.status = "complete" if all_gates_passed(gates) else "partial"
    validation_stage.detail = {
        "gates": {gate.gate: gate.status for gate in gates},
    }


def extension_payload(geometry: InferredGeometry, key: str) -> dict[str, Any]:
    """Return the schema-2 explicit part document, ``mass_g`` omitted."""
    cells = [list(cell) for cell in sorted(geometry.occupied_cells)]
    spec: dict[str, Any] = {
        "key": key,
        "ldraw_part": geometry.part_id,
        "category": "special",
        "height_plates": geometry.height_plates,
        "occupied_cells": cells,
        "filled_cells": cells,
        "top_connectors": _connector_payload(geometry.top_connectors),
        "bottom_connectors": _connector_payload(geometry.bottom_connectors),
        "orientations": [0, 90, 180, 270],
        "origin_offset": list(geometry.origin_offset),
        "replaceable_geometry": False,
        "collision_boxes_ldu": [
            {"minimum": list(box.minimum), "maximum": list(box.maximum)}
            for box in geometry.collision_boxes
        ],
    }
    return {"schema": 2, "parts": [spec]}


def _connector_payload(
    connectors: tuple[InferredConnector, ...],
) -> list[dict[str, Any]]:
    return [
        {"cell": list(connector.cell), "direction": list(connector.direction)}
        for connector in connectors
    ]


def estimates_payload(estimate: DraftEstimate) -> dict[str, Any]:
    """Return the draft-estimates sidecar document."""
    return {
        "schema": CATALOG_ESTIMATES_SCHEMA,
        "estimates": [estimate.to_record().to_payload()],
    }


def occupancy_payload(geometry: InferredGeometry) -> dict[str, Any]:
    """Return the inference evidence stored beside the ``.dat`` copy."""
    return {
        "schema": OCCUPANCY_SCHEMA,
        "part_id": geometry.part_id,
        "height_plates": geometry.height_plates,
        "origin_offset": list(geometry.origin_offset),
        "occupied_cells": [list(cell) for cell in sorted(geometry.occupied_cells)],
        "cell_count": geometry.cell_count,
        "top_connectors": _evidence_payload(geometry.top_connectors),
        "bottom_connectors": _evidence_payload(geometry.bottom_connectors),
        "collision_boxes_ldu": [
            {"minimum": list(box.minimum), "maximum": list(box.maximum)}
            for box in geometry.collision_boxes
        ],
        "confident": geometry.confident,
        "requires_generic_geometry": not geometry.confident,
        "notes": list(geometry.notes),
    }


def _evidence_payload(
    connectors: tuple[InferredConnector, ...],
) -> list[dict[str, Any]]:
    return [
        {
            "cell": list(connector.cell),
            "direction": list(connector.direction),
            "confidence": connector.confidence,
            "basis": connector.basis,
        }
        for connector in connectors
    ]
