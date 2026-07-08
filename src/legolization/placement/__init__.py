"""Placement strategies: greedy rebuild and Luo split/remerge."""

from legolization.placement.base import (
    ObjectiveReport,
    ObjectiveWeights,
    PlacementStrategy,
    evaluate,
)
from legolization.placement.greedy import GreedyStrategy
from legolization.placement.luo import LuoStrategy
from legolization.placement.slopes import apply_slopes, apply_tiles

__all__ = [
    "GreedyStrategy",
    "LuoStrategy",
    "ObjectiveReport",
    "ObjectiveWeights",
    "PlacementStrategy",
    "apply_slopes",
    "apply_tiles",
    "evaluate",
]
