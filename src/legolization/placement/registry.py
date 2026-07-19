"""Strategy registry: names the pipeline and CLI can instantiate.

Adding a strategy means writing a factory here — the CLI ``--strategy``
choices and the pipeline dispatch both derive from this table.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from legolization.placement.greedy import GreedyStrategy
from legolization.placement.layered import (
    BeautyStrategy,
    BeautyWeights,
    BondStrategy,
    FastStrategy,
    KollskerStrategy,
    SmGaConfig,
    SmGaStrategy,
)
from legolization.placement.luo import LuoStrategy

if TYPE_CHECKING:
    from collections.abc import Callable

    from legolization.catalog import Catalog
    from legolization.pipeline import PipelineConfig
    from legolization.placement.base import PlacementStrategy

    StrategyFactory = Callable[[Catalog, PipelineConfig], PlacementStrategy]


def strategy_names() -> tuple[str, ...]:
    """All registered strategy names, sorted."""
    return tuple(sorted(_STRATEGIES))


def make_strategy(
    name: str,
    *,
    catalog: Catalog,
    config: PipelineConfig,
) -> PlacementStrategy:
    """Instantiate a registered strategy from the pipeline configuration."""
    try:
        factory = _STRATEGIES[name]
    except KeyError:
        known = ", ".join(strategy_names())
        msg = f"unknown strategy {name!r}; choose from: {known}"
        raise ValueError(msg) from None
    return factory(catalog, config)


def _make_greedy(catalog: Catalog, config: PipelineConfig) -> PlacementStrategy:
    return GreedyStrategy(
        catalog=catalog,
        weights=config.weights,
        solver_config=config.solver,
        refine=config.refine,
    )


def _make_luo(catalog: Catalog, config: PipelineConfig) -> PlacementStrategy:
    return LuoStrategy(
        catalog=catalog,
        solver_config=config.solver,
        colour_mode=config.colour_mode,
        colour_weight=config.colour_weight,
        refine=config.refine,
    )


def _make_bond(catalog: Catalog, config: PipelineConfig) -> PlacementStrategy:
    return BondStrategy(
        catalog=catalog,
        weights=config.weights,
        solver_config=config.solver,
        time_budget_s=config.time_budget_s,
        progress=config.progress,
    )


def _make_fast(catalog: Catalog, config: PipelineConfig) -> PlacementStrategy:
    return FastStrategy(
        catalog=catalog,
        weights=config.weights,
        solver_config=config.solver,
        time_budget_s=config.time_budget_s,
        progress=config.progress,
    )


def _make_smga(catalog: Catalog, config: PipelineConfig) -> PlacementStrategy:
    return SmGaStrategy(
        catalog=catalog,
        weights=config.weights,
        solver_config=config.solver,
        time_budget_s=config.time_budget_s,
        progress=config.progress,
        config=SmGaConfig(max_generations=config.ga_generations),
    )


def _make_beauty(catalog: Catalog, config: PipelineConfig) -> PlacementStrategy:
    return BeautyStrategy(
        catalog=catalog,
        weights=config.weights,
        solver_config=config.solver,
        time_budget_s=config.time_budget_s,
        progress=config.progress,
        beauty=BeautyWeights.preset(config.beauty_preset),
    )


def _make_kollsker(catalog: Catalog, config: PipelineConfig) -> PlacementStrategy:
    return KollskerStrategy(
        catalog=catalog,
        weights=config.weights,
        solver_config=config.solver,
        time_budget_s=config.time_budget_s,
        progress=config.progress,
        layer_time_s=config.milp_layer_time_s,
        bond_weight=config.milp_bond_weight,
    )


_STRATEGIES: dict[str, StrategyFactory] = {
    "greedy": _make_greedy,
    "luo": _make_luo,
    "bond": _make_bond,
    "fast": _make_fast,
    "smga": _make_smga,
    "beauty": _make_beauty,
    "kollsker": _make_kollsker,
}
