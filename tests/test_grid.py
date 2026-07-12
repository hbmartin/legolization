"""Voxel grid construction, masks, and file loaders."""

import struct

import numpy as np
import pytest

from legolization.grid import EMPTY, VoxelGrid


def test_from_int_codes():
    codes = np.full((2, 2, 2), EMPTY, dtype=np.int64)
    codes[0, 0, 0] = 4
    grid = VoxelGrid.from_array(codes, plates_per_voxel=3)
    assert grid.shape == (2, 2, 6)
    assert grid.code_at(0, 0, 0) == 4
    assert grid.code_at(0, 0, 2) == 4
    assert grid.code_at(0, 0, 3) == EMPTY
    assert grid.filled_count == 3


def test_from_rgba_alpha_marks_empty():
    values = np.zeros((1, 1, 2, 4), dtype=np.uint8)
    values[0, 0, 0] = (201, 26, 9, 255)
    values[0, 0, 1] = (0, 0, 0, 0)  # transparent = empty
    grid = VoxelGrid.from_array(values, plates_per_voxel=1)
    assert grid.code_at(0, 0, 0) == 4
    assert grid.code_at(0, 0, 1) == EMPTY


def test_bad_shape_rejected():
    with pytest.raises(ValueError, match="expected"):
        VoxelGrid.from_array(np.zeros((2, 2), dtype=np.int64))


def test_dither_mixes_gradient_colours():
    # A red-to-blue gradient bar: plain quantization gives solid bands,
    # dithering must interleave codes while covering the same cells.
    n = 16
    values = np.zeros((n, 2, 1, 4), dtype=np.uint8)
    for x in range(n):
        blend = x / (n - 1)
        values[x, :, 0] = (int(200 * (1 - blend)), 0, int(200 * blend), 255)
    plain = VoxelGrid.from_array(values, plates_per_voxel=1)
    dithered = VoxelGrid.from_array(values, plates_per_voxel=1, dither=True)
    assert (plain.filled_mask == dithered.filled_mask).all()
    assert not np.array_equal(plain.codes, dithered.codes)


def test_interior_and_surface_masks():
    codes = np.full((4, 4, 4), 7, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    interior = grid.interior_mask()
    assert interior.sum() == 2 * 2 * 2  # only the core survives erosion
    assert (grid.surface_mask() | interior).sum() == 64


def test_npy_roundtrip(tmp_path):
    codes = np.full((3, 2, 2), EMPTY, dtype=np.int16)
    codes[1, 1, 1] = 14
    path = tmp_path / "model.npy"
    np.save(path, codes)
    grid = VoxelGrid.from_npy(path, plates_per_voxel=1)
    assert grid.shape == (3, 2, 2)
    assert grid.code_at(1, 1, 1) == 14


def _vox_bytes() -> bytes:
    """Build a minimal one-voxel MagicaVoxel file with a red palette."""

    def chunk(cid: bytes, content: bytes, children: bytes = b"") -> bytes:
        return (
            cid + struct.pack("<ii", len(content), len(children)) + content + children
        )

    size = chunk(b"SIZE", struct.pack("<iii", 2, 1, 1))
    xyzi = chunk(b"XYZI", struct.pack("<i", 1) + bytes([1, 0, 0, 1]))
    palette = bytearray(256 * 4)
    palette[0:4] = bytes([201, 26, 9, 255])  # palette index 1
    rgba = chunk(b"RGBA", bytes(palette))
    main = chunk(b"MAIN", b"", size + xyzi + rgba)
    return b"VOX " + struct.pack("<i", 150) + main


def test_vox_loader(tmp_path):
    path = tmp_path / "model.vox"
    path.write_bytes(_vox_bytes())
    grid = VoxelGrid.from_vox(path, plates_per_voxel=3)
    assert grid.shape == (2, 1, 3)
    assert grid.code_at(1, 0, 0) == 4
    assert grid.code_at(0, 0, 0) == EMPTY


def test_vox_rejects_garbage(tmp_path):
    path = tmp_path / "bad.vox"
    path.write_bytes(b"NOTAVOXFILE")
    with pytest.raises(ValueError, match="magic"):
        VoxelGrid.from_vox(path)


def test_vox_rejects_truncated_chunk(tmp_path):
    path = tmp_path / "truncated.vox"
    path.write_bytes(_vox_bytes()[:-40])  # cut into the palette chunk
    with pytest.raises(ValueError, match=r"malformed|palette"):
        VoxelGrid.from_vox(path)


def test_vox_rejects_out_of_bounds_voxel(tmp_path):
    data = bytearray(_vox_bytes())
    # The single voxel sits at x=1 inside SIZE (2,1,1); move it to y=5.
    xyzi_at = data.find(b"XYZI") + 12 + 4
    data[xyzi_at + 1] = 5
    path = tmp_path / "oob.vox"
    path.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="outside SIZE"):
        VoxelGrid.from_vox(path)


def test_npy_rejects_unknown_colour_codes():
    codes = np.full((2, 1, 1), EMPTY, dtype=np.int64)
    codes[0, 0, 0] = 9999  # not an LDraw code
    with pytest.raises(ValueError, match="9999"):
        VoxelGrid.from_array(codes)


def test_integer_codes_are_validated_before_int16_narrowing():
    codes = np.array([[[65_540]]], dtype=np.uint64)  # would wrap to palette code 4
    with pytest.raises(ValueError, match="65540"):
        VoxelGrid.from_array(codes)


@pytest.mark.parametrize("axis_size", [0, 257])
def test_vox_rejects_unsupported_size_before_allocation(tmp_path, axis_size):
    data = bytearray(_vox_bytes())
    size_at = data.find(b"SIZE") + 12
    struct.pack_into("<i", data, size_at, axis_size)
    path = tmp_path / "bad-size.vox"
    path.write_bytes(data)

    with pytest.raises(ValueError, match="SIZE axes"):
        VoxelGrid.from_vox(path)


def test_aspect_correct_resampling():
    # 4 cubic voxels -> round(4 * 2.5) = 10 plate layers, column preserved.
    codes = np.full((1, 1, 4), EMPTY, dtype=np.int64)
    codes[0, 0, :2] = 4
    codes[0, 0, 2:] = 14
    grid = VoxelGrid.from_array(codes, aspect_correct=True)
    assert grid.shape == (1, 1, 10)
    column = [grid.code_at(0, 0, z) for z in range(10)]
    assert column == [4, 4, 4, 4, 4, 14, 14, 14, 14, 14]
