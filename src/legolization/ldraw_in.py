"""LDraw model import: ``.ldr``/``.mpd`` files → :class:`Layout` (strict).

The inverse of :mod:`legolization.ldraw_out`. Every piece must be a
catalogued part in one of the four yaw orientations, sitting on the
stud/plate grid, in the solid-colour palette; MPD submodels are
flattened through their composed world transforms. Anything else is
collected — not first-fail — into one :class:`LdrawImportError` listing
every problem, so a user sees the model's full distance to importable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ldraw.model import read_model

from legolization.catalog import Category, default_catalog, rotate_offset
from legolization.color import default_palette
from legolization.layout import CollisionError, Layout
from legolization.ldraw_out import PLATE_LDU, STUD_LDU, _rotate_ldu

if TYPE_CHECKING:
    from pathlib import Path

    from ldraw.geometry import Matrix, Vector
    from ldraw.model import ModelOccurrence

    from legolization.catalog import Catalog, Part

_GRID_EPS = 1e-6

# LDraw rotation rows for each supported yaw (Identity().rotate(yaw, YAxis)).
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

    def __init__(self, problems: list[str]) -> None:
        self.problems = tuple(problems)
        summary = "\n  ".join(problems)
        super().__init__(
            f"cannot import model ({len(problems)} problem(s)):\n  {summary}"
        )


def layout_from_ldraw(path: Path, *, catalog: Catalog | None = None) -> Layout:
    """Read an ``.ldr``/``.mpd`` model back into a :class:`Layout`.

    Strict: any part outside the catalog, non-yaw rotation, off-grid
    position, out-of-palette colour, collision, or below-ground brick is
    an error; all problems are reported together.
    """
    catalog = catalog or default_catalog()
    # Several catalog parts can share one LDraw code (a flat tile and its
    # sideways-mounted twin both emit 3070b), so the reverse map carries
    # every candidate in catalog order and the decode disambiguates: a
    # flat part only accepts yaw matrices (middle row (0, 1, 0)), a
    # sideways part only its mount matrices (middle row never (0, 1, 0)).
    reverse: dict[str, list[str]] = {}
    for part in catalog.parts.values():
        reverse.setdefault(part.ldraw_part, []).append(part.key)
    layout = Layout(catalog=catalog)
    problems: list[str] = []
    # iter_occurrences composes MPD submodel transforms into world frame;
    # iter_pieces would yield submodel pieces in their local frames.
    for index, occurrence in enumerate(read_model(path).iter_occurrences(), start=1):
        prefix = f"piece {index} ({occurrence.part_code})"
        if (candidates := reverse.get(str(occurrence.part_code))) is None:
            problems.append(f"{prefix}: part not in the catalog")
            continue
        if (colour := _decode_colour(occurrence.colour)) is None:
            problems.append(f"{prefix}: colour is not in the solid palette")
            continue
        matched = _match_candidates(catalog, candidates, occurrence)
        if isinstance(matched, str):
            problems.append(f"{prefix}: {matched}")
            continue
        matched_key, (x, y, layer, yaw) = matched
        try:
            layout.add(matched_key, x, y, layer, yaw, colour)
        except CollisionError as error:
            problems.append(f"{prefix}: {error}")
    if problems:
        raise LdrawImportError(problems)
    return layout


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
    if part.category is Category.SNOT:
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
    coords = []
    for value in (x, y, layer):
        rounded = round(value)
        if abs(value - rounded) > _GRID_EPS:
            return None
        coords.append(int(rounded))
    return (coords[0], coords[1], coords[2], yaw)


def _flat_matrix(matrix: Matrix) -> tuple[int, ...] | None:
    """Return the rotation rows as rounded ints, or None when non-integral."""
    flat: list[int] = []
    for row in matrix.rows:
        for value in row:
            rounded = round(float(value))
            if abs(float(value) - rounded) > _GRID_EPS:
                return None
            flat.append(int(rounded))
    return tuple(flat)


def _decode_yaw(matrix: Matrix) -> int | None:
    """Match a 3x3 rotation against the four canonical yaws."""
    flat = _flat_matrix(matrix)
    return None if flat is None else _YAW_MATRICES.get(flat)


def _decode_colour(colour: object) -> int | None:
    """Return the palette colour code, or None if unknown."""
    code = getattr(colour, "code", colour)
    if not isinstance(code, int):
        return None
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
    coords = []
    for value in (x, y, layer):
        rounded = round(value)
        if abs(value - rounded) > _GRID_EPS:
            return None
        coords.append(int(rounded))
    return (coords[0], coords[1], coords[2])
