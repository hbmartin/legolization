"""Geometry-first assembly representation built from pyldraw inspection."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from ldraw.geometry import Vector
from scipy.spatial import ConvexHull, QhullError

from legolization.assembly_registry import normalize_part_id

if TYPE_CHECKING:
    from ldraw import (
        ModelAnalysis,
        OccurrenceAttribution,
        OccurrenceGeometry,
        PartGeometry,
    )

    from legolization.assembly_registry import ConnectorRegistry
    from legolization.catalog import Catalog

_LDU_CM = 0.04
_ABS_DENSITY_G_CM3 = 1.04
_MIN_ESTIMATED_MASS_G = 0.01


@dataclass(frozen=True, slots=True)
class AssemblyBounds:
    """One occurrence's exact world axis-aligned bounds in LDU."""

    minimum: Vector
    maximum: Vector

    @property
    def size(self) -> Vector:
        """Return the three world extents."""
        return Vector(
            self.maximum.x - self.minimum.x,
            self.maximum.y - self.minimum.y,
            self.maximum.z - self.minimum.z,
        )

    @property
    def center(self) -> Vector:
        """Return the bounds center."""
        return Vector(
            (self.minimum.x + self.maximum.x) / 2,
            (self.minimum.y + self.maximum.y) / 2,
            (self.minimum.z + self.maximum.z) / 2,
        )


@dataclass(frozen=True, slots=True)
class AssemblyOccurrence:
    """One exact, source-addressable leaf occurrence in an LDraw assembly."""

    occurrence_id: int
    pyldraw_index: int
    part_id: str
    reference: str
    colour_code: int
    position_ldu: Vector
    matrix: tuple[float, float, float, float, float, float, float, float, float]
    geometry: PartGeometry
    bounds_ldu: AssemblyBounds
    mass_g: float | None
    mass_source: str
    center_of_mass_ldu: Vector | None
    inertia_g_ldu2: Vector | None
    tags: frozenset[str]
    source: OccurrenceAttribution


@dataclass(frozen=True, slots=True)
class AssemblyModel:
    """Tolerantly resolved arbitrary LDraw occurrences and diagnostics."""

    analysis: ModelAnalysis
    occurrences: tuple[AssemblyOccurrence, ...]
    skipped_occurrence_ids: tuple[int, ...]
    diagnostics: tuple[str, ...]

    @property
    def occurrence_count(self) -> int:
        """Return resolved plus skipped occurrence count."""
        return (
            self.analysis.inspection.occurrence_count if self.analysis.inspection else 0
        )

    @property
    def total_mass_g(self) -> float | None:
        """Return total mass when every occurrence has a usable value."""
        if any(item.mass_g is None for item in self.occurrences):
            return None
        return sum(item.mass_g or 0.0 for item in self.occurrences)


def build_assembly_model(
    analysis: ModelAnalysis,
    *,
    registry: ConnectorRegistry,
    voxel_catalog: Catalog,
) -> AssemblyModel:
    """Adapt one pyldraw analysis without quantizing its geometry."""
    inspection = analysis.inspection
    if inspection is None:
        msg = "model analysis must include pyldraw inspection geometry"
        raise ValueError(msg)
    voxel_by_part: dict[str, float] = {}
    for part in voxel_catalog.parts.values():
        voxel_by_part.setdefault(normalize_part_id(part.ldraw_part), part.mass_g)
    occurrences = tuple(
        _assembly_occurrence(
            geometry,
            registry=registry,
            voxel_mass_g=voxel_by_part.get(
                normalize_part_id(geometry.occurrence.part_code)
            ),
        )
        for geometry in inspection.occurrences
    )
    resolved_ids = {item.occurrence_id for item in occurrences}
    skipped_ids = tuple(
        item.attribution.index + 1
        for item in inspection.skipped_geometry
        if item.attribution.index + 1 not in resolved_ids
    )
    return AssemblyModel(
        analysis=analysis,
        occurrences=occurrences,
        skipped_occurrence_ids=skipped_ids,
        diagnostics=tuple(
            f"{item.code}: {item.message}" for item in analysis.diagnostics
        ),
    )


def _assembly_occurrence(
    geometry: OccurrenceGeometry,
    *,
    registry: ConnectorRegistry,
    voxel_mass_g: float | None,
) -> AssemblyOccurrence:
    occurrence = geometry.occurrence
    part_id = normalize_part_id(occurrence.part_code)
    metadata = registry.get(part_id)
    bounds = AssemblyBounds(
        minimum=_vector(geometry.bounds.min),
        maximum=_vector(geometry.bounds.max),
    )
    if metadata is not None and metadata.mass_g is not None:
        mass_g = metadata.mass_g
        mass_source = "connectivity_registry"
    elif voxel_mass_g is not None:
        mass_g = voxel_mass_g
        mass_source = "voxel_catalog"
    else:
        mass_g = _estimate_mass(geometry.local)
        mass_source = "geometry_convex_hull" if mass_g is not None else "unknown"
    center = _center_of_mass(
        geometry, metadata.center_of_mass_ldu if metadata else None
    )
    inertia = (
        metadata.inertia_g_ldu2
        if metadata is not None and metadata.inertia_g_ldu2 is not None
        else _box_inertia(bounds.size, mass_g)
    )
    matrix_values = tuple(float(value) for value in occurrence.matrix.flatten())
    if len(matrix_values) != 9:
        raise AssertionError
    return AssemblyOccurrence(
        occurrence_id=geometry.index + 1,
        pyldraw_index=geometry.index,
        part_id=part_id,
        reference=occurrence.reference,
        colour_code=occurrence.colour.code
        if occurrence.colour.code is not None
        else 16,
        position_ldu=_vector(occurrence.position),
        matrix=(
            matrix_values[0],
            matrix_values[1],
            matrix_values[2],
            matrix_values[3],
            matrix_values[4],
            matrix_values[5],
            matrix_values[6],
            matrix_values[7],
            matrix_values[8],
        ),
        geometry=geometry.local,
        bounds_ldu=bounds,
        mass_g=mass_g,
        mass_source=mass_source,
        center_of_mass_ldu=center,
        inertia_g_ldu2=inertia,
        tags=metadata.tags if metadata is not None else frozenset(),
        source=geometry.attribution,
    )


def _estimate_mass(geometry: PartGeometry) -> float | None:
    points = np.asarray(
        [(float(item.x), float(item.y), float(item.z)) for item in geometry.points],
        dtype=np.float64,
    )
    if len(points) < 4:
        return None
    try:
        volume_ldu3 = float(ConvexHull(np.unique(points, axis=0)).volume)
    except QhullError:
        if geometry.bounds is None:
            return None
        size = geometry.bounds.size
        volume_ldu3 = float(size.x * size.y * size.z)
    if not math.isfinite(volume_ldu3) or volume_ldu3 <= 0:
        return None
    mass = volume_ldu3 * _LDU_CM**3 * _ABS_DENSITY_G_CM3
    return max(mass, _MIN_ESTIMATED_MASS_G)


def _center_of_mass(
    geometry: OccurrenceGeometry,
    local_center: Vector | None,
) -> Vector | None:
    occurrence = geometry.occurrence
    if local_center is None:
        if geometry.local.bounds is None:
            return None
        local_bounds = geometry.local.bounds
        local_center = Vector(
            (local_bounds.min.x + local_bounds.max.x) / 2,
            (local_bounds.min.y + local_bounds.max.y) / 2,
            (local_bounds.min.z + local_bounds.max.z) / 2,
        )
    return _vector(occurrence.position + occurrence.matrix * local_center)


def _box_inertia(size: Vector, mass_g: float | None) -> Vector | None:
    if mass_g is None:
        return None
    return Vector(
        mass_g * (size.y**2 + size.z**2) / 12,
        mass_g * (size.x**2 + size.z**2) / 12,
        mass_g * (size.x**2 + size.y**2) / 12,
    )


def source_model(attribution: OccurrenceAttribution) -> str | None:
    """Return the model section directly containing the leaf placement."""
    return attribution.model_path[-1] if attribution.model_path else None


def source_line(attribution: OccurrenceAttribution) -> int | None:
    """Return the line directly placing the leaf part."""
    return attribution.source_line_path[-1] if attribution.source_line_path else None


def _vector(value: Vector) -> Vector:
    return Vector(
        float(value.x),
        float(value.y),
        float(value.z),
    )
