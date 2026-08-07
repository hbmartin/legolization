"""Infer coarse grid geometry for one LDraw part from the parts library.

The part's ``.dat`` tree is expanded through pyldraw3 (line types 1, 3,
and 4; recursion stops at ``stud*`` primitives, which become connector
evidence instead of body geometry). Face bounding boxes are voxelized
onto the 20x20-LDU-per-stud, 8-LDU-per-plate cell grid, producing
``occupied_cells``/``height_plates``/``origin_offset``, stud top
connectors, heuristic anti-stud bottom connectors with per-connector
confidence, and merged collision boxes covering the occupied cells.

When any step cannot produce confident geometry the result is still
returned as a draft, with ``confident=False`` and the reasons recorded,
so the caller can require generic complete geometry instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING

from ldraw.geometry import Identity, Vector
from ldraw.lines import Quadrilateral, Triangle
from ldraw.part_geometry import PartError, part_geometry
from ldraw.parts import Parts
from ldraw.pieces import Piece

from legolization.catalog import DOWN, UP, Cell
from legolization.errors import ConfigurationError
from legolization.ldraw_units import PLATE_LDU, STUD_LDU
from legolization.physical import LduBox

if TYPE_CHECKING:
    from ldraw.geometry import Matrix
    from ldraw.part_geometry import PartGeometry

_STUD = int(STUD_LDU)
_PLATE = int(PLATE_LDU)
_HALF_STUD = _STUD // 2

_SNAP_TOLERANCE_LDU = 1.0
"""How far from the stud/plate lattice inferred extents may sit."""

_OVERLAP_EPSILON_LDU = 0.5
"""Minimum positive face/column overlap that proves occupancy."""

_MAX_SUBFILE_DEPTH = 24

_STUD_CONFIDENCE = 0.95
_SOCKET_CONFIDENCE = 0.9
_OPEN_BOTTOM_CONFIDENCE = 0.6


@dataclass(frozen=True, slots=True, kw_only=True)
class InferredConnector:
    """One inferred stud or anti-stud with its evidence and confidence."""

    cell: Cell
    direction: Cell
    confidence: float
    basis: str


@dataclass(frozen=True, slots=True, kw_only=True)
class InferredGeometry:
    """Grid geometry inferred for one LDraw part."""

    part_id: str
    source_path: Path
    occupied_cells: frozenset[Cell]
    height_plates: int
    origin_offset: tuple[float, float, float]
    top_connectors: tuple[InferredConnector, ...]
    bottom_connectors: tuple[InferredConnector, ...]
    collision_boxes: tuple[LduBox, ...]
    confident: bool
    notes: tuple[str, ...]

    @property
    def cell_count(self) -> int:
        """Number of occupied unit cells."""
        return len(self.occupied_cells)


@dataclass(frozen=True, slots=True)
class _FaceBox:
    """Axis-aligned bounds of one drawn face in part-local LDU."""

    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float


class _PartLibrary:
    """A pyldraw3 :class:`Parts` handle over one LDraw directory.

    Libraries without a ``parts.lst`` (the managed ``complete.zip``
    extraction does not generate one) are served through a temporary
    shim directory holding an empty list beside symlinks to the real
    ``parts/``/``p/`` trees; the shim stays alive with this object.
    """

    def __init__(self, ldraw_dir: Path) -> None:
        if not (ldraw_dir / "parts").is_dir():
            msg = (
                f"{ldraw_dir} is not an LDraw parts library (no parts/ tree); "
                "run 'legolization parts sync' or set $LDRAWDIR"
            )
            raise ConfigurationError(msg)
        self._shim: TemporaryDirectory[str] | None = None
        parts_lst = ldraw_dir / "parts.lst"
        if not parts_lst.is_file():
            self._shim = TemporaryDirectory(prefix="legolization-ldraw-")
            shim_root = Path(self._shim.name)
            for name in ("parts", "p"):
                if (source := ldraw_dir / name).is_dir():
                    (shim_root / name).symlink_to(source, target_is_directory=True)
            for config in ("LDConfig.ldr", "ldconfig.ldr"):
                if (source := ldraw_dir / config).is_file():
                    (shim_root / "LDConfig.ldr").symlink_to(source)
                    break
            parts_lst = shim_root / "parts.lst"
            parts_lst.write_text("")
        try:
            self.parts = Parts(parts_lst)
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Release the shim directory, if one was created."""
        if self._shim is not None:
            self._shim.cleanup()
            self._shim = None


_PART_ID_PATTERN = re.compile(r"^[0-9a-z][0-9a-z._-]*$")


def normalize_part_id(part_id: str) -> str:
    """Normalize one user-supplied LDraw part identifier.

    The normalized id reaches filesystem paths (the support-bundle
    directory, ``geometry/<id>.dat``) and source-lookup URLs, so only
    LDraw part-code characters are accepted.
    """
    normalized = part_id.strip().lower().removesuffix(".dat")
    if not _PART_ID_PATTERN.match(normalized) or ".." in normalized:
        msg = f"invalid LDraw part id {part_id!r}"
        raise ConfigurationError(msg)
    return normalized


def infer_geometry(part_id: str, *, ldraw_dir: Path | None = None) -> InferredGeometry:
    """Infer draft grid geometry for ``part_id`` from the parts library."""
    code = normalize_part_id(part_id)
    directory = ldraw_dir if ldraw_dir is not None else _detected_library()
    library = _PartLibrary(directory)
    try:
        return _infer_from_library(library, code)
    finally:
        library.close()


def _detected_library() -> Path:
    from legolization.instructions.render import detect_ldraw_dir  # noqa: PLC0415

    if (directory := detect_ldraw_dir()) is None:
        msg = (
            "no LDraw parts library found; run 'legolization parts sync' "
            "or set $LDRAWDIR"
        )
        raise ConfigurationError(msg)
    return directory


def _infer_from_library(library: _PartLibrary, code: str) -> InferredGeometry:
    try:
        # Resolve through any shim symlinks: the path must outlive them.
        source_path = library.parts.part(code=code).path.resolve()
        expanded = part_geometry(library.parts, code)
        faces: list[_FaceBox] = []
        _collect_face_boxes(library, code, Identity(), Vector(0, 0, 0), faces, 0)
    except PartError as error:
        msg = f"LDraw part {code!r} is not available in the parts library: {error}"
        raise ConfigurationError(msg) from error
    notes: list[str] = [
        f"incomplete subfile expansion: {diagnostic.message}"
        for diagnostic in expanded.diagnostics
    ]
    if not faces:
        msg = f"LDraw part {code!r} draws no face geometry to voxelize"
        raise ConfigurationError(msg)
    return _voxelize(code, source_path, expanded, tuple(faces), notes)


def _collect_face_boxes(  # noqa: PLR0913, PLR0917 - one recursion frame per subfile
    library: _PartLibrary,
    code: str,
    matrix: Matrix,
    offset: Vector,
    faces: list[_FaceBox],
    depth: int,
) -> None:
    """Recursively fold line-type 3/4 face bounds through type-1 refs."""
    # lizard forgives(parameter_count)
    if depth > _MAX_SUBFILE_DEPTH:
        return
    part = library.parts.part(code=code)
    for parsed in part.objects:
        match parsed:
            case Triangle() | Quadrilateral():
                points = [matrix * point + offset for point in parsed.points]
                faces.append(_face_box(points))
            case Piece() if parsed.part.lower().startswith("stud"):
                continue  # Studs are connector evidence, not body geometry.
            case Piece():
                child = library.parts.find_part(code=parsed.part)
                if child is None:
                    continue  # Recorded by part_geometry diagnostics.
                _collect_face_boxes(
                    library,
                    parsed.part,
                    matrix * parsed.matrix,
                    matrix * parsed.position + offset,
                    faces,
                    depth + 1,
                )
            case _:
                continue


def _face_box(points: list[Vector]) -> _FaceBox:
    xs = [float(point.x) for point in points]
    ys = [float(point.y) for point in points]
    zs = [float(point.z) for point in points]
    return _FaceBox(
        min_x=min(xs),
        min_y=min(ys),
        min_z=min(zs),
        max_x=max(xs),
        max_y=max(ys),
        max_z=max(zs),
    )


@dataclass(slots=True, kw_only=True)
class _Lattice:
    """The part-local stud/plate lattice fitted to the face bounds."""

    min_x: float
    min_z: float
    top_y: float
    bottom_y: float
    length_studs: int
    width_studs: int
    height_plates: int

    def column_center(self, dx: int, dy: int) -> tuple[float, float]:
        return (
            self.min_x + _HALF_STUD + _STUD * dx,
            self.min_z + _HALF_STUD + _STUD * dy,
        )


def _snap(value: float, unit: float) -> tuple[int, float]:
    count = round(value / unit)
    return count, abs(value - unit * count)


def _fit_lattice(faces: tuple[_FaceBox, ...], notes: list[str]) -> _Lattice | None:
    min_x = min(face.min_x for face in faces)
    max_x = max(face.max_x for face in faces)
    min_z = min(face.min_z for face in faces)
    max_z = max(face.max_z for face in faces)
    top_y = min(face.min_y for face in faces)
    bottom_y = max(face.max_y for face in faces)
    length, length_error = _snap(max_x - min_x, STUD_LDU)
    width, width_error = _snap(max_z - min_z, STUD_LDU)
    height, height_error = _snap(bottom_y - top_y, PLATE_LDU)
    problems = [
        f"footprint span {max_x - min_x:g} x {max_z - min_z:g} LDU is not on "
        f"the {_STUD}-LDU stud lattice"
        if max(length_error, width_error) > _SNAP_TOLERANCE_LDU
        else None,
        f"height {bottom_y - top_y:g} LDU is not a whole number of {_PLATE}-LDU plates"
        if height_error > _SNAP_TOLERANCE_LDU
        else None,
        "part has a degenerate stud/plate extent"
        if min(length, width, height) < 1
        else None,
    ]
    if found := [problem for problem in problems if problem is not None]:
        notes.extend(found)
        return None
    return _Lattice(
        min_x=min_x,
        min_z=min_z,
        top_y=top_y,
        bottom_y=bottom_y,
        length_studs=length,
        width_studs=width,
        height_plates=height,
    )


def _voxelize(
    code: str,
    source_path: Path,
    expanded: PartGeometry,
    faces: tuple[_FaceBox, ...],
    notes: list[str],
) -> InferredGeometry:
    lattice = _fit_lattice(faces, notes)
    if lattice is None:
        notes.append("geometry is a draft requiring generic complete geometry")
        return _unconfident_bounding_draft(code, source_path, faces, notes)
    cells = _occupied_cells(lattice, faces)
    if not cells:
        notes.append("no face overlaps any grid column")
        notes.append("geometry is a draft requiring generic complete geometry")
        return _unconfident_bounding_draft(code, source_path, faces, notes)
    top, dropped_studs = _top_connectors(lattice, expanded, cells, notes)
    bottom = _bottom_connectors(lattice, expanded, cells)
    confident = not expanded.diagnostics and not dropped_studs
    return InferredGeometry(
        part_id=code,
        source_path=source_path,
        occupied_cells=cells,
        height_plates=lattice.height_plates,
        origin_offset=_origin_offset(lattice, cells),
        top_connectors=top,
        bottom_connectors=bottom,
        collision_boxes=merge_cell_boxes(cells),
        confident=confident,
        notes=tuple(notes),
    )


def _unconfident_bounding_draft(
    code: str,
    source_path: Path,
    faces: tuple[_FaceBox, ...],
    notes: list[str],
) -> InferredGeometry:
    """Fallback: one conservative cell block over the whole face bounds."""
    min_x = min(face.min_x for face in faces)
    max_x = max(face.max_x for face in faces)
    min_z = min(face.min_z for face in faces)
    max_z = max(face.max_z for face in faces)
    top_y = min(face.min_y for face in faces)
    bottom_y = max(face.max_y for face in faces)
    length = max(1, -(-round(max_x - min_x) // _STUD))
    width = max(1, -(-round(max_z - min_z) // _STUD))
    height = max(1, -(-round(bottom_y - top_y) // _PLATE))
    cells = frozenset(
        (dx, dy, dz)
        for dx in range(length)
        for dy in range(width)
        for dz in range(height)
    )
    return InferredGeometry(
        part_id=code,
        source_path=source_path,
        occupied_cells=cells,
        height_plates=height,
        origin_offset=(0.0, 0.0, 0.0),
        top_connectors=(),
        bottom_connectors=(),
        collision_boxes=merge_cell_boxes(cells),
        confident=False,
        notes=tuple(notes),
    )


def _occupied_cells(
    lattice: _Lattice,
    faces: tuple[_FaceBox, ...],
) -> frozenset[Cell]:
    """Mark cells overlapped by face areas, filled per-column vertically."""
    cells: set[Cell] = set()
    for dx in range(lattice.length_studs):
        for dy in range(lattice.width_studs):
            center_x, center_z = lattice.column_center(dx, dy)
            overlapping = [
                face
                for face in faces
                if _overlap(face.min_x, face.max_x, center_x) > _OVERLAP_EPSILON_LDU
                and _overlap(face.min_z, face.max_z, center_z) > _OVERLAP_EPSILON_LDU
            ]
            if not overlapping:
                continue
            column_top = min(face.min_y for face in overlapping)
            column_bottom = max(face.max_y for face in overlapping)
            z_lo = max(0, round((lattice.bottom_y - column_bottom) / PLATE_LDU))
            z_hi = min(
                lattice.height_plates,
                max(z_lo + 1, round((lattice.bottom_y - column_top) / PLATE_LDU)),
            )
            cells.update((dx, dy, dz) for dz in range(z_lo, z_hi))
    return frozenset(cells)


def _overlap(low: float, high: float, center: float) -> float:
    """Positive overlap length of ``[low, high]`` with the cell interval."""
    return min(high, center + _HALF_STUD) - max(low, center - _HALF_STUD)


def _origin_offset(
    lattice: _Lattice,
    cells: frozenset[Cell],
) -> tuple[float, float, float]:
    """Solve the LDraw origin offset the catalog decoder expects.

    A placed part's origin is emitted at the footprint centroid
    horizontally and the body top vertically; the offset is whatever the
    ``.dat`` frame adds beyond that convention.
    """
    columns = sorted({(dx, dy) for dx, dy, _ in cells})
    mean_dx = sum(dx for dx, _ in columns) / len(columns)
    mean_dy = sum(dy for _, dy in columns) / len(columns)
    anchor_x, anchor_z = lattice.column_center(0, 0)
    offset_x = STUD_LDU * (0 - mean_dx) - anchor_x
    offset_z = STUD_LDU * (0 - mean_dy) - anchor_z
    return (
        round(offset_x, 6) + 0.0,
        round(-lattice.top_y, 6) + 0.0,
        round(offset_z, 6) + 0.0,
    )


def _column_for(
    lattice: _Lattice,
    x: float,
    z: float,
) -> tuple[int, int] | None:
    """Map a local point to its grid column when it sits on a center."""
    dx, x_error = _snap(x - lattice.min_x - _HALF_STUD, STUD_LDU)
    dy, z_error = _snap(z - lattice.min_z - _HALF_STUD, STUD_LDU)
    if max(x_error, z_error) > _SNAP_TOLERANCE_LDU:
        return None
    if 0 <= dx < lattice.length_studs and 0 <= dy < lattice.width_studs:
        return (dx, dy)
    return None


def _top_connectors(
    lattice: _Lattice,
    expanded: PartGeometry,
    cells: frozenset[Cell],
    notes: list[str],
) -> tuple[tuple[InferredConnector, ...], int]:
    """Read stud top connectors from ``stud*`` primitive references.

    Returns the connectors plus how many stud references had to be
    dropped for sitting off the lattice or outside occupied cells.
    """
    connectors: dict[Cell, InferredConnector] = {}
    dropped = 0
    top_layer = lattice.height_plates - 1
    for stud in expanded.top_studs:
        column = _column_for(lattice, float(stud.position.x), float(stud.position.z))
        on_top = abs(float(stud.position.y) - lattice.top_y) <= _SNAP_TOLERANCE_LDU
        cell = (column[0], column[1], top_layer) if column is not None else None
        if cell is None or not on_top or cell not in cells:
            dropped += 1
            notes.append(
                f"stud at ({stud.position.x:g}, {stud.position.y:g}, "
                f"{stud.position.z:g}) is off the connector lattice; dropped"
            )
            continue
        connectors[cell] = InferredConnector(
            cell=cell,
            direction=UP,
            confidence=_STUD_CONFIDENCE,
            basis=f"{stud.name} primitive reference",
        )
    ordered = tuple(sorted(connectors.values(), key=lambda item: item.cell))
    return ordered, dropped


def _bottom_connectors(
    lattice: _Lattice,
    expanded: PartGeometry,
    cells: frozenset[Cell],
) -> tuple[InferredConnector, ...]:
    """Infer anti-studs for every column open at the part's bottom plane."""
    socket_columns = {
        column
        for receptacle in expanded.receptacles
        if (
            column := _column_for(
                lattice,
                float(receptacle.position.x),
                float(receptacle.position.z),
            )
        )
        is not None
    }
    connectors: list[InferredConnector] = []
    for dx, dy in sorted({(dx, dy) for dx, dy, dz in cells if dz == 0}):
        socket = (dx, dy) in socket_columns
        connectors.append(
            InferredConnector(
                cell=(dx, dy, 0),
                direction=DOWN,
                confidence=_SOCKET_CONFIDENCE if socket else _OPEN_BOTTOM_CONFIDENCE,
                basis=(
                    "tube receptacle primitive"
                    if socket
                    else "open-bottom cell heuristic"
                ),
            )
        )
    return tuple(connectors)


def merge_cell_boxes(cells: frozenset[Cell]) -> tuple[LduBox, ...]:
    """Merge unit cells into maximal axis-aligned collision boxes."""
    remaining = set(cells)
    boxes: list[LduBox] = []
    while remaining:
        x0, y0, z0 = min(remaining)
        x1 = x0
        while (x1 + 1, y0, z0) in remaining:
            x1 += 1
        y1 = y0
        while all((x, y1 + 1, z0) in remaining for x in range(x0, x1 + 1)):
            y1 += 1
        z1 = z0
        while all(
            (x, y, z1 + 1) in remaining
            for x in range(x0, x1 + 1)
            for y in range(y0, y1 + 1)
        ):
            z1 += 1
        remaining.difference_update(
            (x, y, z)
            for x in range(x0, x1 + 1)
            for y in range(y0, y1 + 1)
            for z in range(z0, z1 + 1)
        )
        boxes.append(
            LduBox(
                minimum=(_STUD * x0 - _HALF_STUD, _STUD * y0 - _HALF_STUD, _PLATE * z0),
                maximum=(
                    _STUD * x1 + _HALF_STUD,
                    _STUD * y1 + _HALF_STUD,
                    _PLATE * (z1 + 1),
                ),
            )
        )
    return tuple(boxes)
