"""Deterministic multi-frame lattice detection for arbitrary assemblies."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ldraw.geometry import Vector

from legolization.ldraw_units import GRID_TOLERANCE_LDU, PLATE_LDU, STUD_LDU

if TYPE_CHECKING:
    from collections.abc import Iterable

    from ldraw.model import ModelOccurrence


@dataclass(frozen=True, slots=True)
class GridFrame:
    """One fitted LDraw lattice basis, phase, and membership."""

    frame_id: int
    kind: str
    origin_ldu: Vector
    basis: tuple[float, float, float, float, float, float, float, float, float]
    inlier_occurrence_ids: tuple[int, ...]
    confidence: float
    horizontal_spacing_ldu: float = STUD_LDU
    vertical_spacing_ldu: float = PLATE_LDU


def detect_grid_frames(
    occurrences: Iterable[ModelOccurrence],
) -> tuple[GridFrame, ...]:
    """Cluster orthonormal placements by orientation and lattice phase."""
    materialized = tuple(occurrences)
    grouped: dict[
        tuple[tuple[int, ...], tuple[float, float, float]],
        list[int],
    ] = defaultdict(list)
    for occurrence_id, occurrence in enumerate(materialized, start=1):
        if (basis := _orthonormal_basis(occurrence.matrix.flatten())) is None:
            continue
        phase = (
            _phase(float(occurrence.position.x), STUD_LDU),
            _phase(float(occurrence.position.y), PLATE_LDU),
            _phase(float(occurrence.position.z), STUD_LDU),
        )
        grouped[(basis, phase)].append(occurrence_id)
    ordered = sorted(
        grouped.items(),
        key=lambda item: (-len(item[1]), item[0]),
    )
    eligible = [item for item in ordered if len(item[1]) >= 2]
    if not eligible and ordered:
        eligible = [ordered[0]]
    frames: list[GridFrame] = []
    for frame_id, ((basis, phase), ids) in enumerate(eligible, start=1):
        up = (basis[1], basis[4], basis[7])
        half_stud = any(
            math.isclose(value, STUD_LDU / 2, abs_tol=GRID_TOLERANCE_LDU)
            for value in (phase[0], phase[2])
        )
        if abs(up[1]) == 1:
            kind = "half-stud" if half_stud else "stud-up"
        elif abs(up[0]) == 1 or abs(up[2]) == 1:
            kind = "snot-half-stud" if half_stud else "snot"
        else:
            kind = "orthonormal"
        frames.append(
            GridFrame(
                frame_id=frame_id,
                kind=kind,
                origin_ldu=Vector(*phase),
                basis=(
                    float(basis[0]),
                    float(basis[1]),
                    float(basis[2]),
                    float(basis[3]),
                    float(basis[4]),
                    float(basis[5]),
                    float(basis[6]),
                    float(basis[7]),
                    float(basis[8]),
                ),
                inlier_occurrence_ids=tuple(ids),
                confidence=len(ids) / len(materialized) if materialized else 0.0,
            )
        )
    return tuple(frames)


def dominant_phase(
    occurrences: Iterable[ModelOccurrence],
) -> tuple[float, float, float]:
    """Return the most common position phase independent of orientation."""
    phases = Counter(
        (
            _phase(float(item.position.x), STUD_LDU),
            _phase(float(item.position.y), PLATE_LDU),
            _phase(float(item.position.z), STUD_LDU),
        )
        for item in occurrences
    )
    return phases.most_common(1)[0][0] if phases else (0.0, 0.0, 0.0)


def _phase(value: float, spacing: float) -> float:
    remainder = value % spacing
    if math.isclose(remainder, spacing, abs_tol=GRID_TOLERANCE_LDU):
        remainder = 0.0
    remainder = round(remainder / GRID_TOLERANCE_LDU) * GRID_TOLERANCE_LDU
    rounded = round(remainder)
    return (
        float(rounded)
        if math.isclose(
            remainder,
            rounded,
            abs_tol=GRID_TOLERANCE_LDU,
        )
        else remainder
    )


def _orthonormal_basis(values: Iterable[float]) -> tuple[int, ...] | None:
    rounded: list[int] = []
    for value in values:
        candidate = round(float(value))
        if abs(float(value) - candidate) > 1e-6 or candidate not in {-1, 0, 1}:
            return None
        rounded.append(candidate)
    if len(rounded) != 9:
        return None
    rows = tuple(
        tuple(rounded[3 * row + column] for column in range(3)) for row in range(3)
    )
    if any(sum(value * value for value in row) != 1 for row in rows):
        return None
    if any(
        sum(rows[first][i] * rows[second][i] for i in range(3))
        for first in range(3)
        for second in range(first + 1, 3)
    ):
        return None
    return tuple(rounded)
