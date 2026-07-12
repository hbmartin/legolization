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
class VoxelGrid:
    """An ``(nx, ny, nlayers)`` grid of LDraw colour codes; ``-1`` = empty."""

    codes: np.ndarray

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
        return type(self)(codes=codes.astype(np.int16))

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
            codes = values.astype(np.int16)
            palette = palette or default_palette()
            unknown = np.unique(
                codes[(codes != EMPTY) & ~np.isin(codes, palette.codes)]
            )
            if unknown.size:
                listed = ", ".join(str(code) for code in unknown)
                msg = f"unknown LDraw colour codes in input: {listed}"
                raise ValueError(msg)
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
        """Load a MagicaVoxel ``.vox`` file (first model only).

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


def _parse_vox(data: bytes) -> tuple[tuple[int, int, int], np.ndarray, np.ndarray]:
    """Parse the first model of a ``.vox`` file → (size, xyzi rows, palette)."""
    if data[:4] != _VOX_MAGIC:
        msg = "not a MagicaVoxel file (missing 'VOX ' magic)"
        raise ValueError(msg)
    size: tuple[int, int, int] | None = None
    voxels: np.ndarray | None = None
    vox_palette: np.ndarray | None = None
    offset = 8  # skip magic + version
    try:
        while offset + 12 <= len(data):
            chunk_id = data[offset : offset + 4]
            content_len, _children_len = struct.unpack_from("<ii", data, offset + 4)
            if content_len < 0:
                msg = "negative chunk length"
                raise ValueError(msg)  # noqa: TRY301 - rewrapped below
            content = data[offset + 12 : offset + 12 + content_len]
            match chunk_id:
                case b"SIZE" if size is None:
                    sx, sy, sz = struct.unpack_from("<iii", content)
                    size = (sx, sy, sz)
                case b"XYZI" if voxels is None:
                    (count,) = struct.unpack_from("<i", content)
                    voxels = np.frombuffer(
                        content, dtype=np.uint8, count=count * 4, offset=4
                    ).reshape(-1, 4)
                case b"RGBA":
                    # Palette index i (1-based in XYZI) sits at position i-1.
                    raw = np.frombuffer(content, dtype=np.uint8, count=256 * 4)
                    vox_palette = np.zeros((257, 4), dtype=np.uint8)
                    vox_palette[1:] = raw.reshape(256, 4)
                case _:
                    pass
            offset += 12 + content_len
    except (struct.error, ValueError) as error:
        msg = f"malformed .vox file: {error}"
        raise ValueError(msg) from error
    if size is None or voxels is None:
        msg = "malformed .vox file: missing SIZE or XYZI chunk"
        raise ValueError(msg)
    if vox_palette is None:
        msg = (
            ".vox file has no RGBA palette chunk; re-export it with a palette "
            "or convert to .npy"
        )
        raise ValueError(msg)
    return size, voxels.astype(np.int64), vox_palette
