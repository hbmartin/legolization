"""Mesh front-end: triangle meshes (.obj/.stl/.ply) → :class:`VoxelGrid`.

The mesh is oriented z-up, pre-stretched 2.5x vertically (a stud pitch is
20 LDU but a plate is only 8 LDU tall), then surface-voxelized once at the
stud pitch and filled. Because the stretch happens *before* voxelization,
every plate layer samples the true mesh surface — no nearest-neighbour
layer replication — and mesh grids are always aspect-correct by
construction.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import numpy as np
import trimesh
from scipy import ndimage

from legolization.color import default_palette
from legolization.grid import EMPTY, VoxelGrid

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from legolization.color import Palette

MESH_SUFFIXES: frozenset[str] = frozenset({".obj", ".stl", ".ply"})
DEFAULT_MESH_COLOUR = 7  # light grey
_PLATES_PER_STUD = 2.5  # 20 LDU stud pitch / 8 LDU plate height
_MAX_GRID_DIM = 512
_MAX_GRID_CELLS = 16_000_000
_HORIZONTAL_AXES = 2
# 6-connectivity for the largest-component filter, matching grid semantics.
_FACE_STRUCTURE = ndimage.generate_binary_structure(rank=3, connectivity=1)


@dataclass(frozen=True, slots=True)
class MeshOptions:
    """Tunables for mesh voxelization.

    ``pitch`` (model units per stud) overrides ``target_studs`` when set.
    ``up`` names the mesh's vertical axis — most ``.obj`` files are y-up.
    ``fill`` floods the enclosed volume; disable for shell meshes. Filled
    cells all take ``colour_code`` (per-voxel colour sampling is a later
    milestone; interiors are hollowed away downstream regardless).
    """

    target_studs: int = 32
    pitch: float | None = None
    up: Literal["x", "y", "z"] = "z"
    colour_code: int = DEFAULT_MESH_COLOUR
    fill: bool = True
    keep_largest: bool = False

    def __post_init__(self) -> None:
        """Reject invalid programmatic configuration at the API boundary."""
        if self.target_studs <= 0:
            msg = "target_studs must be positive"
            raise ValueError(msg)
        if self.pitch is not None and (
            not math.isfinite(self.pitch) or self.pitch <= 0.0
        ):
            msg = "pitch must be finite and positive"
            raise ValueError(msg)
        if self.up not in {"x", "y", "z"}:
            msg = "up must be one of 'x', 'y', or 'z'"
            raise ValueError(msg)


def mesh_to_grid(
    path: Path,
    *,
    options: MeshOptions | None = None,
    palette: Palette | None = None,
    progress: Callable[[str], None] | None = None,
) -> VoxelGrid:
    """Load a mesh file and voxelize it into a plate-resolution grid."""
    return grid_from_mesh(
        _load_mesh(path),
        options=options,
        palette=palette,
        progress=progress,
    )


def grid_from_mesh(
    mesh: trimesh.Trimesh,
    *,
    options: MeshOptions | None = None,
    palette: Palette | None = None,
    progress: Callable[[str], None] | None = None,
) -> VoxelGrid:
    """Voxelize an in-memory mesh into a plate-resolution grid."""
    options = options or MeshOptions()
    palette = palette or default_palette()
    if options.colour_code not in palette.codes:
        msg = f"unknown LDraw colour code {options.colour_code} for mesh fill"
        raise ValueError(msg)
    working = _orient_z_up(mesh, options.up)
    pitch = _stud_pitch(working, options)
    working.apply_scale((1.0, 1.0, _PLATES_PER_STUD))
    _check_grid_dims(working, pitch)
    voxels = working.voxelized(pitch=pitch)
    if options.fill:
        voxels = voxels.fill()
    mask = np.asarray(voxels.matrix, dtype=bool)
    if not mask.any():
        msg = (
            "mesh voxelization produced no filled cells; try a larger "
            "--target-studs or a smaller --pitch"
        )
        raise ValueError(msg)
    if options.keep_largest:
        mask, dropped = _largest_component(mask)
        if dropped:
            message = (
                f"dropped {dropped} voxels disconnected from the largest "
                f"component (non-watertight mesh?)"
            )
            if progress is not None:
                progress(message)
            else:
                warnings.warn(message, UserWarning, stacklevel=2)
    codes = np.where(mask, options.colour_code, EMPTY).astype(np.int16)
    return VoxelGrid.from_array(codes, plates_per_voxel=1, palette=palette)


def _load_mesh(path: Path) -> trimesh.Trimesh:
    """Load a mesh file, wrapping loader failures in a clean ValueError."""
    try:
        loaded = trimesh.load(path, force="mesh")
    except Exception as error:  # trimesh loaders raise arbitrary types
        msg = f"failed to load mesh {path}: {error}"
        raise ValueError(msg) from error
    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        msg = f"no triangle faces found in {path}"
        raise ValueError(msg)
    return loaded


def _orient_z_up(mesh: trimesh.Trimesh, up: Literal["x", "y", "z"]) -> trimesh.Trimesh:
    """Return a copy rotated so the chosen up axis becomes +z.

    Proper 90-degree rotations only — an axis swap would mirror the model.
    """
    oriented = mesh.copy()
    match up:
        case "z":
            return oriented
        case "y":  # rotate +90 about x: y → z
            axis, sign = (1.0, 0.0, 0.0), 1.0
        case "x":  # rotate -90 about y: x → z
            axis, sign = (0.0, 1.0, 0.0), -1.0
    oriented.apply_transform(
        trimesh.transformations.rotation_matrix(sign * math.pi / 2.0, axis)
    )
    return oriented


def _stud_pitch(mesh: trimesh.Trimesh, options: MeshOptions) -> float:
    """Model units per stud: explicit --pitch, else widest extent / studs."""
    if options.pitch is not None:
        return options.pitch
    extent = float(np.max(mesh.extents[:_HORIZONTAL_AXES]))
    if extent <= 0.0:
        msg = "mesh has no horizontal extent after orientation; check --up"
        raise ValueError(msg)
    return extent / options.target_studs


def _check_grid_dims(mesh: trimesh.Trimesh, pitch: float) -> None:
    """Reject pitches that would blow up the voxel grid before voxelizing."""
    dims = [math.ceil(float(extent) / pitch) + 2 for extent in mesh.extents]
    listed = "x".join(str(dim) for dim in dims)
    if max(dims) > _MAX_GRID_DIM:
        msg = (
            f"voxelization would need a ~{listed} grid (cap {_MAX_GRID_DIM}); "
            f"reduce --target-studs or increase --pitch"
        )
        raise ValueError(msg)
    if (cell_count := math.prod(dims)) > _MAX_GRID_CELLS:
        msg = (
            f"voxelization would need a ~{listed} grid "
            f"({cell_count:_} cells; cap {_MAX_GRID_CELLS:_}); "
            f"reduce --target-studs or increase --pitch"
        )
        raise ValueError(msg)


def _largest_component(mask: np.ndarray) -> tuple[np.ndarray, int]:
    """Keep only the largest 6-connected component; return (mask, dropped)."""
    labels, count = ndimage.label(mask, structure=_FACE_STRUCTURE)
    if count <= 1:
        return mask, 0
    sizes = ndimage.sum_labels(mask, labels, index=range(1, count + 1))
    keep = int(np.argmax(sizes)) + 1
    kept = labels == keep
    return kept, int(np.count_nonzero(mask)) - int(np.count_nonzero(kept))
