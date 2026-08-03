"""Turn colored voxel models into physically stable LEGO models in LDraw.

Pipeline: voxelize → hollow → place bricks → check stability (RBE) →
refine → export ``.ldr``/``.mpd`` with step-by-step build instructions.
"""

from legolization.analysis import (
    AnalysisConfig,
    AnalysisReport,
    AnalysisResult,
    analyze_ldraw,
)
from legolization.assembly import (
    AssemblyAnalysisConfig,
    AssemblyAnalysisReport,
    AssemblyAnalysisResult,
    analyze_assembly,
)
from legolization.assembly_grid import GridFrame
from legolization.assembly_model import AssemblyModel, AssemblyOccurrence
from legolization.grid import VoxelGrid
from legolization.layout import Layout, PlacedBrick
from legolization.mesh import MeshOptions, mesh_to_grid
from legolization.pipeline import PipelineConfig, PipelineResult, run, run_file

__all__ = [
    "AnalysisConfig",
    "AnalysisReport",
    "AnalysisResult",
    "AssemblyAnalysisConfig",
    "AssemblyAnalysisReport",
    "AssemblyAnalysisResult",
    "AssemblyModel",
    "AssemblyOccurrence",
    "GridFrame",
    "Layout",
    "MeshOptions",
    "PipelineConfig",
    "PipelineResult",
    "PlacedBrick",
    "VoxelGrid",
    "analyze_assembly",
    "analyze_ldraw",
    "mesh_to_grid",
    "run",
    "run_file",
]
