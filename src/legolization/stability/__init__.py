"""Rigid-Block-Equilibrium stability engine (StableLego formulation)."""

from legolization.stability.constants import T_CAPACITY_N
from legolization.stability.model import StabilityModel, build_model
from legolization.stability.solver import (
    BrickScore,
    MaximinResult,
    SolverConfig,
    StabilityResult,
    analyze,
    build_model_from_config,
    solve_maximin,
    solve_model,
)

__all__ = [
    "T_CAPACITY_N",
    "BrickScore",
    "MaximinResult",
    "SolverConfig",
    "StabilityModel",
    "StabilityResult",
    "analyze",
    "build_model",
    "build_model_from_config",
    "solve_maximin",
    "solve_model",
]
