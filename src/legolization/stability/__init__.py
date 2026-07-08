"""Rigid-Block-Equilibrium stability engine (StableLego formulation)."""

from legolization.stability.constants import T_CAPACITY_N
from legolization.stability.model import StabilityModel, build_model
from legolization.stability.solver import (
    BrickScore,
    SolverConfig,
    StabilityResult,
    analyze,
    solve_model,
)

__all__ = [
    "T_CAPACITY_N",
    "BrickScore",
    "SolverConfig",
    "StabilityModel",
    "StabilityResult",
    "analyze",
    "build_model",
    "solve_model",
]
