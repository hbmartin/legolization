"""Part abstraction and the JSON-seeded part catalog.

Every piece type (brick, plate, tile, slope) is one :class:`Part` record so
cuboid and non-cuboid parts flow through placement, connectivity, and physics
identically. Connectors carry a direction vector (stud-up only in v1), so
enabling SNOT later is a data change, not a rewrite.

Grid conventions
----------------
Local part cells are ``(dx, dy, dz)`` with ``dz`` in plate units (plate = 1,
brick = 3). Footprints are defined with their long axis along ``dx``. Yaw
rotates counterclockwise in the x-y plane (viewed from above) in steps of 90°.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Self

Cell = tuple[int, int, int]
"""A unit grid cell offset ``(dx, dy, dz)``; ``dz`` counts plate heights."""

UP: Cell = (0, 0, 1)
DOWN: Cell = (0, 0, -1)

DEFAULT_CATALOG_PATH = Path(__file__).parent / "data" / "catalog.json"

_FULL_YAWS = (0, 90, 180, 270)


class Category(StrEnum):
    """Coarse part family; drives connector generation and placement rules."""

    BRICK = "brick"
    PLATE = "plate"
    TILE = "tile"
    SLOPE = "slope"
    SNOT = "snot"
    """Sideways parts; excluded from every rect tiler by category."""


@dataclass(frozen=True, slots=True)
class Connector:
    """A stud or anti-stud at ``cell``, mating along ``direction``."""

    cell: Cell
    direction: Cell


def rotate_offset(cell: Cell, yaw: int) -> Cell:
    """Rotate a local cell offset counterclockwise by ``yaw`` degrees."""
    dx, dy, dz = cell
    match yaw % 360:
        case 0:
            return (dx, dy, dz)
        case 90:
            return (-dy, dx, dz)
        case 180:
            return (-dx, -dy, dz)
        case 270:
            return (dy, -dx, dz)
        case _:
            msg = f"yaw must be a multiple of 90, got {yaw}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Part:
    """One placeable piece type; geometry in unit cells, physics in grams."""

    key: str
    ldraw_part: str
    category: Category
    occupied_cells: frozenset[Cell]
    top_connectors: tuple[Connector, ...]
    bottom_connectors: tuple[Connector, ...]
    height_plates: int
    mass_g: float
    orientations: tuple[int, ...] = _FULL_YAWS
    origin_offset: tuple[float, float, float] = (0.0, 0.0, 0.0)
    filled_cells: frozenset[Cell] = field(default=frozenset())
    """Cells the part contributes to the target shape; for cuboid parts this
    equals ``occupied_cells``, for slopes it excludes the sloped void."""

    mount_normal: Cell | None = None
    """Local stud-axis direction for sideways-mounted parts (LDraw
    orientation only); None for ordinary stud-up parts."""

    emit_yaw_offset: int = 0
    """Extra LDraw yaw composed with the placement yaw at emission —
    carriers whose .dat side-stud axis differs from the modelled local
    lateral direction (87087's stud points LDraw -Z; +90 lands it on
    local +x)."""

    mount_matrices: tuple[tuple[tuple[int, int], tuple[int, ...]], ...] = ()
    """Cladding emission rotations: ``((outward_xy, 9 row-major ints),
    ...)`` per outward grid direction, pinned as catalog data (probed
    against pyldraw3's rotation sign convention, not composed at
    runtime). Empty for non-cladding parts."""

    mount_offset_ldu: tuple[float, float, float] = (0.0, 0.0, 0.0)
    """Cladding origin offset in LDU along ``(outward, vertical,
    transverse)``; vertical is measured from ``-PLATE_LDU * layer``."""

    def mount_matrix(self, outward: tuple[int, int]) -> tuple[int, ...] | None:
        """Return the pinned emission rotation for ``outward`` claddings."""
        for direction, rows in self.mount_matrices:
            if direction == outward:
                return rows
        return None

    def __post_init__(self) -> None:
        if not self.filled_cells:
            object.__setattr__(self, "filled_cells", self.occupied_cells)

    @property
    def footprint(self) -> frozenset[tuple[int, int]]:
        """The set of ``(dx, dy)`` columns this part covers."""
        return frozenset((dx, dy) for dx, dy, _ in self.occupied_cells)

    @property
    def stud_count(self) -> int:
        """Number of exposed studs on top."""
        return len(self.top_connectors)

    @property
    def cell_count(self) -> int:
        """Number of unit cells occupied."""
        return len(self.occupied_cells)

    def cells_at(self, x: int, y: int, layer: int, yaw: int) -> list[Cell]:
        """World cells occupied when placed at ``(x, y, layer)`` with ``yaw``."""
        return [
            (x + rx, y + ry, layer + rz)
            for rx, ry, rz in (rotate_offset(c, yaw) for c in self.occupied_cells)
        ]

    def filled_at(self, x: int, y: int, layer: int, yaw: int) -> list[Cell]:
        """World cells contributing to the target shape at this placement."""
        return [
            (x + rx, y + ry, layer + rz)
            for rx, ry, rz in (rotate_offset(c, yaw) for c in self.filled_cells)
        ]

    def connectors_at(
        self,
        x: int,
        y: int,
        layer: int,
        yaw: int,
        *,
        top: bool,
    ) -> list[Connector]:
        """World-space connectors (studs up / anti-studs down) at a placement."""
        source = self.top_connectors if top else self.bottom_connectors
        result: list[Connector] = []
        for conn in source:
            rx, ry, rz = rotate_offset(conn.cell, yaw)
            result.append(
                Connector(
                    cell=(x + rx, y + ry, layer + rz),
                    direction=rotate_offset(conn.direction, yaw),
                )
            )
        return result


def _rect_cells(width: int, length: int, height: int) -> frozenset[Cell]:
    return frozenset(
        (dx, dy, dz)
        for dx in range(length)
        for dy in range(width)
        for dz in range(height)
    )


def _rect_part(spec: dict[str, Any]) -> Part:
    """Expand a compact rectangular JSON spec into a full :class:`Part`."""
    category = Category(str(spec["category"]))
    width, length = (int(v) for v in spec["size"])
    height = int(spec["height_plates"])
    cells = _rect_cells(width=width, length=length, height=height)
    top: tuple[Connector, ...] = ()
    if category is not Category.TILE:
        top = tuple(
            Connector(cell=(dx, dy, height - 1), direction=UP)
            for dx in range(length)
            for dy in range(width)
        )
    bottom = tuple(
        Connector(cell=(dx, dy, 0), direction=DOWN)
        for dx in range(length)
        for dy in range(width)
    )
    # Square footprints only need two yaws; 1-wide squares need one.
    orientations = _FULL_YAWS if width != length else (0, 90)
    if width == length == 1:
        orientations = (0,)
    return Part(
        key=str(spec["key"]),
        ldraw_part=str(spec["ldraw_part"]),
        category=category,
        occupied_cells=cells,
        top_connectors=top,
        bottom_connectors=bottom,
        height_plates=height,
        mass_g=float(spec["mass_g"]),
        orientations=orientations,
    )


def _slope_part(spec: dict[str, Any]) -> Part:
    """Expand a slope JSON spec.

    Slope specs list ``stud_cells`` (full-height columns carrying the studs)
    and ``slope_cells`` (columns under the sloped face). Slope columns occupy
    the full bounding box for collision but only their bottom plate counts as
    shape fill. The LDraw origin sits at the stud-cell centroid, with the
    slope descending toward smaller local ``dy``.
    """
    height = int(spec["height_plates"])
    stud_cols = [tuple(int(v) for v in c) for c in spec["stud_cells"]]
    slope_cols = [tuple(int(v) for v in c) for c in spec["slope_cells"]]
    occupied = frozenset(
        (dx, dy, dz) for dx, dy in (*stud_cols, *slope_cols) for dz in range(height)
    )
    filled = frozenset(
        {(dx, dy, dz) for dx, dy in stud_cols for dz in range(height)}
        | {(dx, dy, 0) for dx, dy in slope_cols}
    )
    top = tuple(
        Connector(cell=(dx, dy, height - 1), direction=UP) for dx, dy in stud_cols
    )
    bottom = tuple(
        Connector(cell=(dx, dy, 0), direction=DOWN)
        for dx, dy in (*stud_cols, *slope_cols)
    )
    columns = [*stud_cols, *slope_cols]
    center_x = sum(dx for dx, _ in columns) / len(columns)
    center_y = sum(dy for _, dy in columns) / len(columns)
    stud_x = sum(dx for dx, _ in stud_cols) / len(stud_cols)
    stud_y = sum(dy for _, dy in stud_cols) / len(stud_cols)
    origin_offset = (20.0 * (stud_x - center_x), 0.0, 20.0 * (stud_y - center_y))
    return Part(
        key=str(spec["key"]),
        ldraw_part=str(spec["ldraw_part"]),
        category=Category.SLOPE,
        occupied_cells=occupied,
        top_connectors=top,
        bottom_connectors=bottom,
        height_plates=height,
        mass_g=float(spec["mass_g"]),
        orientations=_FULL_YAWS,
        origin_offset=origin_offset,
        filled_cells=filled,
    )


def _cell(raw: list[int] | tuple[int, ...]) -> Cell:
    """Validate a 3-int JSON cell."""
    x, y, z = (int(v) for v in raw)
    return (x, y, z)


def _connectors(raw: list[dict[str, Any]]) -> tuple[Connector, ...]:
    """Expand JSON ``{cell, direction}`` records."""
    return tuple(
        Connector(cell=_cell(entry["cell"]), direction=_cell(entry["direction"]))
        for entry in raw
    )


def _carrier_part(spec: dict[str, Any]) -> Part:
    """Expand a carrier spec: stud-up body plus lateral studs (87087, 11211).

    Structurally a normal brick — full rect body, top studs, bottom
    anti-studs, tiled around by carve-and-refill — plus the
    ``lateral_studs`` the catalog data declares. Lateral studs are
    appended after the top studs so existing connector indexing holds.
    """
    width, length = (int(v) for v in spec["size_studs"])
    height = int(spec["height_plates"])
    columns = [(dx, dy) for dy in range(width) for dx in range(length)]
    return Part(
        key=str(spec["key"]),
        ldraw_part=str(spec["ldraw_part"]),
        category=Category.SNOT,
        occupied_cells=frozenset(
            (dx, dy, dz) for dx, dy in columns for dz in range(height)
        ),
        top_connectors=(
            *(Connector(cell=(dx, dy, height - 1), direction=UP) for dx, dy in columns),
            *_connectors(spec["lateral_studs"]),
        ),
        bottom_connectors=tuple(
            Connector(cell=(dx, dy, 0), direction=DOWN) for dx, dy in columns
        ),
        height_plates=height,
        mass_g=float(spec["mass_g"]),
        orientations=_FULL_YAWS,
        emit_yaw_offset=int(spec.get("emit_yaw_offset", 0)),
    )


def _cladding_part(spec: dict[str, Any]) -> Part:
    """Expand a cladding spec: a sideways facade part (3070b, sideways 3069b).

    Occupies the full 3-plate window of every column it clads —
    conservative collision volume — with ``sockets`` pointing back along
    ``mount_normal`` toward the carrier studs and only the declared
    ``filled_cells`` counted as shape fill. Emission rotations per
    outward direction are pinned catalog data (``mount_matrices``), not
    runtime composition.
    """
    height = int(spec["height_plates"])
    columns = [(int(cx), int(cy)) for cx, cy in spec["window_columns"]]
    matrices = tuple(
        ((int(key.split(",")[0]), int(key.split(",")[1])), tuple(int(v) for v in rows))
        for key, rows in spec["mount_matrices"].items()
    )
    offset_out, offset_up, offset_across = (float(v) for v in spec["mount_offset_ldu"])
    return Part(
        key=str(spec["key"]),
        ldraw_part=str(spec["ldraw_part"]),
        category=Category.SNOT,
        occupied_cells=frozenset(
            (dx, dy, dz) for dx, dy in columns for dz in range(height)
        ),
        top_connectors=(),
        bottom_connectors=_connectors(spec["sockets"]),
        height_plates=height,
        mass_g=float(spec["mass_g"]),
        orientations=_FULL_YAWS,
        filled_cells=frozenset(_cell(cell) for cell in spec["filled_cells"]),
        mount_normal=_cell(spec["mount_normal"]),
        mount_matrices=matrices,
        mount_offset_ldu=(offset_out, offset_up, offset_across),
    )


def _snot_part(spec: dict[str, Any]) -> Part:
    """Expand a sideways (SNOT) JSON spec by its declared ``snot_role``."""
    match spec.get("snot_role"):
        case "carrier":
            return _carrier_part(spec)
        case "cladding":
            return _cladding_part(spec)
        case role:
            msg = f"unknown snot_role {role!r} for part spec {spec.get('key')!r}"
            raise ValueError(msg)


@dataclass(frozen=True, slots=True, eq=False)
class Catalog:
    """An immutable collection of :class:`Part` records keyed by ``key``.

    ``eq=False`` keeps identity hashing so cached methods can key on ``self``.
    """

    parts: dict[str, Part]

    @classmethod
    def load(cls, path: Path = DEFAULT_CATALOG_PATH) -> Self:
        """Load and expand the JSON seed catalog."""
        specs = json.loads(path.read_text())
        parts: dict[str, Part] = {}
        for spec in specs["parts"]:
            match spec["category"]:
                case Category.SLOPE:
                    part = _slope_part(spec)
                case Category.SNOT:
                    part = _snot_part(spec)
                case _:
                    part = _rect_part(spec)
            parts[part.key] = part
        return cls(parts=parts)

    def __getitem__(self, key: str) -> Part:
        return self.parts[key]

    def __contains__(self, key: str) -> bool:
        return key in self.parts

    def by_category(self, *categories: Category) -> list[Part]:
        """Parts in the given categories, largest cell count first."""
        return sorted(
            (p for p in self.parts.values() if p.category in categories),
            key=lambda p: (-p.cell_count, p.key),
        )

    def rect_key(
        self,
        width: int,
        length: int,
        height_plates: int,
        *,
        category: Category | None = None,
    ) -> str | None:
        """Look up the brick/plate part key for this footprint and height.

        ``width``/``length`` are order-insensitive; the caller picks the yaw.
        ``category`` restricts the lookup (e.g. to tiles for finishing).
        """
        small, big = sorted((width, length))
        if category is Category.TILE:
            return self._tile_index().get((small, big))
        key = self._rect_index().get((small, big, height_plates))
        if (
            key is not None
            and category is not None
            and self.parts[key].category is not category
        ):
            return None
        return key

    @lru_cache(maxsize=1)  # noqa: B019 - Catalog instances are long-lived
    def _rect_index(self) -> dict[tuple[int, int, int], str]:
        index: dict[tuple[int, int, int], str] = {}
        for part in self.by_category(Category.BRICK, Category.PLATE):
            width, length = _footprint_dims(part)
            index[(width, length, part.height_plates)] = part.key
        return index

    @lru_cache(maxsize=1)  # noqa: B019 - Catalog instances are long-lived
    def _tile_index(self) -> dict[tuple[int, int], str]:
        index: dict[tuple[int, int], str] = {}
        for part in self.by_category(Category.TILE):
            width, length = _footprint_dims(part)
            index[(width, length)] = part.key
        return index


def _footprint_dims(part: Part) -> tuple[int, int]:
    """Sorted (small, big) footprint extents of a rectangular part."""
    xs = [dx for dx, _ in part.footprint]
    ys = [dy for _, dy in part.footprint]
    dims = sorted((max(xs) - min(xs) + 1, max(ys) - min(ys) + 1))
    return (dims[0], dims[1])


@lru_cache(maxsize=1)
def default_catalog() -> Catalog:
    """Load (once) the catalog packaged in ``data/catalog.json``."""
    return Catalog.load()
