"""Turn colored voxel models into physically stable LEGO models in LDraw.

Pipeline: voxelize → hollow → place bricks → check stability (RBE) →
refine → export ``.ldr``/``.mpd`` with step-by-step build instructions.
"""

from legolization.grid import VoxelGrid
from legolization.layout import Layout, PlacedBrick
from legolization.mesh import MeshOptions, mesh_to_grid
from legolization.pipeline import PipelineConfig, PipelineResult, run, run_file

__all__ = [
    "Layout",
    "MeshOptions",
    "PipelineConfig",
    "PipelineResult",
    "PlacedBrick",
    "VoxelGrid",
    "mesh_to_grid",
    "run",
    "run_file",
]
