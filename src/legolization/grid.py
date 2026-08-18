"""Colored voxel grid: the pipeline's input representation.

A :class:`VoxelGrid` is a numpy int16 array of LDraw colour codes indexed
``(x, y, layer)`` with ``-1`` marking empty cells. ``layer`` counts **plate
heights** (plate = 1, brick = 3), z-up in model space. Loaders quantize
incoming RGB data to the LDraw palette immediately, so everything downstream
speaks LDraw colour codes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

import numpy as np
from scipy import ndimage

from legolization.color import Palette, default_palette

if TYPE_CHECKING:
    from pathlib import Path

EMPTY = -1
IGNORE = -2
"""Filled but colour-free: an invisible cell any brick colour may cover."""
_RGB_CHANNELS = 3
_RGBA_CHANNELS = 4
_VOX_MAGIC = b"VOX "
_ASPECT_PLATES_PER_VOXEL = 2.5  # 20 LDU stud pitch / 8 LDU plate height
_MAX_VOX_SCENE_DEPTH = 512

type Cell = tuple[int, int, int]


def colour_matches(a: int, b: int) -> bool:
    """Check two colour codes for compatibility (IGNORE matches anything)."""
    return a == b or IGNORE in (a, b)


def merge_colour(*codes: int) -> int | None:
    """Return the single colour compatible with all ``codes``, or None.

    All-IGNORE stays IGNORE; one specific colour wins over IGNORE; two
    different specific colours are incompatible.
    """
    specific = {code for code in codes if code != IGNORE}
    if not specific:
        return IGNORE
    if len(specific) == 1:
        return next(iter(specific))
    return None


# 6-connectivity: faces only, so diagonal contact does not count as interior.
_FACE_STRUCTURE = ndimage.generate_binary_structure(rank=3, connectivity=1)
# Axis-only erosion structures for anisotropic shells (see core_mask).
_Z_LINE = np.ones((1, 1, 3), dtype=bool)
_XY_CROSS = _FACE_STRUCTURE[:, :, 1:2]


@dataclass(frozen=True, slots=True)
class MeshFeatureAnnotations:
    """Deterministic mesh evidence attached to a voxelized target."""

    target_studs: int
    grid_phase: tuple[float, float, float]
    surface_error: float
    constructibility: float
    planar_region_count: int
    detail_candidates: tuple[Cell, ...]
    local_normals: tuple[tuple[Cell, tuple[float, float, float]], ...]


@dataclass(frozen=True, slots=True)
class VoxelGrid:
    """An ``(nx, ny, nlayers)`` grid of LDraw colour codes; ``-1`` = empty."""

    codes: np.ndarray
    mesh_features: MeshFeatureAnnotations | None = None

    def __post_init__(self) -> None:
        if self.codes.ndim != _RGB_CHANNELS:
            msg = f"grid must be 3-D (x, y, layer), got shape {self.codes.shape}"
            raise ValueError(msg)

    @property
    def shape(self) -> tuple[int, int, int]:
        """Grid extents ``(nx, ny, nlayers)``."""
        nx, ny, nz = self.codes.shape
        return (nx, ny, nz)

    @property
    def filled_mask(self) -> np.ndarray:
        """Boolean mask of non-empty cells."""
        return self.codes != EMPTY

    @property
    def filled_count(self) -> int:
        """Number of non-empty cells."""
        return int(np.count_nonzero(self.filled_mask))

    def count_filled_components(self) -> int:
        """Count face-connected filled islands with a full-grid labeling pass.

        Every stud-connected layout component lies within one such island, so
        this is a lower bound on the layout's component count. Thin islands
        that require multiple coplanar parts may not attain that bound.
        """
        _, count = ndimage.label(self.filled_mask, structure=_FACE_STRUCTURE)
        return int(count)

    def interior_mask(self) -> np.ndarray:
        """Cells whose six face-neighbours are all filled (erosion).

        This is the *invisibility* criterion (used for colour-free IGNORE
        labelling); for hollowing shells see :meth:`core_mask`.
        """
        return ndimage.binary_erosion(
            self.filled_mask,
            structure=_FACE_STRUCTURE,
            border_value=0,
        )

    def core_mask(self, *, margin_xy: int = 1, margin_z: int = 3) -> np.ndarray:
        """Cells deeper than the given shell margins (anisotropic erosion).

        The grid is anisotropic — cells are 1 stud x 1 stud x 1 plate — so
        "one brick of shell everywhere" means ``margin_xy=1`` stud and
        ``margin_z=3`` plates (a naive cubic erosion would leave walls three
        studs thick instead). Face-structure erosions run to the common
        margin, then axis-only erosions extend the deeper direction, so
        ``margin_xy == margin_z == 1`` reproduces :meth:`interior_mask`.
        """
        core = self.filled_mask
        common = min(margin_xy, margin_z)
        if common:
            core = ndimage.binary_erosion(
                core,
                structure=_FACE_STRUCTURE,
                iterations=common,
                border_value=0,
            )
        if margin_z > common:
            core = ndimage.binary_erosion(
                core,
                structure=_Z_LINE,
                iterations=margin_z - common,
                border_value=0,
            )
        if margin_xy > common:
            core = ndimage.binary_erosion(
                core,
                structure=_XY_CROSS,
                iterations=margin_xy - common,
                border_value=0,
            )
        return core

    def surface_mask(self) -> np.ndarray:
        """Mask filled cells with an empty (or boundary) face-neighbour."""
        return self.filled_mask & ~self.interior_mask()

    def code_at(self, x: int, y: int, layer: int) -> int:
        """Return the colour code at a cell, or ``EMPTY``."""
        return int(self.codes[x, y, layer])

    def with_codes(self, codes: np.ndarray) -> Self:
        """Return a copy of this grid with a replaced code array."""
        return type(self)(
            codes=codes.astype(np.int16),
            mesh_features=self.mesh_features,
        )

    @classmethod
    def from_array(
        cls,
        values: np.ndarray,
        *,
        plates_per_voxel: int = 3,
        palette: Palette | None = None,
        dither: bool = False,
        aspect_correct: bool = False,
    ) -> Self:
        """Build a grid from an int code array or an RGB(A) voxel array.

        Integer arrays are taken as LDraw colour codes (``-1`` empty) and
        validated against the palette. ``(x, y, z, 3|4)`` uint arrays are
        quantized to the LDraw palette; alpha 0 marks empty; ``dither``
        applies Floyd-Steinberg error diffusion per horizontal slice for
        smoother gradients. Each input voxel becomes ``plates_per_voxel``
        layers so cubic voxels map to brick heights; ``aspect_correct``
        instead resamples to 2.5 plates per voxel (a brick is 20 LDU wide
        but only 3 x 8 = 24 LDU tall, a 1.2x vertical stretch otherwise) —
        expect nearest-neighbour stair artifacts on sloped surfaces.
        """
        if values.ndim == _RGB_CHANNELS and np.issubdtype(values.dtype, np.integer):
            palette = palette or default_palette()
            unknown = np.unique(
                values[(values != EMPTY) & ~np.isin(values, palette.codes)]
            )
            if unknown.size:
                listed = ", ".join(str(code) for code in unknown)
                msg = f"unknown LDraw colour codes in input: {listed}"
                raise ValueError(msg)
            codes = values.astype(np.int16)
        elif values.ndim == _RGBA_CHANNELS and values.shape[-1] in (
            _RGB_CHANNELS,
            _RGBA_CHANNELS,
        ):
            palette = palette or default_palette()
            shape = values.shape[:_RGB_CHANNELS]
            rgb = values[..., :_RGB_CHANNELS].astype(np.float64)
            filled = (
                values[..., _RGB_CHANNELS] > 0
                if values.shape[-1] == _RGBA_CHANNELS
                else np.ones(shape, dtype=bool)
            )
            if dither:
                codes = _dither_slices(rgb, filled, palette)
            else:
                codes = (
                    palette.quantize(rgb.reshape(-1, _RGB_CHANNELS))
                    .reshape(shape)
                    .astype(np.int16)
                )
            codes[~filled] = EMPTY
        else:
            msg = (
                "expected an int (x, y, z) code array or a (x, y, z, 3|4) "
                f"RGB(A) array, got shape {values.shape} dtype {values.dtype}"
            )
            raise ValueError(msg)
        scale = _ASPECT_PLATES_PER_VOXEL if aspect_correct else float(plates_per_voxel)
        return cls(codes=_stretch_layers(codes, scale))

    @classmethod
    def from_npy(
        cls,
        path: Path,
        *,
        plates_per_voxel: int = 3,
        palette: Palette | None = None,
        dither: bool = False,
        aspect_correct: bool = False,
    ) -> Self:
        """Load a grid from a ``.npy`` file (codes or RGB(A) voxels)."""
        return cls.from_array(
            np.load(path),
            plates_per_voxel=plates_per_voxel,
            palette=palette,
            dither=dither,
            aspect_correct=aspect_correct,
        )

    @classmethod
    def from_vox(
        cls,
        path: Path,
        *,
        plates_per_voxel: int = 3,
        palette: Palette | None = None,
        dither: bool = False,
        aspect_correct: bool = False,
    ) -> Self:
        """Load a MagicaVoxel ``.vox`` file, composing its full scene graph.

        MagicaVoxel is z-up like the grid, so coordinates map directly.
        """
        size, voxels, vox_palette = _parse_vox(path.read_bytes())
        values = np.zeros((*size, _RGBA_CHANNELS), dtype=np.uint8)
        if voxels.size:
            if (voxels[:, :_RGB_CHANNELS] >= np.asarray(size)).any():
                msg = "malformed .vox file: voxel coordinates outside SIZE"
                raise ValueError(msg)
            rgba = vox_palette[voxels[:, 3]].copy()
            rgba[:, 3] = 255  # palette alpha is unreliable; XYZI presence rules
            values[voxels[:, 0], voxels[:, 1], voxels[:, 2]] = rgba
        return cls.from_array(
            values,
            plates_per_voxel=plates_per_voxel,
            palette=palette,
            dither=dither,
            aspect_correct=aspect_correct,
        )


_FS_KERNEL = (  # Floyd-Steinberg error-diffusion taps: (dx, dy, weight)
    (0, 1, 7 / 16),
    (1, -1, 3 / 16),
    (1, 0, 5 / 16),
    (1, 1, 1 / 16),
)


def _dither_slices(
    rgb: np.ndarray,
    filled: np.ndarray,
    palette: Palette,
) -> np.ndarray:
    """Floyd-Steinberg dither each horizontal slice against the palette.

    Error only diffuses into filled cells of the same slice, so gradients
    dissolve into plausible colour mixes without bleeding across empties.
    """
    nx, ny, nz = filled.shape
    codes = np.full((nx, ny, nz), EMPTY, dtype=np.int16)
    for z in range(nz):
        if not filled[:, :, z].any():
            continue
        work = rgb[:, :, z].copy()
        for x in range(nx):
            for y in range(ny):
                if not filled[x, y, z]:
                    continue
                old = work[x, y]
                code = int(palette.quantize(old[None, :])[0])
                codes[x, y, z] = code
                error = old - np.asarray(palette.rgb_of(code), dtype=np.float64)
                for dx, dy, weight in _FS_KERNEL:
                    px, py = x + dx, y + dy
                    if 0 <= px < nx and 0 <= py < ny and filled[px, py, z]:
                        work[px, py] = np.clip(
                            work[px, py] + error * weight, 0.0, 255.0
                        )
    return codes


def _stretch_layers(codes: np.ndarray, scale: float) -> np.ndarray:
    """Resample voxel layers to plate layers by a (fractional) z scale."""
    n_layers = round(codes.shape[2] * scale)
    index = np.minimum(
        (np.arange(n_layers) / scale).astype(np.int64),
        codes.shape[2] - 1,
    )
    return codes[:, :, index]


type _VoxMatrix = tuple[Cell, Cell, Cell]

_IDENTITY_VOX: _VoxMatrix = ((1, 0, 0), (0, 1, 0), (0, 0, 1))


@dataclass(frozen=True, slots=True)
class _VoxModel:
    size: tuple[int, int, int]
    voxels: np.ndarray


@dataclass(frozen=True, slots=True)
class _VoxTransform:
    child: int
    translation: Cell
    rotation: _VoxMatrix
    hidden: bool


@dataclass(frozen=True, slots=True)
class _VoxGroup:
    children: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _VoxShape:
    models: tuple[int, ...]


type _VoxNode = _VoxTransform | _VoxGroup | _VoxShape


@dataclass(frozen=True, slots=True)
class _VoxPose:
    rotation: _VoxMatrix
    translation: Cell
    ancestry: frozenset[int] = frozenset()


@dataclass(slots=True)
class _SceneWalk:
    nodes: dict[int, _VoxNode]
    placements: list[tuple[int, _VoxMatrix, Cell]]


def _parse_vox(data: bytes) -> tuple[tuple[int, int, int], np.ndarray, np.ndarray]:
    """Parse and compose a MagicaVoxel model or positioned multi-model scene."""
    if data[:4] != _VOX_MAGIC:
        msg = "not a MagicaVoxel file (missing 'VOX ' magic)"
        raise ValueError(msg)
    try:
        models, nodes, vox_palette = _parse_vox_chunks(data)
        size, voxels = _compose_vox_scene(models, nodes)
    except (
        IndexError,
        RecursionError,
        struct.error,
        UnicodeError,
        ValueError,
    ) as error:
        msg = f"malformed .vox file: {error}"
        raise ValueError(msg) from error
    return size, voxels.astype(np.int64), vox_palette


def _parse_vox_chunks(
    data: bytes,
) -> tuple[list[_VoxModel], dict[int, _VoxNode], np.ndarray]:
    models: list[_VoxModel] = []
    nodes: dict[int, _VoxNode] = {}
    palette: np.ndarray | None = None
    pending_size: tuple[int, int, int] | None = None
    offset = 8
    while offset + 12 <= len(data):
        chunk_id = data[offset : offset + 4]
        content_len, _children_len = struct.unpack_from("<ii", data, offset + 4)
        if content_len < 0 or offset + 12 + content_len > len(data):
            msg = "truncated or negative chunk length"
            raise ValueError(msg)
        content = data[offset + 12 : offset + 12 + content_len]
        match chunk_id:
            case b"SIZE":
                pending_size = _parse_vox_size(content)
            case b"XYZI":
                if pending_size is None:
                    msg = "XYZI chunk has no preceding SIZE"
                    raise ValueError(msg)
                models.append(_VoxModel(pending_size, _parse_voxels(content)))
                pending_size = None
            case b"RGBA":
                palette = _parse_vox_palette(content)
            case b"nTRN" | b"nGRP" | b"nSHP":
                node_id, node = _parse_vox_node(chunk_id, content)
                nodes[node_id] = node
            case _:
                pass
        offset += 12 + content_len
    if not models:
        msg = "missing SIZE or XYZI chunk"
        raise ValueError(msg)
    return models, nodes, palette if palette is not None else _magica_default_palette()


def _parse_vox_size(content: bytes) -> tuple[int, int, int]:
    sx, sy, sz = struct.unpack_from("<iii", content)
    return _validate_vox_size((sx, sy, sz))


def _parse_voxels(content: bytes) -> np.ndarray:
    (count,) = struct.unpack_from("<i", content)
    if count < 0 or len(content) != 4 + count * 4:
        msg = "XYZI count does not match its chunk length"
        raise ValueError(msg)
    return np.frombuffer(content, dtype=np.uint8, count=count * 4, offset=4).reshape(
        -1, 4
    )


def _parse_vox_palette(content: bytes) -> np.ndarray:
    if len(content) != 256 * 4:
        msg = "RGBA palette must contain 256 entries"
        raise ValueError(msg)
    palette = np.zeros((257, 4), dtype=np.uint8)
    palette[1:] = np.frombuffer(content, dtype=np.uint8).reshape(256, 4)
    return palette


def _parse_vox_node(chunk_id: bytes, content: bytes) -> tuple[int, _VoxNode]:
    cursor = 0
    node_id, cursor = _take_i32(content, cursor)
    attributes, cursor = _take_dict(content, cursor)
    match chunk_id:
        case b"nTRN":
            child, cursor = _take_i32(content, cursor)
            _, cursor = _take_i32(content, cursor)  # reserved id
            _, cursor = _take_i32(content, cursor)  # layer id
            frame_count, cursor = _take_i32(content, cursor)
            frames = []
            for _ in range(max(frame_count, 0)):
                frame, cursor = _take_dict(content, cursor)
                frames.append(frame)
            frame = frames[0] if frames else {}
            translation = _parse_translation(frame.get("_t", "0 0 0"))
            rotation = _decode_vox_rotation(int(frame.get("_r", "4")))
            return (
                node_id,
                _VoxTransform(
                    child=child,
                    translation=translation,
                    rotation=rotation,
                    hidden=attributes.get("_hidden") == "1",
                ),
            )
        case b"nGRP":
            count, cursor = _take_i32(content, cursor)
            children = tuple(
                _take_i32(content, cursor + 4 * index)[0] for index in range(count)
            )
            return (node_id, _VoxGroup(children=children))
        case b"nSHP":
            count, cursor = _take_i32(content, cursor)
            models: list[int] = []
            for _ in range(count):
                model, cursor = _take_i32(content, cursor)
                _, cursor = _take_dict(content, cursor)
                models.append(model)
            return (node_id, _VoxShape(models=tuple(models)))
        case _:
            raise AssertionError


def _take_i32(data: bytes, cursor: int) -> tuple[int, int]:
    return (struct.unpack_from("<i", data, cursor)[0], cursor + 4)


def _take_string(data: bytes, cursor: int) -> tuple[str, int]:
    length, cursor = _take_i32(data, cursor)
    if length < 0 or cursor + length > len(data):
        msg = "invalid scene string length"
        raise ValueError(msg)
    return (data[cursor : cursor + length].decode(), cursor + length)


def _take_dict(data: bytes, cursor: int) -> tuple[dict[str, str], int]:
    count, cursor = _take_i32(data, cursor)
    result: dict[str, str] = {}
    for _ in range(max(count, 0)):
        key, cursor = _take_string(data, cursor)
        value, cursor = _take_string(data, cursor)
        result[key] = value
    return (result, cursor)


def _parse_translation(value: str) -> Cell:
    coordinates = tuple(int(item) for item in value.split())
    if len(coordinates) != 3:
        msg = f"invalid scene translation {value!r}"
        raise ValueError(msg)
    return coordinates


def _decode_vox_rotation(value: int) -> _VoxMatrix:
    first_axis = value & 0x3
    second_axis = (value >> 2) & 0x3
    if first_axis > 2 or second_axis > 2 or first_axis == second_axis:
        msg = f"invalid scene rotation {value}"
        raise ValueError(msg)
    third_axis = 3 - first_axis - second_axis
    rows: list[Cell] = []
    for row, axis in enumerate((first_axis, second_axis, third_axis)):
        sign = -1 if value & (1 << (4 + row)) else 1
        values = [0, 0, 0]
        values[axis] = sign
        rows.append((values[0], values[1], values[2]))
    return (rows[0], rows[1], rows[2])


def _compose_vox_scene(
    models: list[_VoxModel],
    nodes: dict[int, _VoxNode],
) -> tuple[tuple[int, int, int], np.ndarray]:
    if not nodes:
        if len(models) != 1:
            msg = "multiple models lack usable scene placement"
            raise ValueError(msg)
        return (models[0].size, models[0].voxels)
    placements = _scene_placements(nodes)
    if not placements:
        msg = "scene graph contains no visible model placements"
        raise ValueError(msg)
    colours: dict[Cell, int] = {}
    for model_id, rotation, translation in placements:
        if not 0 <= model_id < len(models):
            msg = f"scene references missing model {model_id}"
            raise ValueError(msg)
        _place_vox_model(
            models[model_id],
            rotation=rotation,
            translation=translation,
            colours=colours,
        )
    if not colours:
        msg = "scene contains no voxels"
        raise ValueError(msg)
    minimum = tuple(min(cell[axis] for cell in colours) for axis in range(3))
    shifted = {
        (cell[0] - minimum[0], cell[1] - minimum[1], cell[2] - minimum[2]): colour
        for cell, colour in colours.items()
    }
    size = (
        max(cell[0] for cell in shifted) + 1,
        max(cell[1] for cell in shifted) + 1,
        max(cell[2] for cell in shifted) + 1,
    )
    voxels = np.asarray([(*cell, colour) for cell, colour in sorted(shifted.items())])
    return (size, voxels)


def _scene_placements(
    nodes: dict[int, _VoxNode],
) -> tuple[tuple[int, _VoxMatrix, Cell], ...]:
    referenced = {
        child
        for node in nodes.values()
        for child in (
            (node.child,)
            if isinstance(node, _VoxTransform)
            else node.children
            if isinstance(node, _VoxGroup)
            else ()
        )
    }
    roots = sorted(set(nodes) - referenced)
    placements: list[tuple[int, _VoxMatrix, Cell]] = []
    context = _SceneWalk(nodes=nodes, placements=placements)
    for root in roots:
        _walk_vox_scene(
            root,
            context=context,
            pose=_VoxPose(rotation=_IDENTITY_VOX, translation=(0, 0, 0)),
        )
    return tuple(placements)


def _walk_vox_scene(
    node_id: int,
    *,
    context: _SceneWalk,
    pose: _VoxPose,
) -> None:
    if len(pose.ancestry) >= _MAX_VOX_SCENE_DEPTH:
        msg = f"scene graph exceeds {_MAX_VOX_SCENE_DEPTH} nodes of depth"
        raise ValueError(msg)
    if node_id in pose.ancestry:
        msg = "scene graph contains a cycle"
        raise ValueError(msg)
    node = context.nodes.get(node_id)
    if node is None:
        msg = f"scene references missing node {node_id}"
        raise ValueError(msg)
    ancestry = pose.ancestry | {node_id}
    match node:
        case _VoxTransform() if not node.hidden:
            child_rotation = _multiply_matrix(pose.rotation, node.rotation)
            child_translation = _add_cell(
                _apply_matrix(pose.rotation, node.translation),
                pose.translation,
            )
            _walk_vox_scene(
                node.child,
                context=context,
                pose=_VoxPose(
                    rotation=child_rotation,
                    translation=child_translation,
                    ancestry=ancestry,
                ),
            )
        case _VoxGroup():
            for child in node.children:
                _walk_vox_scene(
                    child,
                    context=context,
                    pose=_VoxPose(
                        rotation=pose.rotation,
                        translation=pose.translation,
                        ancestry=ancestry,
                    ),
                )
        case _VoxShape():
            context.placements.extend(
                (model, pose.rotation, pose.translation) for model in node.models
            )
        case _:
            pass


def _place_vox_model(
    model: _VoxModel,
    *,
    rotation: _VoxMatrix,
    translation: Cell,
    colours: dict[Cell, int],
) -> None:
    if (
        model.voxels.size
        and (model.voxels[:, :_RGB_CHANNELS] >= np.asarray(model.size)).any()
    ):
        msg = "voxel coordinates outside SIZE"
        raise ValueError(msg)
    coordinates = model.voxels[:, :_RGB_CHANNELS].astype(np.int64)
    coordinates -= np.asarray(model.size, dtype=np.int64) // 2
    transformed = coordinates @ np.asarray(rotation, dtype=np.int64).T
    transformed += np.asarray(translation, dtype=np.int64)
    for row, colour in zip(transformed, model.voxels[:, 3], strict=True):
        position = (int(row[0]), int(row[1]), int(row[2]))
        value = int(colour)
        if (existing := colours.get(position)) is not None and existing != value:
            msg = f"conflicting scene colours at {position}"
            raise ValueError(msg)
        colours[position] = value


def _apply_matrix(matrix: _VoxMatrix, cell: Cell) -> Cell:
    return (
        _dot(matrix[0], cell),
        _dot(matrix[1], cell),
        _dot(matrix[2], cell),
    )


def _multiply_matrix(left: _VoxMatrix, right: _VoxMatrix) -> _VoxMatrix:
    columns = (
        (right[0][0], right[1][0], right[2][0]),
        (right[0][1], right[1][1], right[2][1]),
        (right[0][2], right[1][2], right[2][2]),
    )
    return (
        _product_row(left[0], columns),
        _product_row(left[1], columns),
        _product_row(left[2], columns),
    )


def _dot(left: Cell, right: Cell) -> int:
    return left[0] * right[0] + left[1] * right[1] + left[2] * right[2]


def _product_row(row: Cell, columns: _VoxMatrix) -> Cell:
    return (_dot(row, columns[0]), _dot(row, columns[1]), _dot(row, columns[2]))


def _add_cell(left: Cell, right: Cell) -> Cell:
    return (left[0] + right[0], left[1] + right[1], left[2] + right[2])


def _magica_default_palette() -> np.ndarray:
    """Return MagicaVoxel's documented implicit 255-colour RGBA palette."""
    levels = (255, 204, 153, 102, 51, 0)
    colours = [
        (red, green, blue, 255)
        for red in levels
        for green in levels
        for blue in levels
        if (red, green, blue) != (0, 0, 0)
    ]
    ramp = (238, 221, 187, 170, 136, 119, 85, 68, 34, 17)
    colours.extend((value, 0, 0, 255) for value in ramp)
    colours.extend((0, value, 0, 255) for value in ramp)
    colours.extend((0, 0, value, 255) for value in ramp)
    colours.extend((value, value, value, 255) for value in ramp)
    palette = np.zeros((257, 4), dtype=np.uint8)
    palette[1:256] = np.asarray(colours, dtype=np.uint8)
    return palette


def _validate_vox_size(size: tuple[int, int, int]) -> tuple[int, int, int]:
    if any(axis <= 0 or axis > 256 for axis in size):
        msg = "SIZE axes must be between 1 and 256"
        raise ValueError(msg)
    return size
