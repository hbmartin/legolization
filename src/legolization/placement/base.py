"""Placement strategy interface and the configurable weighted objective.

The objective is a weighted sum (all terms normalized to ~[0, 1], lower is
better): part cost, physical instability (from the RBE), aesthetics (seam
bonding), colour fidelity, perpendicularity (alternating brick directions
between layers; reported but unweighted by default), global-plane mirror
symmetry, and the weightless audition terms colour speckle and profile
roughness. Strategies use it to accept or reject refinement steps; the CLI
reports it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from legolization.graph import ConnectionGraph
from legolization.placement.aesthetics import (
    colour_speckle_error,
    perpendicularity_error,
    profile_roughness,
    symmetry_error,
)
from legolization.stability.solver import SolverConfig, StabilityResult, analyze

if TYPE_CHECKING:
    import numpy as np

    from legolization.grid import VoxelGrid
    from legolization.layout import Layout
    from legolization.placement.global_exact import ExactOutcome


@dataclass(frozen=True, slots=True)
class ObjectiveWeights:
    """Relative importance of the objective terms."""

    cost: float = 1.0
    stability: float = 4.0
    aesthetics: float = 0.5
    colour: float = 1.0
    # Measured inverted against the human corpora — official sets score WORSE
    # than we do, so any positive weight pushes away from how good builds
    # actually look. Reported but unweighted; see
    # docs/reports/aesthetics-validation.md.
    perpendicularity: float = 0.0
    symmetry: float = 0.25
    # Kollsker stretcher-bond constants used by greedy candidate scoring:
    # a border whose seam below sits d studs away is penalized
    # ``bond_alpha1 * exp(-bond_alpha2 * d)`` (d = 0 is a stacked seam).
    bond_alpha1: float = 4.0
    bond_alpha2: float = 0.8
    # Appended to preserve positional callers that set the two bond constants.
    # Audition terms remain reported-only until they pass the validation gates.
    speckle: float = 0.0
    profile: float = 0.0

    def __post_init__(self) -> None:
        values = (
            ("cost", self.cost),
            ("stability", self.stability),
            ("aesthetics", self.aesthetics),
            ("colour", self.colour),
            ("perpendicularity", self.perpendicularity),
            ("symmetry", self.symmetry),
            ("speckle", self.speckle),
            ("profile", self.profile),
            ("bond_alpha1", self.bond_alpha1),
            ("bond_alpha2", self.bond_alpha2),
        )
        for name, value in values:
            if not math.isfinite(value) or value < 0:
                msg = f"objective weight {name} must be finite and non-negative"
                raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class ObjectiveReport:
    """Scored terms for one layout (all lower-is-better)."""

    cost: float
    instability: float
    aesthetics: float
    colour_error: float
    perpendicularity: float
    symmetry: float
    total: float
    stability: StabilityResult
    # Appended with sentinels so historical positional construction remains
    # valid without silently reporting an uncomputed term as a perfect score.
    speckle: float = math.nan
    profile: float = math.nan


def _objective_terms(layout: Layout, grid: VoxelGrid) -> dict[str, float]:
    """Every objective term except stability (shared with screened scoring)."""
    voxels = max(grid.filled_count, 1)
    return {
        "cost": len(layout) / voxels,
        "aesthetics": _seam_alignment(layout),
        "colour_error": _colour_mismatch(layout, grid.codes),
        "perpendicularity": perpendicularity_error(layout),
        "symmetry": symmetry_error(layout),
        "speckle": colour_speckle_error(layout),
        "profile": profile_roughness(layout),
    }


def _weighted_total(
    terms: dict[str, float], instability: float, weights: ObjectiveWeights
) -> float:
    """Compose the weighted objective from terms plus a stability score."""
    return (
        weights.cost * terms["cost"]
        + weights.stability * instability
        + weights.aesthetics * terms["aesthetics"]
        + weights.colour * terms["colour_error"]
        + weights.perpendicularity * terms["perpendicularity"]
        + weights.symmetry * terms["symmetry"]
        + weights.speckle * terms["speckle"]
        + weights.profile * terms["profile"]
    )


def evaluate(
    layout: Layout,
    grid: VoxelGrid,
    weights: ObjectiveWeights | None = None,
    solver_config: SolverConfig | None = None,
) -> ObjectiveReport:
    """Score a layout against the weighted objective."""
    weights = weights or ObjectiveWeights()
    stability = analyze(layout, solver_config)
    terms = _objective_terms(layout, grid)
    instability = stability.max_score
    return ObjectiveReport(
        cost=terms["cost"],
        instability=instability,
        aesthetics=terms["aesthetics"],
        colour_error=terms["colour_error"],
        perpendicularity=terms["perpendicularity"],
        symmetry=terms["symmetry"],
        speckle=terms["speckle"],
        profile=terms["profile"],
        total=_weighted_total(terms, instability, weights),
        stability=stability,
    )


class PlacementStrategy(Protocol):
    """Turns a voxel grid into a placed-brick layout."""

    @property
    def outcome(self) -> ExactOutcome | None:
        """Return exact-strategy evidence, or None for heuristic strategies."""
        ...

    def place(
        self,
        grid: VoxelGrid,
        *,
        rng: np.random.Generator,
        deadline: float | None = None,
    ) -> Layout:
        """Produce a layout, honoring an absolute deadline when supported."""
        ...


def connection_density(layout: Layout) -> float:
    """Mean distinct vertical partners per brick (higher = better bonded)."""
    if not len(layout):
        return 0.0
    graph = ConnectionGraph.from_layout(layout)
    partners = set(graph.support_edges())
    return 2.0 * len(partners) / len(layout)


def _seam_alignment(layout: Layout) -> float:
    """Fraction of brick-pair interfaces repeated directly one plate up.

    Stacked (aligned) seams are structurally and visually weak — the classic
    stack-bond smell. A seam is counted once per touching brick pair per
    vertical run (at the run's top plate), not once per plate cell, so a
    brick-to-brick joint doesn't triple-count: the metric genuinely spans
    [0, 1] — a stretcher-bond wall scores 0.0, an n-course stack-bond wall
    (n - 1) / n.
    """
    seams: dict[tuple[int, int, int, int], tuple[int, int]] = {}
    interfaces: dict[tuple[tuple[int, int], int], set[tuple[int, int, int]]] = {}
    for brick in layout:
        own = brick.brick_id
        for x, y, z in layout.cells_of(brick):
            for axis, (dx, dy) in enumerate(((1, 0), (0, 1))):
                neighbour = layout.brick_at((x + dx, y + dy, z))
                if neighbour is not None and neighbour.brick_id != own:
                    pair = (min(own, neighbour.brick_id), max(own, neighbour.brick_id))
                    seams[(x, y, z, axis)] = pair
                    interfaces.setdefault((pair, axis), set()).add((x, y, z))
    tops = len(interfaces)
    repeated = 0
    for (_, axis), cells in interfaces.items():
        top = max(z for _, _, z in cells)
        if any((x, y, top + 1, axis) in seams for x, y, z in cells if z == top):
            repeated += 1
    return repeated / tops if tops else 0.0


def _colour_mismatch(layout: Layout, codes: np.ndarray) -> float:
    """Fraction of covered voxels whose brick colour differs from the grid."""
    total = 0
    wrong = 0
    nx, ny, nz = codes.shape
    for brick in layout:
        for x, y, z in layout.filled_cells_of(brick):
            if 0 <= x < nx and 0 <= y < ny and 0 <= z < nz:
                target = int(codes[x, y, z])
                if target >= 0:
                    total += 1
                    if target != brick.colour_code:
                        wrong += 1
    return wrong / total if total else 0.0
