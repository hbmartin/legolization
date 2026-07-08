"""Turn colored voxel models into physically stable LEGO models in LDraw.

Pipeline: voxelize → hollow → place bricks → check stability (RBE) →
refine → export ``.ldr``/``.mpd`` with step-by-step build instructions.
"""

from legolization.grid import VoxelGrid
from legolization.layout import Layout, PlacedBrick
from legolization.pipeline import PipelineConfig, PipelineResult, run, run_file

__all__ = [
    "Layout",
    "PipelineConfig",
    "PipelineResult",
    "PlacedBrick",
    "VoxelGrid",
    "run",
    "run_file",
]
