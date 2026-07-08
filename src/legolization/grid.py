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
_RGB_CHANNELS = 3
_RGBA_CHANNELS = 4
_VOX_MAGIC = b"VOX "

# 6-connectivity: faces only, so diagonal contact does not count as interior.
_FACE_STRUCTURE = ndimage.generate_binary_structure(rank=3, connectivity=1)


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
        """Cells whose six face-neighbours are all filled (erosion)."""
        return ndimage.binary_erosion(
            self.filled_mask,
            structure=_FACE_STRUCTURE,
            border_value=0,
        )

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
    ) -> Self:
        """Build a grid from an int code array or an RGB(A) voxel array.

        Integer arrays are taken as LDraw colour codes (``-1`` empty) and
        used as-is. ``(x, y, z, 3|4)`` uint arrays are quantized to the LDraw
        palette; alpha 0 marks empty. Each input voxel becomes
        ``plates_per_voxel`` layers so cubic voxels map to brick heights.
        """
        if values.ndim == _RGB_CHANNELS and np.issubdtype(values.dtype, np.integer):
            codes = values.astype(np.int16)
        elif values.ndim == _RGBA_CHANNELS and values.shape[-1] in (
            _RGB_CHANNELS,
            _RGBA_CHANNELS,
        ):
            palette = palette or default_palette()
            rgb = values[..., :_RGB_CHANNELS].reshape(-1, _RGB_CHANNELS)
            codes = palette.quantize(rgb).reshape(values.shape[:_RGB_CHANNELS])
            codes = codes.astype(np.int16)
            if values.shape[-1] == _RGBA_CHANNELS:
                codes[values[..., _RGB_CHANNELS] == 0] = EMPTY
        else:
            msg = (
                "expected an int (x, y, z) code array or a (x, y, z, 3|4) "
                f"RGB(A) array, got shape {values.shape} dtype {values.dtype}"
            )
            raise ValueError(msg)
        return cls(codes=np.repeat(codes, repeats=plates_per_voxel, axis=2))

    @classmethod
    def from_npy(
        cls,
        path: Path,
        *,
        plates_per_voxel: int = 3,
        palette: Palette | None = None,
    ) -> Self:
        """Load a grid from a ``.npy`` file (codes or RGB(A) voxels)."""
        return cls.from_array(
            np.load(path),
            plates_per_voxel=plates_per_voxel,
            palette=palette,
        )

    @classmethod
    def from_vox(
        cls,
        path: Path,
        *,
        plates_per_voxel: int = 3,
        palette: Palette | None = None,
    ) -> Self:
        """Load a MagicaVoxel ``.vox`` file (first model only).

        MagicaVoxel is z-up like the grid, so coordinates map directly.
        """
        size, voxels, vox_palette = _parse_vox(path.read_bytes())
        palette = palette or default_palette()
        codes = np.full(size, EMPTY, dtype=np.int16)
        if voxels.size:
            colour_codes = palette.quantize(
                vox_palette[voxels[:, 3], :_RGB_CHANNELS].astype(np.float64)
            )
            codes[voxels[:, 0], voxels[:, 1], voxels[:, 2]] = colour_codes
        return cls(codes=np.repeat(codes, repeats=plates_per_voxel, axis=2))


def _parse_vox(data: bytes) -> tuple[tuple[int, int, int], np.ndarray, np.ndarray]:
    """Parse the first model of a ``.vox`` file → (size, xyzi rows, palette)."""
    if data[:4] != _VOX_MAGIC:
        msg = "not a MagicaVoxel file (missing 'VOX ' magic)"
        raise ValueError(msg)
    size: tuple[int, int, int] | None = None
    voxels: np.ndarray | None = None
    vox_palette: np.ndarray | None = None
    offset = 8  # skip magic + version
    while offset + 12 <= len(data):
        chunk_id = data[offset : offset + 4]
        content_len, _children_len = struct.unpack_from("<ii", data, offset + 4)
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
                # Palette index i (1-based in XYZI) is stored at position i-1.
                raw = np.frombuffer(content, dtype=np.uint8, count=256 * 4)
                vox_palette = np.zeros((257, 4), dtype=np.uint8)
                vox_palette[1:] = raw.reshape(256, 4)
            case _:
                pass
        offset += 12 + content_len
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
