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

# Sideways-tile rotations (ldraw_out._TILE_ROTATIONS images) → outward dir.
_TILE_MATRICES: dict[tuple[int, ...], tuple[int, int]] = {
    (0, -1, 0, 1, 0, 0, 0, 0, 1): (1, 0),
    (0, 1, 0, -1, 0, 0, 0, 0, 1): (-1, 0),
    (1, 0, 0, 0, 0, 1, 0, -1, 0): (0, 1),
    (1, 0, 0, 0, 0, -1, 0, 1, 0): (0, -1),
}

# Outward grid direction → placement yaw (mirror of snot._FACE_YAW).
_OUTWARD_YAW = {(1, 0): 0, (0, 1): 90, (-1, 0): 180, (0, -1): 270}


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
    reverse = {part.ldraw_part: part.key for part in catalog.parts.values()}
    layout = Layout(catalog=catalog)
    problems: list[str] = []
    # iter_occurrences composes MPD submodel transforms into world frame;
    # iter_pieces would yield submodel pieces in their local frames.
    for index, occurrence in enumerate(read_model(path).iter_occurrences(), start=1):
        prefix = f"piece {index} ({occurrence.part_code})"
        if (part_key := reverse.get(str(occurrence.part_code))) is None:
            problems.append(f"{prefix}: part not in the catalog")
            continue
        part = catalog.parts[part_key]
        if (colour := _decode_colour(occurrence.colour)) is None:
            problems.append(f"{prefix}: colour is not in the solid palette")
            continue
        if part.category is Category.SNOT:
            placed = _decode_snot(part, occurrence)
            if placed is None:
                problems.append(
                    f"{prefix}: sideways part in an unsupported orientation"
                )
                continue
            x, y, layer, yaw = placed
        else:
            if (yaw := _decode_yaw(occurrence.matrix)) is None:
                problems.append(f"{prefix}: rotation is not a yaw multiple of 90°")
                continue
            placement = _decode_position(part, occurrence.position, yaw)
            if placement is None:
                problems.append(f"{prefix}: position is off the stud/plate grid")
                continue
            x, y, layer = placement
        try:
            layout.add(part_key, x, y, layer, yaw, colour)
        except CollisionError as error:
            problems.append(f"{prefix}: {error}")
    if problems:
        raise LdrawImportError(problems)
    return layout


def _decode_snot(
    part: Part,
    occurrence: ModelOccurrence,
) -> tuple[int, int, int, int] | None:
    """Invert ``ldraw_out._snot_piece`` for the two sideways parts."""
    position = occurrence.position
    matrix = occurrence.matrix
    if part.mount_normal is None:  # the bracket: yaw + 90 about Y
        if (rotated := _decode_yaw(matrix)) is None:
            return None
        yaw = (rotated - 90) % 360
        placement = _decode_position(part, position, yaw)
        if placement is None:
            return None
        return (*placement, yaw)
    flat = _flat_matrix(matrix)
    if flat is None or (outward := _TILE_MATRICES.get(flat)) is None:
        return None
    ox, oy = outward
    yaw = _OUTWARD_YAW[outward]
    x = (float(position.x) + 2.0 * ox) / STUD_LDU
    y = (float(position.z) + 2.0 * oy) / STUD_LDU
    layer = (-float(position.y) - 12.0) / PLATE_LDU
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
