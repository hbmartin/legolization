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
import math
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Any, Self, cast

from legolization.ldraw_units import GRID_TOLERANCE_LDU, PLATE_LDU, STUD_LDU
from legolization.physical import (
    LduBox,
    LduConnector,
    box_for_cell,
    physical_connector,
    placement_offset,
)

Cell = tuple[int, int, int]
"""A unit grid cell offset ``(dx, dy, dz)``; ``dz`` counts plate heights."""

UP: Cell = (0, 0, 1)
DOWN: Cell = (0, 0, -1)

DEFAULT_CATALOG_PATH = Path(__file__).parent / "data" / "catalog.json"
CATALOG_SCHEMA = 2

_FULL_YAWS = (0, 90, 180, 270)


class Category(StrEnum):
    """Coarse part family; drives connector generation and placement rules."""

    BRICK = "brick"
    PLATE = "plate"
    TILE = "tile"
    SLOPE = "slope"
    SNOT = "snot"
    """Sideways parts; excluded from every rect tiler by category."""

    SPECIAL = "special"
    """Import-only stud-up geometry excluded from placement strategies."""

    SPECIAL_SNOT = "special_snot"
    """Import-only sideways geometry excluded from the SNOT finishing pass."""


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
    ...)`` per outward grid direction, pinned as raw LDraw transform data
    rather than composed through pyldraw3's rotation convention at runtime.
    Empty for non-cladding parts."""

    mount_offset_ldu: tuple[float, float, float] = (0.0, 0.0, 0.0)
    """Cladding origin offset in LDU along ``(outward, vertical,
    transverse)``; vertical is measured from ``-PLATE_LDU * layer``."""

    replaceable_geometry: bool = False
    """Whether repair may re-tile this otherwise special part's exact cells."""

    collision_boxes_ldu: tuple[LduBox, ...] = ()
    """Exact physical collision volumes; coarse target coverage stays separate."""

    top_connectors_ldu: tuple[LduConnector, ...] = ()
    bottom_connectors_ldu: tuple[LduConnector, ...] = ()

    def mount_matrix(self, outward: tuple[int, int]) -> tuple[int, ...] | None:
        """Return the pinned emission rotation for ``outward`` claddings."""
        for direction, rows in self.mount_matrices:
            if direction == outward:
                return rows
        return None

    def __post_init__(self) -> None:
        if not self.filled_cells:
            object.__setattr__(self, "filled_cells", self.occupied_cells)
        if not self.collision_boxes_ldu:
            object.__setattr__(
                self,
                "collision_boxes_ldu",
                tuple(box_for_cell(cell) for cell in sorted(self.occupied_cells)),
            )
        if not self.top_connectors_ldu:
            object.__setattr__(
                self,
                "top_connectors_ldu",
                tuple(physical_connector(item) for item in self.top_connectors),
            )
        if not self.bottom_connectors_ldu:
            object.__setattr__(
                self,
                "bottom_connectors_ldu",
                tuple(physical_connector(item) for item in self.bottom_connectors),
            )

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

    def collision_boxes_at(
        self,
        x: int,
        y: int,
        layer: int,
        yaw: int,
        *,
        offset_ldu: tuple[int, int, int] = (0, 0, 0),
    ) -> tuple[LduBox, ...]:
        """Return exact world-space collision volumes for a placement."""
        offset = placement_offset(x, y, layer, offset_ldu)
        return tuple(
            box.rotated_yaw(yaw).translated(offset) for box in self.collision_boxes_ldu
        )

    def physical_connectors_at(
        self,
        anchor: Cell,
        yaw: int,
        *,
        top: bool,
        offset_ldu: tuple[int, int, int] = (0, 0, 0),
    ) -> tuple[LduConnector, ...]:
        """Return exact world-space physical connectors."""
        source = self.top_connectors_ldu if top else self.bottom_connectors_ldu
        x, y, layer = anchor
        offset = placement_offset(x, y, layer, offset_ldu)
        return tuple(item.rotated_yaw(yaw).translated(offset) for item in source)


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
    collision_boxes, top_ldu, bottom_ldu = _physical_geometry(spec)
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
        collision_boxes_ldu=collision_boxes,
        top_connectors_ldu=top_ldu,
        bottom_connectors_ldu=bottom_ldu,
    )


def _slope_part(spec: dict[str, Any]) -> Part:
    """Expand a slope JSON spec.

    Slope specs list ``stud_cells`` (full-height columns carrying the studs)
    and ``slope_cells`` (columns under the sloped face). Slope columns occupy
    the full bounding box for collision but only their bottom plate counts as
    shape fill. The LDraw origin sits at the stud-cell centroid, with the
    slope descending toward smaller local ``dy``.
    """
    # Declarative schema expansion is intentionally colocated for auditability.
    # lizard forgives(cyclomatic_complexity)
    height = int(spec["height_plates"])
    stud_cols = [tuple(int(v) for v in c) for c in spec["stud_cells"]]
    slope_cols = [tuple(int(v) for v in c) for c in spec["slope_cells"]]
    occupied = frozenset(
        (dx, dy, dz) for dx, dy in (*stud_cols, *slope_cols) for dz in range(height)
    )
    filled = frozenset(
        {(dx, dy, dz) for dx, dy in stud_cols for dz in range(height)}
        | {
            (dx, dy, height - 1 if spec.get("inverted", False) else 0)
            for dx, dy in slope_cols
        }
    )
    top = tuple(
        Connector(cell=(dx, dy, height - 1), direction=UP) for dx, dy in stud_cols
    )
    columns = [*stud_cols, *slope_cols]
    bottom_columns = stud_cols if spec.get("inverted", False) else columns
    bottom = tuple(
        Connector(cell=(dx, dy, 0), direction=DOWN) for dx, dy in bottom_columns
    )
    center_x = sum(dx for dx, _ in columns) / len(columns)
    center_y = sum(dy for _, dy in columns) / len(columns)
    stud_x = sum(dx for dx, _ in stud_cols) / len(stud_cols)
    stud_y = sum(dy for _, dy in stud_cols) / len(stud_cols)
    origin_offset = (20.0 * (stud_x - center_x), 0.0, 20.0 * (stud_y - center_y))
    collision_boxes, top_ldu, bottom_ldu = _physical_geometry(spec)
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
        collision_boxes_ldu=collision_boxes,
        top_connectors_ldu=top_ldu,
        bottom_connectors_ldu=bottom_ldu,
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


def _ldu_point(raw: list[int] | tuple[int, ...]) -> tuple[int, int, int]:
    """Validate one exact integer-LDU three-vector."""
    values = tuple(int(value) for value in raw)
    if len(values) != 3 or any(
        value != original for value, original in zip(values, raw, strict=True)
    ):
        msg = f"invalid integer-LDU point {raw!r}"
        raise ValueError(msg)
    return values


def _physical_geometry(
    spec: dict[str, Any],
) -> tuple[tuple[LduBox, ...], tuple[LduConnector, ...], tuple[LduConnector, ...]]:
    """Parse optional exact physical geometry from one catalog record."""
    boxes = tuple(
        LduBox(
            minimum=_ldu_point(entry["minimum"]),
            maximum=_ldu_point(entry["maximum"]),
        )
        for entry in spec.get("collision_boxes_ldu", ())
    )

    def connectors(name: str) -> tuple[LduConnector, ...]:
        return tuple(
            LduConnector(
                point=_ldu_point(entry["point"]),
                direction=_cell(entry["direction"]),
            )
            for entry in spec.get(name, ())
        )

    return (
        boxes,
        connectors("top_connectors_ldu"),
        connectors("bottom_connectors_ldu"),
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
    collision_boxes, top_ldu, bottom_ldu = _physical_geometry(spec)
    return Part(
        key=str(spec["key"]),
        ldraw_part=str(spec["ldraw_part"]),
        category=Category(str(spec["category"])),
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
        collision_boxes_ldu=collision_boxes,
        top_connectors_ldu=top_ldu,
        bottom_connectors_ldu=bottom_ldu,
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
    collision_boxes, top_ldu, bottom_ldu = _physical_geometry(spec)
    return Part(
        key=str(spec["key"]),
        ldraw_part=str(spec["ldraw_part"]),
        category=Category(str(spec["category"])),
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
        collision_boxes_ldu=collision_boxes,
        top_connectors_ldu=top_ldu,
        bottom_connectors_ldu=bottom_ldu,
    )


def _snot_part(spec: dict[str, Any]) -> Part:
    """Expand a sideways (SNOT) JSON spec by its declared ``snot_role``."""
    match spec.get("snot_role"):
        case "carrier":
            return _carrier_part(spec)
        case "cladding":
            return _cladding_part(spec)
        case "explicit":
            return _explicit_part(spec)
        case role:
            msg = f"unknown snot_role {role!r} for part spec {spec.get('key')!r}"
            raise ValueError(msg)


def _special_part(spec: dict[str, Any]) -> Part:
    """Expand import-only stud-up geometry from explicit footprint columns.

    Special parts participate in collision, connectivity, stability, and
    LDraw round-tripping, but their category keeps them out of the rectangular
    placement catalog.  This is useful for corner plates, legacy mould aliases,
    and other parts found in hand-authored models without making the voxel
    tilers choose them.
    """
    height = int(spec["height_plates"])
    if "size" in spec:
        width, length = (int(value) for value in spec["size"])
        columns = [(dx, dy) for dx in range(length) for dy in range(width)]
    else:
        columns = [(int(dx), int(dy)) for dx, dy in spec["occupied_columns"]]
    top_columns = [(int(dx), int(dy)) for dx, dy in spec.get("top_columns", columns)]
    bottom_columns = [
        (int(dx), int(dy)) for dx, dy in spec.get("bottom_columns", columns)
    ]
    offset_x, offset_y, offset_z = (
        float(value) for value in spec.get("origin_offset", (0.0, 0.0, 0.0))
    )
    collision_boxes, top_ldu, bottom_ldu = _physical_geometry(spec)
    return Part(
        key=str(spec["key"]),
        ldraw_part=str(spec["ldraw_part"]),
        category=Category.SPECIAL,
        occupied_cells=frozenset(
            (dx, dy, dz) for dx, dy in columns for dz in range(height)
        ),
        top_connectors=tuple(
            Connector(cell=(dx, dy, height - 1), direction=UP) for dx, dy in top_columns
        ),
        bottom_connectors=tuple(
            Connector(cell=(dx, dy, 0), direction=DOWN) for dx, dy in bottom_columns
        ),
        height_plates=height,
        mass_g=float(spec["mass_g"]),
        orientations=tuple(
            int(value) for value in spec.get("orientations", _FULL_YAWS)
        ),
        origin_offset=(offset_x, offset_y, offset_z),
        replaceable_geometry=cast(
            "bool",
            spec.get("replaceable_geometry", "size" in spec),
        ),
        collision_boxes_ldu=collision_boxes,
        top_connectors_ldu=top_ldu,
        bottom_connectors_ldu=bottom_ldu,
    )


def _explicit_part(spec: dict[str, Any]) -> Part:
    """Construct a custom non-rectangular part from trusted full metadata."""
    origin = _float_triple(spec["origin_offset"])
    collision_boxes, top_ldu, bottom_ldu = _physical_geometry(spec)
    return Part(
        key=str(spec["key"]),
        ldraw_part=str(spec["ldraw_part"]),
        category=Category(str(spec["category"])),
        occupied_cells=frozenset(_cell(cell) for cell in spec["occupied_cells"]),
        filled_cells=frozenset(_cell(cell) for cell in spec["filled_cells"]),
        top_connectors=_connectors(spec["top_connectors"]),
        bottom_connectors=_connectors(spec["bottom_connectors"]),
        height_plates=int(spec["height_plates"]),
        mass_g=float(spec["mass_g"]),
        orientations=tuple(int(value) for value in spec["orientations"]),
        origin_offset=origin,
        mount_normal=(
            _cell(spec["mount_normal"])
            if spec.get("mount_normal") is not None
            else None
        ),
        emit_yaw_offset=int(spec.get("emit_yaw_offset", 0)),
        mount_matrices=_parse_mount_matrices(spec.get("mount_matrices", {})),
        mount_offset_ldu=_float_triple(spec.get("mount_offset_ldu", (0.0, 0.0, 0.0))),
        replaceable_geometry=cast("bool", spec.get("replaceable_geometry", False)),
        collision_boxes_ldu=collision_boxes,
        top_connectors_ldu=top_ldu,
        bottom_connectors_ldu=bottom_ldu,
    )


def _float_triple(raw: list[float] | tuple[float, ...]) -> tuple[float, float, float]:
    """Convert one already schema-validated three-vector to floats."""
    x, y, z = (float(value) for value in raw)
    return (x, y, z)


def _parse_mount_matrices(
    raw: dict[str, list[int]],
) -> tuple[tuple[tuple[int, int], tuple[int, ...]], ...]:
    """Parse pinned cladding rotations from a JSON mapping."""
    return tuple(
        (
            (int(key.split(",")[0]), int(key.split(",")[1])),
            tuple(int(value) for value in values),
        )
        for key, values in sorted(raw.items())
    )


@dataclass(frozen=True, slots=True, eq=False)
class Catalog:
    """An immutable collection of :class:`Part` records keyed by ``key``.

    ``eq=False`` keeps identity hashing so cached methods can key on ``self``.
    """

    parts: dict[str, Part]

    @classmethod
    def load(
        cls,
        path: Path = DEFAULT_CATALOG_PATH,
        *,
        require_explicit_nonrect: bool = False,
    ) -> Self:
        """Load and validate one versioned catalog document."""
        specs = json.loads(path.read_text())
        if not isinstance(specs, dict) or specs.get("schema") != CATALOG_SCHEMA:
            msg = f"catalog {path} must declare schema {CATALOG_SCHEMA}"
            raise ValueError(msg)
        raw_parts = specs.get("parts")
        if not isinstance(raw_parts, list):
            msg = f"catalog {path} must contain a parts list"
            raise TypeError(msg)
        parts: dict[str, Part] = {}
        for index, raw_spec in enumerate(raw_parts, start=1):
            if not isinstance(raw_spec, dict):
                msg = f"catalog {path} part {index} must be an object"
                raise TypeError(msg)
            spec = cast("dict[str, Any]", raw_spec)
            _validate_part_spec(
                spec,
                path=path,
                index=index,
                require_explicit_nonrect=require_explicit_nonrect,
            )
            category = Category(str(spec["category"]))
            if require_explicit_nonrect and category not in _RECT_CATEGORIES:
                part = _explicit_part(spec)
            else:
                match category:
                    case Category.SLOPE:
                        part = _slope_part(spec)
                    case Category.SNOT | Category.SPECIAL_SNOT:
                        part = _snot_part(spec)
                    case Category.SPECIAL:
                        part = _special_part(spec)
                    case _:
                        part = _rect_part(spec)
            _validate_part(part, path=path, index=index)
            if part.key in parts:
                msg = f"catalog {path} contains duplicate key {part.key!r}"
                raise ValueError(msg)
            parts[part.key] = part
        catalog = cls(parts=parts)
        _validate_decode_ambiguity(catalog)
        return catalog

    def with_extensions(self, paths: tuple[Path, ...] | list[Path]) -> Self:
        """Return this catalog merged with non-overriding extension files."""
        if not paths:
            return self
        merged = dict(self.parts)
        for path in paths:
            extension = type(self).load(path, require_explicit_nonrect=True)
            duplicate = sorted(merged.keys() & extension.parts.keys())
            if duplicate:
                listed = ", ".join(duplicate)
                msg = f"catalog extension {path} duplicates keys: {listed}"
                raise ValueError(msg)
            merged.update(extension.parts)
        catalog = type(self)(parts=merged)
        _validate_decode_ambiguity(catalog)
        return catalog

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


def load_catalog(*extensions: Path) -> Catalog:
    """Load the built-in catalog plus optional versioned extensions."""
    catalog = default_catalog()
    return catalog if not extensions else catalog.with_extensions(extensions)


_RECT_CATEGORIES = frozenset((Category.BRICK, Category.PLATE, Category.TILE))
_EXPLICIT_GEOMETRY_FIELDS = frozenset(
    (
        "occupied_cells",
        "filled_cells",
        "top_connectors",
        "bottom_connectors",
        "orientations",
        "origin_offset",
        "height_plates",
    )
)


def _validate_part_spec(
    spec: dict[str, Any],
    *,
    path: Path,
    index: int,
    require_explicit_nonrect: bool,
) -> None:
    """Reject incomplete physical metadata before constructing a part."""
    required = {"key", "ldraw_part", "category", "mass_g"}
    missing = sorted(required - spec.keys())
    if missing:
        msg = f"catalog {path} part {index} is missing {', '.join(missing)}"
        raise ValueError(msg)
    category, mass_g = _category_and_mass(spec, path=path, index=index)
    if not math.isfinite(mass_g) or mass_g <= 0:
        msg = f"catalog {path} part {index} mass_g must be finite and positive"
        raise ValueError(msg)
    fields = _geometry_fields(
        spec,
        category=category,
        require_explicit_nonrect=require_explicit_nonrect,
    )
    missing_geometry = sorted(fields - spec.keys())
    if missing_geometry:
        msg = (
            f"catalog {path} part {index} is missing physical geometry "
            f"{', '.join(missing_geometry)}"
        )
        raise ValueError(msg)
    _validate_height(spec, path=path, index=index)
    _validate_replaceable_geometry(spec, path=path, index=index)
    if category in _RECT_CATEGORIES or (
        category is Category.SPECIAL and "size" in spec
    ):
        _validate_rect_size(spec, path=path, index=index)
    _validate_orientations(spec, path=path, index=index)


def _category_and_mass(
    spec: dict[str, Any],
    *,
    path: Path,
    index: int,
) -> tuple[Category, float]:
    try:
        return Category(str(spec["category"])), float(spec["mass_g"])
    except (TypeError, ValueError) as error:
        msg = f"catalog {path} part {index} has invalid category or mass"
        raise ValueError(msg) from error


def _geometry_fields(
    spec: dict[str, Any],
    *,
    category: Category,
    require_explicit_nonrect: bool,
) -> frozenset[str] | set[str]:
    if require_explicit_nonrect and category not in _RECT_CATEGORIES:
        return _EXPLICIT_GEOMETRY_FIELDS
    match category:
        case Category.BRICK | Category.PLATE | Category.TILE:
            return {"size", "height_plates"}
        case Category.SPECIAL:
            geometry_field = "size" if "size" in spec else "occupied_columns"
            return {geometry_field, "height_plates"}
        case Category.SLOPE:
            return {"stud_cells", "slope_cells", "height_plates"}
        case Category.SNOT | Category.SPECIAL_SNOT:
            return {"snot_role", "height_plates"}
        case _:
            return _EXPLICIT_GEOMETRY_FIELDS


def _validate_height(spec: dict[str, Any], *, path: Path, index: int) -> None:
    try:
        height = int(spec["height_plates"])
    except (TypeError, ValueError) as error:
        msg = f"catalog {path} part {index} has invalid height_plates"
        raise ValueError(msg) from error
    if height <= 0 or height != spec["height_plates"]:
        msg = f"catalog {path} part {index} height_plates must be a positive integer"
        raise ValueError(msg)


def _validate_replaceable_geometry(
    spec: dict[str, Any],
    *,
    path: Path,
    index: int,
) -> None:
    if "replaceable_geometry" in spec and not isinstance(
        spec["replaceable_geometry"],
        bool,
    ):
        msg = f"catalog {path} part {index} replaceable_geometry must be boolean"
        raise ValueError(msg)


def _validate_rect_size(spec: dict[str, Any], *, path: Path, index: int) -> None:
    try:
        size = tuple(int(value) for value in spec["size"])
    except (TypeError, ValueError) as error:
        msg = f"catalog {path} part {index} has malformed rectangular size"
        raise ValueError(msg) from error
    if len(size) != 2 or min(size, default=0) <= 0:
        msg = f"catalog {path} part {index} size must contain two positive integers"
        raise ValueError(msg)


def _validate_orientations(
    spec: dict[str, Any],
    *,
    path: Path,
    index: int,
) -> None:
    orientations = spec.get("orientations", _FULL_YAWS)
    try:
        parsed_orientations = tuple(int(value) for value in orientations)
    except (TypeError, ValueError) as error:
        msg = f"catalog {path} part {index} has malformed orientations"
        raise ValueError(msg) from error
    if (
        not parsed_orientations
        or len(parsed_orientations) != len(set(parsed_orientations))
        or any(value not in _FULL_YAWS for value in parsed_orientations)
    ):
        msg = f"catalog {path} part {index} has unsupported rotations"
        raise ValueError(msg)


def _validate_part(part: Part, *, path: Path, index: int) -> None:
    """Validate expanded cells, connectors, and origin metadata."""
    # Fail-fast schema validation keeps every malformed-field diagnostic local.
    # lizard forgives(cyclomatic_complexity)
    label = f"catalog {path} part {index} ({part.key})"
    if not part.key or not part.ldraw_part:
        msg = f"{label} must have non-empty key and ldraw_part values"
        raise ValueError(msg)
    if not part.occupied_cells or not part.filled_cells <= part.occupied_cells:
        msg = f"{label} has invalid occupied/filled cell geometry"
        raise ValueError(msg)
    if any(
        cell[2] < 0 or cell[2] >= part.height_plates for cell in part.occupied_cells
    ):
        msg = f"{label} has cells outside height_plates"
        raise ValueError(msg)
    if any(not math.isfinite(value) for value in part.origin_offset):
        msg = f"{label} has a non-finite LDraw origin offset"
        raise ValueError(msg)
    for connector in (*part.top_connectors, *part.bottom_connectors):
        if connector.cell not in part.occupied_cells:
            msg = f"{label} has a connector outside occupied geometry"
            raise ValueError(msg)
        if sum(abs(value) for value in connector.direction) != 1:
            msg = f"{label} has a malformed connector direction"
            raise ValueError(msg)
    _validate_physical_connectors(part, label=label)
    for _, matrix in part.mount_matrices:
        if len(matrix) != 9 or any(value not in (-1, 0, 1) for value in matrix):
            msg = f"{label} has a malformed LDraw mount matrix"
            raise ValueError(msg)


def _validate_physical_connectors(part: Part, *, label: str) -> None:
    """Validate exact connector axes independently of coarse coverage."""
    for connector in (*part.top_connectors_ldu, *part.bottom_connectors_ldu):
        if sum(abs(value) for value in connector.direction) != 1:
            msg = f"{label} has a malformed physical connector direction"
            raise ValueError(msg)


_YAW_ROTATIONS = (
    (1, 0, 0, 0, 1, 0, 0, 0, 1),
    (0, 0, -1, 0, 1, 0, 1, 0, 0),
    (-1, 0, 0, 0, 1, 0, 0, 0, -1),
    (0, 0, 1, 0, 1, 0, -1, 0, 0),
)


def _decode_cosets(
    part: Part,
) -> frozenset[tuple[tuple[int, ...], tuple[float, float, float]]]:
    """Return accepted rotation and grid-lattice position cosets."""
    entries: set[tuple[tuple[int, ...], tuple[float, float, float]]] = set()
    if part.mount_normal is not None:
        for outward, matrix in part.mount_matrices:
            if (yaw := _yaw_for_mount(part, outward)) is None:
                continue
            ox, oy = outward
            across_x, across_y = -oy, ox
            offset_out, offset_up, offset_across = part.mount_offset_ldu
            columns = [rotate_offset((*cell, 0), yaw) for cell in part.footprint]
            mean_x = sum(x for x, _, _ in columns) / len(columns)
            mean_y = sum(y for _, y, _ in columns) / len(columns)
            position = (
                STUD_LDU * mean_x + offset_out * ox + offset_across * across_x,
                offset_up,
                STUD_LDU * mean_y + offset_out * oy + offset_across * across_y,
            )
            entries.add((matrix, _position_coset(position)))
        return frozenset(entries)
    for yaw in _FULL_YAWS:
        emitted_yaw = (yaw + part.emit_yaw_offset) % 360
        columns = [rotate_offset((*cell, 0), yaw) for cell in part.footprint]
        mean_x = sum(x for x, _, _ in columns) / len(columns)
        mean_y = sum(y for _, y, _ in columns) / len(columns)
        offset_x, offset_y, offset_z = part.origin_offset
        # Preserve sub-LDU offsets while rotating the exact floating values.
        match yaw:
            case 0:
                rotated_x_f, rotated_z_f = offset_x, offset_z
            case 90:
                rotated_x_f, rotated_z_f = -offset_z, offset_x
            case 180:
                rotated_x_f, rotated_z_f = -offset_x, -offset_z
            case 270:
                rotated_x_f, rotated_z_f = offset_z, -offset_x
        position = (
            STUD_LDU * mean_x + rotated_x_f,
            -PLATE_LDU * part.height_plates + offset_y,
            STUD_LDU * mean_y + rotated_z_f,
        )
        entries.add((_YAW_ROTATIONS[emitted_yaw // 90], _position_coset(position)))
    return frozenset(entries)


def _position_coset(position: tuple[float, float, float]) -> tuple[float, float, float]:
    """Reduce an LDraw origin modulo the stud/plate import lattice."""
    x, y, z = position
    return (
        round(x % STUD_LDU, 8),
        round(y % PLATE_LDU, 8),
        round(z % STUD_LDU, 8),
    )


def _cosets_overlap(
    first: tuple[float, float, float],
    second: tuple[float, float, float],
) -> bool:
    """Whether two decoder tolerance windows overlap on the LDraw lattice."""
    periods = (STUD_LDU, PLATE_LDU, STUD_LDU)
    return all(
        _axis_windows_overlap(a, b, period)
        for a, b, period in zip(first, second, periods, strict=True)
    )


def _axis_windows_overlap(first: float, second: float, period: float) -> bool:
    distance = _cyclic_distance(first, second, period)
    limit = 2.0 * GRID_TOLERANCE_LDU
    return distance <= limit or math.isclose(distance, limit, abs_tol=1e-9)


def _cyclic_distance(first: float, second: float, period: float) -> float:
    distance = abs(first - second) % period
    return min(distance, period - distance)


def _yaw_for_mount(part: Part, outward: tuple[int, int]) -> int | None:
    """Resolve the logical placement yaw for one cladding outward normal."""
    if part.mount_normal is None:
        return None
    target = (-outward[0], -outward[1], part.mount_normal[2])
    return next(
        (
            yaw
            for yaw in part.orientations
            if rotate_offset(part.mount_normal, yaw) == target
        ),
        None,
    )


def _validate_decode_ambiguity(catalog: Catalog) -> None:
    """Reject shared-code parts with overlapping tolerant decode windows."""
    by_code: dict[str, list[Part]] = {}
    for part in catalog.parts.values():
        by_code.setdefault(part.ldraw_part.casefold(), []).append(part)
    for parts in by_code.values():
        decoded = [(part, _decode_cosets(part)) for part in parts]
        for (part, first_cosets), (other, other_cosets) in combinations(decoded, 2):
            ambiguous = any(
                first_matrix == other_matrix
                and _cosets_overlap(first_position, other_position)
                for first_matrix, first_position in first_cosets
                for other_matrix, other_position in other_cosets
            )
            if ambiguous:
                msg = (
                    f"catalog parts {part.key!r} and {other.key!r} have "
                    f"ambiguous LDraw decode metadata"
                )
                raise ValueError(msg)
