"""Bridge synthesizer: MILP ring re-tiling for the connectivity pass."""

import numpy as np
import pytest

from legolization.catalog import default_catalog
from legolization.graph import ConnectionGraph
from legolization.grid import VoxelGrid
from legolization.layout import Layout
from legolization.placement.layered.bridge import BridgeSynthesizer
from legolization.placement.merge import improve_connectivity


@pytest.fixture
def catalog():
    return default_catalog()


def _towers() -> tuple[Layout, VoxelGrid]:
    # Two 2-wide towers, straight seam, two courses: the drift report's
    # motivating fragmentation class.
    codes = np.full((4, 1, 6), 4, dtype=np.int16)
    grid = VoxelGrid(codes=codes)
    layout = Layout(catalog=default_catalog())
    for x in (0, 2):
        for level in (0, 1):
            layout.add("brick_1x2", x, 0, 3 * level, 0, 4)
    return layout, grid


def test_bridge_beats_random_on_clean_seams(catalog):
    # The random rewrite bridges the towers by splitting to six 1x4
    # plates; the MILP produces the true optimum: two 1x4 bricks.
    layout, grid = _towers()
    final = improve_connectivity(
        layout,
        grid,
        np.random.default_rng(7),
        bridge_draws=5,
        bridge=BridgeSynthesizer(catalog=catalog),
    )
    assert final == 1
    assert sorted(b.part_key for b in layout) == ["brick_1x4", "brick_1x4"]
    assert not ConnectionGraph.from_layout(layout).floating_ids()


def test_bridge_is_deterministic(catalog):
    def run() -> list[tuple[str, int, int, int, int]]:
        layout, grid = _towers()
        improve_connectivity(
            layout,
            grid,
            np.random.default_rng(7),
            bridge_draws=5,
            bridge=BridgeSynthesizer(catalog=catalog),
        )
        return sorted((b.part_key, b.x, b.y, b.layer, b.yaw) for b in layout)

    assert run() == run()


def test_bridge_decline_matches_random_only(catalog):
    # When the synthesizer declines, the rng stream and outcome must be
    # byte-identical to bridge=None — the fallback contract.
    def declining(layout, region, grid) -> None:  # contract signature
        return None

    def run(bridge) -> list[tuple[str, int, int, int, int]]:
        layout, grid = _towers()
        improve_connectivity(
            layout, grid, np.random.default_rng(11), bridge_draws=5, bridge=bridge
        )
        return sorted((b.part_key, b.x, b.y, b.layer, b.yaw) for b in layout)

    assert run(declining) == run(None)


def test_bridge_timeout_returns_none(catalog):
    layout, grid = _towers()
    synth = BridgeSynthesizer(catalog=catalog, slab_time_s=1e-9, total_time_s=1e-9)
    region = set(layout.bricks)
    assert synth(layout, region, grid) is None


def test_bridge_skips_uncarvable_regions(catalog):
    # A region of only non-mergeable parts (a sideways tile) yields no
    # cells to re-tile.
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x1_side_stud", 0, 0, 0, 0, 4)
    tile = layout.add("tile_1x1_snot", 1, 0, 0, 0, 4)
    codes = np.full((1, 1, 3), 4, dtype=np.int16)
    synth = BridgeSynthesizer(catalog=catalog)
    assert synth(layout, {tile.brick_id}, VoxelGrid(codes=codes)) is None


def test_bridge_requires_component_drop(catalog):
    # A single-component layout cannot improve; the final guard declines.
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x4", 0, 0, 0, 0, 4)
    codes = np.full((4, 1, 3), 4, dtype=np.int16)
    synth = BridgeSynthesizer(catalog=catalog)
    assert synth(layout, set(layout.bricks), VoxelGrid(codes=codes)) is None


def test_bridge_expired_budget_skips_enumeration(catalog, monkeypatch):
    # PR #18 review (v5 scope): candidate enumeration ran before the
    # budget check, same flaw as kollsker's — an exhausted budget must
    # not pay for enumeration on any slab component.
    import legolization.placement.layered.bridge as bridge_mod

    def failing_enumerate(*args: object, **kwargs: object) -> object:
        msg = "enumeration must not run under an expired budget"
        raise AssertionError(msg)

    monkeypatch.setattr(bridge_mod, "enumerate_layer_rects", failing_enumerate)
    layout, grid = _towers()
    synth = BridgeSynthesizer(catalog=catalog, slab_time_s=1e-9, total_time_s=1e-9)
    assert synth(layout, set(layout.bricks), grid) is None
