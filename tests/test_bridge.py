"""Bridge synthesizer: MILP ring re-tiling for the connectivity pass."""

import numpy as np
import pytest

from legolization import telemetry
from legolization.catalog import default_catalog
from legolization.graph import ConnectionGraph
from legolization.grid import EMPTY, VoxelGrid
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


def test_rephase_keeps_phase_zero_winner_on_clean_seam(catalog):
    def signature(result: Layout) -> list[tuple[str, int, int, int, int]]:
        return sorted((b.part_key, b.x, b.y, b.layer, b.yaw) for b in result)

    layout, grid = _towers()
    phase_zero = BridgeSynthesizer(catalog=catalog, rephase=False)(
        layout,
        set(layout.bricks),
        grid,
    )
    rephased = BridgeSynthesizer(catalog=catalog, rephase=True)(
        layout,
        set(layout.bricks),
        grid,
    )
    assert phase_zero is not None
    assert rephased is not None
    assert signature(rephased) == signature(phase_zero)


def test_rephase_selects_best_phase_deterministically(
    catalog,
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_phase_candidate(  # noqa: PLR0913
        self: BridgeSynthesizer,
        layout: Layout,
        region: set[int],
        grid: VoxelGrid,
        deadline: float,
        before: int,
        *,
        phase: int = 0,
    ) -> Layout | None:
        del self, layout, region, grid, deadline, before
        if phase == 0:
            return None
        candidate = Layout(catalog=default_catalog())
        candidate.add("brick_2x2", 0, 0, 0, 0, 4)
        if phase == 1:
            candidate.add("brick_2x2", 0, 0, 3, 0, 4)
        return candidate

    monkeypatch.setattr(
        BridgeSynthesizer,
        "_per_slab_candidate",
        fake_phase_candidate,
    )
    layout, grid = _towers()
    with telemetry.record() as session:
        chosen = BridgeSynthesizer(
            catalog=catalog,
            flow_escalate=False,
            rephase=True,
        )(layout, set(layout.bricks), grid)
    assert chosen is not None
    assert len(chosen) == 1
    assert session.values["connectivity.bridge.phase_attempted"] == [0.0, 1.0, 2.0]
    assert session.values["connectivity.bridge.phase_accepted"] == [2.0]


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


@pytest.mark.parametrize("mode", ["equal", "worse"])
def test_non_improving_bridge_callback_consumes_failures(
    catalog,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
):
    import legolization.placement.merge as merge_mod

    monkeypatch.setattr(
        merge_mod,
        "_best_bridging_draw",
        lambda *args, **kwargs: (None, None),
    )
    calls = 0

    def non_improving(
        layout: Layout,
        region: set[int],
        grid: VoxelGrid,
    ) -> Layout:
        nonlocal calls
        del region, grid
        calls += 1
        candidate = layout.copy()
        if mode == "worse":
            candidate.add("brick_1x1", 20, 20, 0, 0, 4)
        return candidate

    layout, grid = _towers()
    assert (
        improve_connectivity(
            layout,
            grid,
            np.random.default_rng(7),
            fail_max=3,
            bridge=non_improving,
        )
        == 2
    )
    assert calls == 3


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


def test_bridge_outer_deadline_skips_enumeration(catalog, monkeypatch):
    import legolization.placement.layered.bridge as bridge_mod

    def failing_enumerate(*args: object, **kwargs: object) -> object:
        msg = "enumeration must not run past the placement deadline"
        raise AssertionError(msg)

    monkeypatch.setattr(bridge_mod, "enumerate_layer_rects", failing_enumerate)
    layout, grid = _towers()
    synth = BridgeSynthesizer(
        catalog=catalog,
        placement_deadline=0.0,
    )
    assert synth(layout, set(layout.bricks), grid) is None


def _elevated_ladder() -> tuple[Layout, VoxelGrid, set[int]]:
    # The cover-coordination class the per-slab pass cannot solve: an
    # elevated ten-column plate run (max part length 8 forces a two-rect
    # cover with a seam) on a central pedestal, end risers up to two
    # floating plates A and B, and a mid plate that bridges the z3 seam
    # ONLY if the cover chooses to put the seam under it. Per-slab
    # commits the z3 cover before knowing the mid plate needs the seam
    # at x4..6; the joint flow MILP coordinates the choice. Elevation
    # matters: a grounded run would let flow route through the ground
    # (the documented approximation) instead of through structure.
    codes = np.full((10, 1, 6), EMPTY, dtype=np.int16)
    codes[4:6, 0, 0:3] = 4
    codes[:, 0, 3] = 4
    codes[0, 0, 4] = 4
    codes[9, 0, 4] = 4
    codes[3:7, 0, 4] = 4
    codes[0, 0, 5] = 4
    codes[9, 0, 5] = 4
    grid = VoxelGrid(codes=codes)
    layout = Layout(catalog=default_catalog())
    layout.add("brick_1x2", 4, 0, 0, 0, 4)  # pedestal, remaining
    ring = [
        layout.add("plate_1x8", 0, 0, 3, 0, 4),
        layout.add("plate_1x2", 8, 0, 3, 0, 4),
        layout.add("plate_1x1", 0, 0, 4, 0, 4),
        layout.add("plate_1x1", 9, 0, 4, 0, 4),
        layout.add("plate_1x4", 3, 0, 4, 0, 4),
    ]
    layout.add("plate_1x1", 0, 0, 5, 0, 4)  # A, remaining
    layout.add("plate_1x1", 9, 0, 5, 0, 4)  # B, remaining
    return layout, grid, {r.brick_id for r in ring}


def test_flow_escalation_solves_what_per_slab_declines(catalog):
    layout, grid, region = _elevated_ladder()
    assert ConnectionGraph.from_layout(layout).component_count() == 2

    per_slab_only = BridgeSynthesizer(catalog=catalog, flow_escalate=False)
    assert per_slab_only(layout, region, grid) is None

    bridged = BridgeSynthesizer(catalog=catalog)(layout, region, grid)
    assert bridged is not None
    graph = ConnectionGraph.from_layout(bridged)
    assert graph.component_count() == 1
    assert not graph.floating_ids()


def test_flow_is_deterministic(catalog):
    def signature(result) -> list:
        return sorted(
            (b.part_key, b.x, b.y, b.layer, b.yaw, b.colour_code)
            for b in result.bricks.values()
        )

    runs = []
    for _ in range(2):
        layout, grid, region = _elevated_ladder()
        bridged = BridgeSynthesizer(catalog=catalog)(layout, region, grid)
        assert bridged is not None
        runs.append(signature(bridged))
    assert runs[0] == runs[1]


def test_flow_graph_mates_across_brick_plate_planes(catalog):
    # A carved brick slab (planes 0..3) under a carved plate course
    # (planes 3..4) under a remaining plate at layer 4: hubs key on the
    # actual mating plane, so the brick problem's top and the plate
    # problem's bottom share hub (column, 3) — plate interleaving falls
    # out of the keying, never a fixed z±3 offset.
    import time

    codes = np.full((2, 1, 5), EMPTY, dtype=np.int16)
    codes[:, 0, :] = 4
    grid = VoxelGrid(codes=codes)
    layout = Layout(catalog=default_catalog())
    ring = [
        layout.add("brick_1x2", 0, 0, 0, 0, 4),
        layout.add("plate_1x2", 0, 0, 3, 0, 4),
    ]
    remaining = layout.add("plate_1x2", 0, 0, 4, 0, 4)
    del remaining
    region = {r.brick_id for r in ring}

    synth = BridgeSynthesizer(catalog=catalog)
    carved = synth._carve(layout, region, grid)  # noqa: SLF001
    assert carved is not None
    candidate, cells = carved
    labels = ConnectionGraph.from_layout(candidate).brick_components()
    entries = synth._gather_entries(  # noqa: SLF001
        candidate, cells, time.monotonic() + 5.0
    )
    assert entries is not None
    assert sorted((e.problem.layer, e.problem.height_plates) for e in entries) == [
        (0, 3),
        (3, 1),
    ]
    graph = synth._flow_graph(candidate, entries, labels)  # noqa: SLF001
    assert graph is not None
    shared_plane_hubs = [
        key
        for key in graph.node_ids
        if key[0] == "h" and isinstance(key[1], tuple) and key[1][1] == 3
    ]
    assert shared_plane_hubs  # the brick top / plate bottom meeting plane
    arcs_by_node = dict.fromkeys(graph.node_ids.values(), 0)
    for tail, head in graph.arcs:
        arcs_by_node[tail] += 1
        arcs_by_node[head] += 1
    for key in shared_plane_hubs:
        # Both the brick-problem rects and the plate-problem rects reach
        # this hub, so it carries arcs from more than one problem.
        assert arcs_by_node[graph.node_ids[key]] >= 4


def test_flow_grounded_root_when_everything_carved(catalog):
    # No remaining components: the ground is the flow root and the
    # towers rejoin through it legitimately (they rebuild from plane 0).
    import time

    layout, grid = _towers()
    region = set(layout.bricks)
    synth = BridgeSynthesizer(catalog=catalog)
    before = ConnectionGraph.from_layout(layout).component_count()
    bridged = synth._flow_candidate(  # noqa: SLF001
        layout, region, grid, time.monotonic() + 10.0, before
    )
    assert bridged is not None
    assert ConnectionGraph.from_layout(bridged).component_count() == 1


def test_flow_limits_return_none(catalog):
    layout, grid, region = _elevated_ladder()
    tight_candidates = BridgeSynthesizer(catalog=catalog, flow_candidate_limit=10)
    assert tight_candidates(layout, region, grid) is None
    tight_arcs = BridgeSynthesizer(catalog=catalog, flow_arc_limit=10)
    assert tight_arcs(layout, region, grid) is None


def test_flow_decline_still_preserves_rng(catalog):
    # With escalation ON and both paths declining, improve_connectivity
    # must consume the same rng stream as bridge=None (the synthesizer
    # never draws).
    layout_a, grid = _towers()
    layout_b = layout_a.copy()
    declining = BridgeSynthesizer(catalog=catalog, slab_time_s=1e-9, total_time_s=1e-9)
    final_a = improve_connectivity(
        layout_a, grid, np.random.default_rng(3), bridge_draws=2, bridge=declining
    )
    final_b = improve_connectivity(
        layout_b, grid, np.random.default_rng(3), bridge_draws=2, bridge=None
    )
    assert final_a == final_b == 1
    sig = lambda lo: sorted(  # noqa: E731
        (b.part_key, b.x, b.y, b.layer) for b in lo.bricks.values()
    )
    assert sig(layout_a) == sig(layout_b)
