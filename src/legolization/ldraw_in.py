"""LDraw model import: ``.ldr``/``.mpd`` files → :class:`Layout` (strict).

The inverse of :mod:`legolization.ldraw_out`. Every piece must be a
catalogued part in one of the four yaw orientations, sitting on the
stud/plate grid, in the solid-colour palette; MPD submodels are
flattened through their composed world transforms. Anything else is
collected — not first-fail — into one :class:`LdrawImportError` listing
every problem, so a user sees the model's full distance to importable.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ldraw import (
    Diagnostic,
    InstructionDocument,
    ModelAnalysis,
    Severity,
    iter_ldr_issues,
    load_model,
)

from legolization.catalog import Category, default_catalog, rotate_offset
from legolization.color import default_palette
from legolization.layout import CollisionError, Layout
from legolization.ldraw_out import _rotate_ldu
from legolization.ldraw_units import (
    GRID_TOLERANCE_LDU,
    PLATE_LDU,
    ROTATION_TOLERANCE,
    STUD_LDU,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from ldraw.geometry import Matrix, Vector
    from ldraw.model import ModelOccurrence
    from ldraw.parts import Parts

    from legolization.catalog import Catalog, Part

# Raw LDraw rows for each logical grid yaw. Since pyldraw3 1.3 corrected its
# positive Y rotation, emission produces these with rotate(-yaw, YAxis).
_YAW_MATRICES: dict[tuple[int, ...], int] = {
    (1, 0, 0, 0, 1, 0, 0, 0, 1): 0,
    (0, 0, -1, 0, 1, 0, 1, 0, 0): 90,
    (-1, 0, 0, 0, 1, 0, 0, 0, -1): 180,
    (0, 0, 1, 0, 1, 0, -1, 0, 0): 270,
}


def _mount_inverse(part: Part) -> dict[tuple[int, ...], tuple[int, int]]:
    """Invert a cladding's pinned emission rotations: matrix rows → outward."""
    return {rows: outward for outward, rows in part.mount_matrices}


def _yaw_for_outward(part: Part, outward: tuple[int, int]) -> int | None:
    """Solve the placement yaw rotating the socket direction onto ``-outward``."""
    if (normal := part.mount_normal) is None:  # claddings only
        return None
    target = (-outward[0], -outward[1], normal[2])
    for yaw in part.orientations:
        if rotate_offset(normal, yaw) == target:
            return yaw
    return None


class LdrawImportError(ValueError):
    """The model contains pieces this pipeline cannot represent."""

    def __init__(
        self,
        details: Iterable[LdrawImportProblem],
        *,
        diagnostics: Iterable[Diagnostic] = (),
        load_diagnostics: Iterable[Diagnostic] | None = None,
        load_complete: bool | None = None,
    ) -> None:
        self.details = tuple(details)
        self.diagnostics = tuple(diagnostics)
        self.load_diagnostics = (
            self.diagnostics if load_diagnostics is None else tuple(load_diagnostics)
        )
        self.load_complete = load_complete
        diagnostic_problems = tuple(
            _diagnostic_summary(diagnostic)
            for diagnostic in self.diagnostics
            if diagnostic.severity is Severity.ERROR
        )
        self.problems = diagnostic_problems + tuple(
            problem.summary for problem in self.details
        )
        summary = "\n  ".join(self.problems)
        super().__init__(
            f"cannot import model ({len(self.problems)} problem(s)):\n  {summary}"
        )


def _diagnostic_summary(diagnostic: Diagnostic) -> str:
    """Render one pyldraw diagnostic for the compatibility error surface."""
    location = str(diagnostic.path) if diagnostic.path is not None else "model"
    if diagnostic.section is not None:
        location = f"{location}:{diagnostic.section}"
    if diagnostic.line_number is not None:
        location = f"{location}:{diagnostic.line_number}"
    return f"{diagnostic.code} at {location}: {diagnostic.message}"


@dataclass(frozen=True, slots=True)
class LdrawImportProblem:
    """One source-addressable problem found during strict import."""

    occurrence: int
    part_reference: str
    source_model: str
    source_line: int | None
    message: str

    @property
    def summary(self) -> str:
        """Render the compatibility error text used by existing callers."""
        location = (
            f"{self.source_model}:{self.source_line}"
            if self.source_line is not None
            else self.source_model
        )
        return (
            f"piece {self.occurrence} ({self.part_reference}) at "
            f"{location}: {self.message}"
        )


@dataclass(frozen=True, slots=True)
class LdrawSourceRef:
    """Location of one flattened occurrence in the source LDraw document."""

    occurrence: int
    source_model: str
    source_line: int | None
    source_step: int | None
    global_step: int | None
    path: tuple[LdrawSourcePathItem, ...] = ()


@dataclass(frozen=True, slots=True)
class LdrawSourcePathItem:
    """One placement in a flattened occurrence's root-to-leaf path."""

    source_model: str
    reference: str
    source_line: int | None
    local_step: int | None
    effective_step: int | None


@dataclass(frozen=True, slots=True)
class ImportedLdrawModel:
    """An imported layout plus source provenance and grounding metadata."""

    layout: Layout
    source_refs: dict[int, LdrawSourceRef]
    ground_offset_layers: int = 0
    load_complete: bool = True
    diagnostics: tuple[Diagnostic, ...] = ()
    instruction_document: InstructionDocument | None = None
    model_analysis: ModelAnalysis | None = None

    @property
    def has_explicit_steps(self) -> bool:
        """Whether the source contains more than the implicit first step."""
        if self.instruction_document is not None:
            return any(
                len(section.steps) > 1 for section in self.instruction_document.sections
            )
        return any(
            (ref.global_step or ref.source_step or 1) > 1
            for ref in self.source_refs.values()
        )


@dataclass(frozen=True, slots=True)
class _DecodedOccurrence:
    """One catalogued occurrence decoded into logical grid coordinates."""

    index: int
    occurrence: ModelOccurrence
    part_key: str
    x: int
    y: int
    layer: int
    yaw: int
    colour: int


@dataclass(frozen=True, slots=True)
class OccurrenceImport:
    """Decoded occurrences before they are wrapped in a document result."""

    layout: Layout
    source_refs: dict[int, LdrawSourceRef]
    occurrence_bricks: dict[int, int]
    ground_offset_layers: int
    problems: tuple[LdrawImportProblem, ...]


def layout_from_ldraw(
    path: Path,
    *,
    catalog: Catalog | None = None,
    ground: bool = False,
) -> Layout:
    """Read an ``.ldr``/``.mpd`` model back into a :class:`Layout`.

    Strict: any part outside the catalog, non-yaw rotation, off-grid
    position, out-of-palette colour, or collision is an error; all problems
    are reported together. ``ground=True`` shifts any vertical origin so the
    lowest occupied layer rests on the analysis ground plane. With
    ``ground=False``, below-ground geometry remains invalid.
    """
    return import_ldraw(path, catalog=catalog, ground=ground).layout


def import_ldraw(
    path: Path,
    *,
    catalog: Catalog | None = None,
    ground: bool = False,
    parts: Parts | None = None,
) -> ImportedLdrawModel:
    """Read a model once with diagnostics and stable occurrence provenance."""
    catalog = catalog or default_catalog()
    # Parser/structure diagnostics stay independent of pyldraw's catalog:
    # legolization's curated (and extensible) catalog remains authoritative
    # for whether a placement is supported. Exact geometry below can still
    # report an unavailable pyldraw part as supplementary skipped evidence.
    loaded = load_model(path)
    load_diagnostics = tuple(iter_ldr_issues(loaded))
    model_analysis = loaded.analyze(parts)
    if loaded.model is None or model_analysis is None:
        raise LdrawImportError(
            (),
            diagnostics=load_diagnostics,
            load_complete=loaded.complete,
        )
    document = loaded.model.instruction_document(parts=parts)
    imported = import_occurrences(
        model_analysis.occurrences,
        catalog=catalog,
        ground=ground,
        default_model=path.name,
    )
    if imported.problems or any(
        diagnostic.severity is Severity.ERROR for diagnostic in load_diagnostics
    ):
        raise LdrawImportError(
            imported.problems,
            diagnostics=model_analysis.diagnostics,
            load_diagnostics=load_diagnostics,
            load_complete=loaded.complete,
        )
    return ImportedLdrawModel(
        layout=imported.layout,
        source_refs=imported.source_refs,
        ground_offset_layers=imported.ground_offset_layers,
        load_complete=loaded.complete,
        diagnostics=load_diagnostics,
        instruction_document=document,
        model_analysis=model_analysis,
    )


def import_occurrences(
    occurrences: Iterable[ModelOccurrence],
    *,
    catalog: Catalog,
    ground: bool,
    default_model: str,
    allow_main_colour: bool = False,
) -> OccurrenceImport:
    """Decode an already-materialized occurrence stream into one layout."""
    # Several catalog parts can share one LDraw code (a flat tile and its
    # sideways-mounted twin both emit 3070b), so the reverse map carries
    # every candidate in catalog order and the decode disambiguates: a
    # flat part only accepts yaw matrices (middle row (0, 1, 0)), a
    # sideways part only its mount matrices (middle row never (0, 1, 0)).
    reverse: dict[str, list[str]] = {}
    for part in catalog.parts.values():
        reverse.setdefault(part.ldraw_part.casefold(), []).append(part.key)
    layout = Layout(catalog=catalog)
    source_refs: dict[int, LdrawSourceRef] = {}
    occurrence_bricks: dict[int, int] = {}
    problems: list[LdrawImportProblem] = []
    decoded: list[_DecodedOccurrence] = []
    for index, occurrence in enumerate(occurrences, start=1):
        if (candidates := reverse.get(str(occurrence.part_code).casefold())) is None:
            problems.append(
                _problem(
                    index,
                    occurrence,
                    "part not in the catalog",
                    default_model=default_model,
                )
            )
            continue
        if (
            colour := _decode_colour(
                occurrence.colour,
                allow_main_colour=allow_main_colour,
            )
        ) is None:
            problems.append(
                _problem(
                    index,
                    occurrence,
                    "colour is not in the solid palette",
                    default_model=default_model,
                )
            )
            continue
        matched = _match_candidates(catalog, candidates, occurrence)
        if isinstance(matched, str):
            problems.append(
                _problem(index, occurrence, matched, default_model=default_model)
            )
            continue
        matched_key, (x, y, layer, yaw) = matched
        decoded.append(
            _DecodedOccurrence(
                index=index,
                occurrence=occurrence,
                part_key=matched_key,
                x=x,
                y=y,
                layer=layer,
                yaw=yaw,
                colour=colour,
            )
        )
    ground_offset = min((item.layer for item in decoded), default=0) if ground else 0
    for item in decoded:
        try:
            brick = layout.add(
                part_key=item.part_key,
                x=item.x,
                y=item.y,
                layer=item.layer - ground_offset,
                yaw=item.yaw,
                colour_code=item.colour,
            )
        except CollisionError as error:
            problems.append(
                _problem(
                    item.index,
                    item.occurrence,
                    str(error),
                    default_model=default_model,
                )
            )
        else:
            source_refs[brick.brick_id] = LdrawSourceRef(
                occurrence=item.index,
                source_model=str(item.occurrence.source_model.name or default_model),
                source_line=item.occurrence.source_line,
                source_step=item.occurrence.source_step,
                global_step=item.occurrence.step,
                path=tuple(
                    LdrawSourcePathItem(
                        source_model=str(path_item.model.name or default_model),
                        reference=path_item.piece.reference,
                        source_line=path_item.source_line,
                        local_step=path_item.local_step,
                        effective_step=path_item.effective_step,
                    )
                    for path_item in item.occurrence.path
                ),
            )
            occurrence_bricks[item.index] = brick.brick_id
    return OccurrenceImport(
        layout=layout,
        source_refs=source_refs,
        occurrence_bricks=occurrence_bricks,
        ground_offset_layers=ground_offset,
        problems=tuple(problems),
    )


def _problem(
    index: int,
    occurrence: ModelOccurrence,
    message: str,
    *,
    default_model: str,
) -> LdrawImportProblem:
    """Capture a strict-import diagnostic with its original source location."""
    return LdrawImportProblem(
        occurrence=index,
        part_reference=str(occurrence.part_code),
        source_model=str(occurrence.source_model.name or default_model),
        source_line=occurrence.source_line,
        message=message,
    )


def _match_candidates(
    catalog: Catalog,
    candidates: list[str],
    occurrence: ModelOccurrence,
) -> tuple[str, tuple[int, int, int, int]] | str:
    """Decode against every candidate part; exactly one clean fit wins.

    The decode sets of parts sharing an LDraw code are disjoint by
    construction (a flat part only accepts yaw matrices, a sideways part
    only its pinned mount matrices), but that is a property of today's
    catalog data — two clean fits are reported as ambiguity, not
    silently resolved by catalog order.
    """
    reasons: list[str] = []
    fits: list[tuple[str, tuple[int, int, int, int]]] = []
    for part_key in candidates:
        decoded = _decode_occurrence(catalog.parts[part_key], occurrence)
        if isinstance(decoded, str):
            reasons.append(decoded)
        else:
            fits.append((part_key, decoded))
    if len(fits) == 1:
        return fits[0]
    if fits:
        keys = ", ".join(key for key, _ in fits)
        return f"ambiguous piece: decodes as {keys}"
    if len(candidates) == 1:
        return reasons[0]
    detail = "; ".join(
        f"{key}: {reason}" for key, reason in zip(candidates, reasons, strict=True)
    )
    return f"no candidate part fits ({detail})"


def _decode_occurrence(
    part: Part,
    occurrence: ModelOccurrence,
) -> tuple[int, int, int, int] | str:
    """Decode one occurrence as ``part``: placement, or the failure reason."""
    if part.category in (Category.SNOT, Category.SPECIAL_SNOT):
        if (snot := _decode_snot(part, occurrence)) is None:
            return "sideways part in an unsupported orientation"
        return snot
    if (yaw := _decode_yaw(occurrence.matrix)) is None:
        return "rotation is not a yaw multiple of 90°"
    if (placement := _decode_position(part, occurrence.position, yaw)) is None:
        return "position is off the stud/plate grid"
    return (*placement, yaw)


def _decode_snot(
    part: Part,
    occurrence: ModelOccurrence,
) -> tuple[int, int, int, int] | None:
    """Invert the SNOT emission paths, both driven by catalog data.

    Carriers are ordinary bodies with an extra emission yaw: subtract
    ``emit_yaw_offset`` and reuse the standard position inversion.
    Claddings look their outward direction up in the pinned mount
    matrices, solve the placement yaw from the socket direction, and
    invert the centroid-plus-``mount_offset_ldu`` origin.
    """
    if part.mount_normal is None:  # a carrier
        if (rotated := _decode_yaw(occurrence.matrix)) is None:
            return None
        yaw = (rotated - part.emit_yaw_offset) % 360
        placement = _decode_position(part, occurrence.position, yaw)
        if placement is None:
            return None
        return (*placement, yaw)
    return _decode_cladding(part, occurrence)


def _decode_cladding(
    part: Part,
    occurrence: ModelOccurrence,
) -> tuple[int, int, int, int] | None:
    """Invert the cladding origin: pinned matrix → outward, centroid → anchor."""
    position = occurrence.position
    flat = _flat_matrix(occurrence.matrix)
    if flat is None or (outward := _mount_inverse(part).get(flat)) is None:
        return None
    if (yaw := _yaw_for_outward(part, outward)) is None:
        return None
    ox, oy = outward
    offset_out, offset_up, offset_across = part.mount_offset_ldu
    across_x, across_y = -oy, ox
    rotated_cols = [
        rotate_offset((dx, dy, 0), yaw) for dx, dy in sorted(part.footprint)
    ]
    mean_rx = sum(rx for rx, _, _ in rotated_cols) / len(rotated_cols)
    mean_ry = sum(ry for _, ry, _ in rotated_cols) / len(rotated_cols)
    x = (
        float(position.x) - offset_out * ox - offset_across * across_x
    ) / STUD_LDU - mean_rx
    y = (
        float(position.z) - offset_out * oy - offset_across * across_y
    ) / STUD_LDU - mean_ry
    layer = (offset_up - float(position.y)) / PLATE_LDU
    coords = _grid_coordinates((x, y, layer))
    return None if coords is None else (*coords, yaw)


def _flat_matrix(matrix: Matrix) -> tuple[int, ...] | None:
    """Return the rotation rows as rounded ints, or None when non-integral."""
    flat: list[int] = []
    for row in matrix.rows:
        for value in row:
            rounded = round(float(value))
            if abs(float(value) - rounded) > ROTATION_TOLERANCE:
                return None
            flat.append(int(rounded))
    return tuple(flat)


def _decode_yaw(matrix: Matrix) -> int | None:
    """Match a 3x3 rotation against the four canonical yaws."""
    flat = _flat_matrix(matrix)
    return None if flat is None else _YAW_MATRICES.get(flat)


def _decode_colour(
    colour: object,
    *,
    allow_main_colour: bool = False,
) -> int | None:
    """Return the palette colour code, or None if unknown."""
    code = getattr(colour, "code", colour)
    if not isinstance(code, int):
        return None
    if allow_main_colour and code == 16:
        # Colour 16 means "inherit from the placement", which has no world
        # value when a submodel section is analyzed standalone. Substitute
        # neutral Light_Grey — colour never affects physics — and still run
        # it through the palette check below.
        code = 7
    try:
        default_palette().name_of(code)
    except ValueError:
        return None
    return code


def _decode_position(
    part: Part,
    position: Vector,
    yaw: int,
) -> tuple[int, int, int] | None:
    """Invert ``ldraw_out.piece_for``'s position math; None if off-grid."""
    offset_x, offset_y, offset_z = part.origin_offset
    rotated_ox, rotated_oz = _rotate_ldu(offset_x, offset_z, yaw)
    rotated = [rotate_offset((dx, dy, 0), yaw) for dx, dy in sorted(part.footprint)]
    mean_rx = sum(rx for rx, _, _ in rotated) / len(rotated)
    mean_ry = sum(ry for _, ry, _ in rotated) / len(rotated)
    x = (float(position.x) - rotated_ox) / STUD_LDU - mean_rx
    y = (float(position.z) - rotated_oz) / STUD_LDU - mean_ry
    layer = -(float(position.y) - offset_y) / PLATE_LDU - part.height_plates
    return _grid_coordinates((x, y, layer))


def _grid_coordinates(
    values: tuple[float, float, float],
) -> tuple[int, int, int] | None:
    """Snap x/z stud units and vertical plate units using one LDU budget."""
    coords: list[int] = []
    for value, scale in zip(values, (STUD_LDU, STUD_LDU, PLATE_LDU), strict=True):
        rounded = round(value)
        error_ldu = abs(value - rounded) * scale
        if error_ldu > GRID_TOLERANCE_LDU and not math.isclose(
            error_ldu,
            GRID_TOLERANCE_LDU,
            abs_tol=1e-9,
        ):
            return None
        coords.append(int(rounded))
    return (coords[0], coords[1], coords[2])
