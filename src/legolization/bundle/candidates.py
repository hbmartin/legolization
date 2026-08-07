"""Candidate plans: quality tiers, colour variants, and exact preflight.

The three quality tiers fix the candidate policy and time budget:
fast (greedy, seed 0, 2 minutes), balanced (the full ordinary sweep at
seed 0 plus eligible exact placement, 15 minutes), and exhaustive
(ordinary strategies at seeds 0/1/2 plus exact once; the caller must
supply the duration). Source colours are compared automatically across
hard/no-dither, soft/no-dither, and soft+dither variants; variants
whose target grids are byte-identical collapse, and single-colour
targets keep only the hard variant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from legolization.errors import ConfigurationError

if TYPE_CHECKING:
    from legolization.grid import VoxelGrid

FAST_BUDGET_S = 120.0
BALANCED_BUDGET_S = 900.0

type QualityTier = Literal["fast", "balanced", "exhaustive"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ColourVariant:
    """One automatic source-colour comparison case."""

    name: Literal["hard", "soft", "soft-dither"]
    colour_mode: Literal["hard", "soft"]
    dither: bool

    def to_dict(self) -> dict[str, object]:
        """Return the JSON payload for this variant."""
        return {
            "name": self.name,
            "colour_mode": self.colour_mode,
            "dither": self.dither,
        }


VARIANTS: tuple[ColourVariant, ...] = (
    ColourVariant(name="hard", colour_mode="hard", dither=False),
    ColourVariant(name="soft", colour_mode="soft", dither=False),
    ColourVariant(name="soft-dither", colour_mode="soft", dither=True),
)
CANONICAL_VARIANT = VARIANTS[0]


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateSpec:
    """One (strategy, seed, variant) case of the plan."""

    strategy: str
    seed: int
    variant: ColourVariant

    def key(self) -> str:
        """Return the stable worker-directory key for this case."""
        from legolization.eval_artifacts import safe_name  # noqa: PLC0415

        return safe_name(f"{self.strategy}-s{self.seed}-{self.variant.name}")

    def to_dict(self) -> dict[str, object]:
        """Return the JSON payload for this spec."""
        return {
            "strategy": self.strategy,
            "seed": self.seed,
            "variant": self.variant.name,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidatePlan:
    """The complete candidate policy for one bundle invocation."""

    quality: QualityTier
    specs: tuple[CandidateSpec, ...]
    time_budget_s: float
    exact_included: bool
    exact_skip_reason: str | None
    variants: tuple[ColourVariant, ...]
    collapsed: dict[str, str]

    def to_detail(self) -> dict[str, object]:
        """Return the stage-detail payload describing this plan."""
        return {
            "quality": self.quality,
            "candidate_count": len(self.specs),
            "time_budget_s": self.time_budget_s,
            "exact_included": self.exact_included,
            "exact_skip_reason": self.exact_skip_reason,
            "variants": [variant.name for variant in self.variants],
            "collapsed_variants": dict(self.collapsed),
        }


def dedup_variants(
    grids: dict[str, VoxelGrid],
) -> tuple[tuple[ColourVariant, ...], dict[str, str]]:
    """Collapse variants whose targets are equivalent.

    Content-hash equality collapses the dithered variant onto its
    undithered twin; a single-colour target keeps only the hard
    variant (soft merging cannot differ without colour boundaries).
    """
    import numpy as np  # noqa: PLC0415

    hard = grids[CANONICAL_VARIANT.name]
    filled = hard.codes[hard.filled_mask]
    if np.unique(filled).size <= 1:
        return (
            (CANONICAL_VARIANT,),
            {"soft": "hard", "soft-dither": "hard"},
        )
    dithered = grids["soft-dither"]
    if dithered.codes.tobytes() == hard.codes.tobytes():
        return (VARIANTS[0], VARIANTS[1]), {"soft-dither": "soft"}
    return VARIANTS, {}


def plan_candidates(
    *,
    quality: QualityTier,
    variants: tuple[ColourVariant, ...],
    collapsed: dict[str, str],
    exact_skip_reason: str | None,
    duration_s: float | None = None,
) -> CandidatePlan:
    """Build the candidate plan for one quality tier."""
    from legolization.placement.registry import strategy_names  # noqa: PLC0415

    match quality:
        case "fast":
            strategies: tuple[str, ...] = ("greedy",)
            seeds: tuple[int, ...] = (0,)
            budget = FAST_BUDGET_S
        case "balanced":
            strategies = strategy_names()
            seeds = (0,)
            budget = BALANCED_BUDGET_S
        case "exhaustive":
            strategies = strategy_names()
            seeds = (0, 1, 2)
            if duration_s is None:
                msg = "exhaustive quality requires an explicit --duration"
                raise ConfigurationError(msg)
            budget = duration_s
    if duration_s is not None:
        budget = duration_s
    specs = [
        CandidateSpec(strategy=strategy, seed=seed, variant=variant)
        for strategy in strategies
        for seed in seeds
        for variant in variants
    ]
    exact_included = quality != "fast" and exact_skip_reason is None
    if exact_included:
        specs.append(
            CandidateSpec(strategy="global-exact", seed=0, variant=variants[0])
        )
    return CandidatePlan(
        quality=quality,
        specs=tuple(specs),
        time_budget_s=budget,
        exact_included=exact_included,
        exact_skip_reason=None if exact_included else exact_skip_reason,
        variants=variants,
        collapsed=collapsed,
    )
