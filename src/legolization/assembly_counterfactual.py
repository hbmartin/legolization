"""Bounded BOM-preserving counterfactual diagnosis for LDraw placements."""

from __future__ import annotations

import math
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from ldraw import load_model
from pyldcad import ConnectivityConfig, analyze_connectivity

if TYPE_CHECKING:
    from ldraw import BoundingBox, ModelAnalysis
    from ldraw.parts import Parts
    from pyldcad import ConnectivityAnalysis

    from legolization.assembly_model import (
        AssemblyBounds,
        AssemblyModel,
        AssemblyOccurrence,
    )

type EditKind = Literal["rotate", "translate", "mirror"]
type Matrix3 = tuple[
    tuple[float, float, float],
    tuple[float, float, float],
    tuple[float, float, float],
]


@dataclass(frozen=True, slots=True)
class CounterfactualCandidate:
    """One validated one-placement edit and its topology evidence."""

    occurrence_id: int
    source_model: str | None
    source_line: int
    part_id: str
    kind: EditKind
    description: str
    before_component_count: int
    after_component_count: int
    before_connection_count: int
    after_connection_count: int
    confirmed_connection_gain: int
    component_reduction: int
    confidence: float
    model_text: str


@dataclass(frozen=True, slots=True)
class CounterfactualSearchResult:
    """Best candidate plus deterministic search accounting."""

    status: str
    candidate: CounterfactualCandidate | None
    tested_candidates: int
    timed_out: bool
    elapsed_s: float
    message: str


@dataclass(frozen=True, slots=True)
class _Edit:
    kind: EditKind
    description: str
    matrix: Matrix3 | None = None
    translation: tuple[float, float, float] | None = None
    magnitude: float = 0.0


def search_counterfactuals(  # noqa: C901, PLR0913 - bounded search orchestration
    path: Path,
    *,
    model: AssemblyModel,
    connectivity: ConnectivityAnalysis,
    parts: Parts,
    connectivity_config: ConnectivityConfig,
    time_budget_s: float,
) -> CounterfactualSearchResult:
    """Search implicated placements for one connector-improving edit."""
    # Bounded deterministic search is one deadline-aware candidate state machine.
    # lizard forgives(cyclomatic_complexity, length)
    started = time.perf_counter()
    if connectivity.confirmed_component_count <= 1:
        return CounterfactualSearchResult(
            status="not_needed",
            candidate=None,
            tested_candidates=0,
            timed_out=False,
            elapsed_s=0.0,
            message="confirmed topology is already connected",
        )
    original = path.read_text()
    baseline_overlaps = _overlap_pairs(model)
    candidates = _candidate_occurrences(model, connectivity)
    best: CounterfactualCandidate | None = None
    best_score: tuple[int, int, float] | None = None
    tested = 0
    timed_out = False
    for occurrence in candidates:
        if (source_line := occurrence.source.source_line) is None:
            continue
        for edit in _edits():
            if time.perf_counter() - started >= time_budget_s:
                timed_out = True
                break
            if (
                candidate_text := _apply_edit(
                    original,
                    source_line=source_line,
                    expected_reference=occurrence.reference,
                    edit=edit,
                )
            ) is None:
                continue
            tested += 1
            candidate_analysis = _analyze_text(
                candidate_text,
                suffix=path.suffix,
                directory=path.parent,
                parts=parts,
            )
            if candidate_analysis is None or candidate_analysis.inspection is None:
                continue
            candidate_connectivity = analyze_connectivity(
                candidate_analysis,
                connectivity_config,
            )
            component_reduction = (
                connectivity.confirmed_component_count
                - candidate_connectivity.confirmed_component_count
            )
            connection_gain = len(candidate_connectivity.confirmed_connections) - len(
                connectivity.confirmed_connections
            )
            if component_reduction <= 0 and connection_gain <= 0:
                continue
            if _introduces_collision(
                candidate_analysis,
                baseline_overlaps=baseline_overlaps,
                connected_pairs={
                    edge.occurrence_ids for edge in candidate_connectivity.connections
                },
            ):
                continue
            score = (component_reduction, connection_gain, -edit.magnitude)
            if best_score is None or score > best_score:
                best_score = score
                best = CounterfactualCandidate(
                    occurrence_id=occurrence.occurrence_id,
                    source_model=occurrence.source.source_model,
                    source_line=source_line,
                    part_id=occurrence.part_id,
                    kind=edit.kind,
                    description=edit.description,
                    before_component_count=connectivity.confirmed_component_count,
                    after_component_count=(
                        candidate_connectivity.confirmed_component_count
                    ),
                    before_connection_count=len(connectivity.confirmed_connections),
                    after_connection_count=len(
                        candidate_connectivity.confirmed_connections
                    ),
                    confirmed_connection_gain=connection_gain,
                    component_reduction=component_reduction,
                    confidence=(
                        0.95
                        if component_reduction > 0 and connection_gain >= 4
                        else 0.8
                    ),
                    model_text=candidate_text,
                )
                if component_reduction > 0 and connection_gain >= 4:
                    break
        if timed_out or (
            best is not None
            and best.component_reduction > 0
            and best.confirmed_connection_gain >= 4
        ):
            break
    elapsed = time.perf_counter() - started
    return CounterfactualSearchResult(
        status="found" if best is not None else "not_found",
        candidate=best,
        tested_candidates=tested,
        timed_out=timed_out,
        elapsed_s=elapsed,
        message=(
            "found a BOM-preserving connector improvement"
            if best is not None
            else "no validated one-edit connector improvement was found"
        ),
    )


def _candidate_occurrences(
    model: AssemblyModel,
    connectivity: ConnectivityAnalysis,
) -> tuple[AssemblyOccurrence, ...]:
    sizes = {
        occurrence_id: len(component)
        for component in connectivity.confirmed_components
        for occurrence_id in component
    }
    return tuple(
        sorted(
            model.occurrences,
            key=lambda item: (
                item.source.source_page
                if item.source.source_page is not None
                else math.inf,
                sizes.get(item.occurrence_id, math.inf),
                item.occurrence_id,
            ),
        )[:24]
    )


def _edits() -> tuple[_Edit, ...]:
    return (
        _Edit("rotate", "rotate local Y by 90 degrees", _rotation_y(90), magnitude=1),
        _Edit("rotate", "rotate local Y by -90 degrees", _rotation_y(-90), magnitude=1),
        _Edit("rotate", "rotate local Y by 180 degrees", _rotation_y(180), magnitude=2),
        _Edit("rotate", "rotate local X by 90 degrees", _rotation_x(90), magnitude=1),
        _Edit("rotate", "rotate local X by -90 degrees", _rotation_x(-90), magnitude=1),
        _Edit("rotate", "rotate local Z by 90 degrees", _rotation_z(90), magnitude=1),
        _Edit("rotate", "rotate local Z by -90 degrees", _rotation_z(-90), magnitude=1),
        _Edit(
            "translate", "translate +20 LDU on X", translation=(20, 0, 0), magnitude=20
        ),
        _Edit(
            "translate", "translate -20 LDU on X", translation=(-20, 0, 0), magnitude=20
        ),
        _Edit("translate", "translate +8 LDU on Y", translation=(0, 8, 0), magnitude=8),
        _Edit(
            "translate", "translate -8 LDU on Y", translation=(0, -8, 0), magnitude=8
        ),
        _Edit(
            "translate", "translate +20 LDU on Z", translation=(0, 0, 20), magnitude=20
        ),
        _Edit(
            "translate", "translate -20 LDU on Z", translation=(0, 0, -20), magnitude=20
        ),
        _Edit(
            "translate", "translate +10 LDU on X", translation=(10, 0, 0), magnitude=10
        ),
        _Edit(
            "translate", "translate -10 LDU on X", translation=(-10, 0, 0), magnitude=10
        ),
        _Edit(
            "translate", "translate +10 LDU on Z", translation=(0, 0, 10), magnitude=10
        ),
        _Edit(
            "translate", "translate -10 LDU on Z", translation=(0, 0, -10), magnitude=10
        ),
        _Edit(
            "mirror",
            "mirror local X",
            matrix=((-1, 0, 0), (0, 1, 0), (0, 0, 1)),
            magnitude=1,
        ),
    )


def _apply_edit(
    text: str,
    *,
    source_line: int,
    expected_reference: str,
    edit: _Edit,
) -> str | None:
    lines = text.splitlines()
    if source_line < 1 or source_line > len(lines):
        return None
    fields = lines[source_line - 1].split()
    if len(fields) < 15 or fields[0] != "1":
        return None
    if (
        fields[-1].replace("\\", "/").casefold()
        != expected_reference.replace("\\", "/").casefold()
    ):
        return None
    try:
        numbers = [float(item) for item in fields[2:14]]
    except ValueError:
        return None
    position = numbers[:3]
    matrix: Matrix3 = (
        (numbers[3], numbers[4], numbers[5]),
        (numbers[6], numbers[7], numbers[8]),
        (numbers[9], numbers[10], numbers[11]),
    )
    if edit.translation is not None:
        position = [
            value + delta
            for value, delta in zip(position, edit.translation, strict=True)
        ]
    if edit.matrix is not None:
        matrix = _multiply(edit.matrix, matrix)
    serialized = [
        "1",
        fields[1],
        *(_format(value) for value in position),
        *(_format(value) for row in matrix for value in row),
        fields[-1],
    ]
    lines[source_line - 1] = " ".join(serialized)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _analyze_text(
    text: str,
    *,
    suffix: str,
    directory: Path,
    parts: Parts,
) -> ModelAnalysis | None:
    # Keep the candidate beside the source so relative external references retain
    # the same resolution root, while making concurrent analyses independent.
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix=".legolization-counterfactual-",
        suffix=suffix,
        dir=directory,
        delete=False,
    ) as handle:
        handle.write(text)
        candidate_path = Path(handle.name)
    try:
        return load_model(candidate_path).analyze(parts)
    finally:
        candidate_path.unlink(missing_ok=True)


def _overlap_pairs(model: AssemblyModel) -> frozenset[tuple[int, int]]:
    return frozenset(
        (left.occurrence_id, right.occurrence_id)
        for index, left in enumerate(model.occurrences)
        for right in model.occurrences[index + 1 :]
        if _bounds_overlap(left.bounds_ldu, right.bounds_ldu)
    )


def _introduces_collision(
    analysis: ModelAnalysis,
    *,
    baseline_overlaps: frozenset[tuple[int, int]],
    connected_pairs: set[tuple[int, int]],
) -> bool:
    inspection = getattr(analysis, "inspection", None)
    if inspection is None:
        return True
    occurrences = inspection.occurrences
    for index, left in enumerate(occurrences):
        for right in occurrences[index + 1 :]:
            pair = (left.index + 1, right.index + 1)
            if pair in baseline_overlaps or pair in connected_pairs:
                continue
            if _raw_bounds_overlap(left.bounds, right.bounds):
                return True
    return False


def _bounds_overlap(left: AssemblyBounds, right: AssemblyBounds) -> bool:
    return all(
        minimum < maximum - 0.25
        for minimum, maximum in (
            (
                max(left.minimum.x, right.minimum.x),
                min(left.maximum.x, right.maximum.x),
            ),
            (
                max(left.minimum.y, right.minimum.y),
                min(left.maximum.y, right.maximum.y),
            ),
            (
                max(left.minimum.z, right.minimum.z),
                min(left.maximum.z, right.maximum.z),
            ),
        )
    )


def _raw_bounds_overlap(left: BoundingBox, right: BoundingBox) -> bool:
    return all(
        minimum < maximum - 0.25
        for minimum, maximum in (
            (max(left.min.x, right.min.x), min(left.max.x, right.max.x)),
            (max(left.min.y, right.min.y), min(left.max.y, right.max.y)),
            (max(left.min.z, right.min.z), min(left.max.z, right.max.z)),
        )
    )


def _multiply(
    left: Matrix3,
    right: Matrix3,
) -> Matrix3:
    def cell(row: int, column: int) -> float:
        return float(sum(left[row][index] * right[index][column] for index in range(3)))

    return (
        (cell(0, 0), cell(0, 1), cell(0, 2)),
        (cell(1, 0), cell(1, 1), cell(1, 2)),
        (cell(2, 0), cell(2, 1), cell(2, 2)),
    )


def _rotation_x(degrees: int) -> Matrix3:
    angle = math.radians(degrees)
    cosine, sine = round(math.cos(angle)), round(math.sin(angle))
    return ((1, 0, 0), (0, cosine, -sine), (0, sine, cosine))


def _rotation_y(degrees: int) -> Matrix3:
    angle = math.radians(degrees)
    cosine, sine = round(math.cos(angle)), round(math.sin(angle))
    return ((cosine, 0, -sine), (0, 1, 0), (sine, 0, cosine))


def _rotation_z(degrees: int) -> Matrix3:
    angle = math.radians(degrees)
    cosine, sine = round(math.cos(angle)), round(math.sin(angle))
    return ((cosine, -sine, 0), (sine, cosine, 0), (0, 0, 1))


def _format(value: float) -> str:
    rounded = round(value)
    return str(rounded) if math.isclose(value, rounded, abs_tol=1e-9) else f"{value:g}"
