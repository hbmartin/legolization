"""Strategy-agnostic ALNS stability repair (Kollsker's Algorithm 1).

Localize instability with the artificial-link QP (or the RBE's per-brick
scores as a fallback), destroy every brick adjacent to the strongest links,
refill the freed cells — a small exact-cover MILP when the region is small,
the merge engine otherwise — and accept only strict improvement of the link
deficit ``q``. On failure the destroy threshold decays (``beta = beta0 *
gamma^i``), widening the neighbourhood until it covers everything. Because
repair rearranges bricks at constant volume, the pipeline runs it before
falling back to the hollow-restore loop, which adds material.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp
from scipy.sparse import coo_matrix

from legolization import telemetry
from legolization.catalog import Category, rotate_offset
from legolization.grid import EMPTY, merge_colour
from legolization.placement.merge import (
    compact_columns,
    compact_vertical,
    regional_random_merge,
)
from legolization.stability.links import LinkReport, localize_instability
from legolization.stability.solver import analyze

if TYPE_CHECKING:
    from legolization.catalog import Catalog, Cell
    from legolization.grid import VoxelGrid
    from legolization.layout import Layout
    from legolization.stability.solver import SolverConfig

_Q_TOLERANCE = 1e-9


@dataclass(frozen=True, slots=True)
class RepairConfig:
    """ALNS knobs (Kollsker's beta0/gamma/epsilon defaults)."""

    beta0: float = 0.8
    gamma: float = 0.5
    epsilon: float = 0.05
    max_rounds: int = 12
    localizer: Literal["qp", "rbe"] = "qp"
    filler: Literal["merge", "milp"] = "merge"
    milp_cell_limit: int = 200


@dataclass(frozen=True, slots=True)
class RepairReport:
    """What a repair run did."""

    stable: bool
    rounds: int
    q_history: tuple[float, ...]
    bricks_rebuilt: int


def repair_stability(  # noqa: PLR0913 - the repair owns the whole pipeline state
    layout: Layout,
    grid: VoxelGrid,
    *,
    catalog: Catalog,
    solver_config: SolverConfig | None = None,
    rng: np.random.Generator,
    config: RepairConfig | None = None,
    deadline: float | None = None,
) -> RepairReport:
    """Destroy-and-repair around the strongest artificial links, in place.

    ``deadline`` (absolute monotonic seconds) is the pipeline's shared
    budget, checked at round boundaries only — a running localize/refill
    is never interrupted, and ``None`` keeps the historical unbounded
    behaviour byte-identical. Every full-structure localization costs
    ~n^2.8 in brick count (measured v8), so unbudgeted rounds are what
    turned Armadillo-class repairs into 600-second walls.
    """
    config = config or RepairConfig()
    report = _localize(layout, solver_config, config)
    q_history = [report.q]
    rounds = 0
    rebuilt = 0
    escalation = 0
    while report.q > _Q_TOLERANCE and rounds < config.max_rounds:
        if deadline is not None and time.monotonic() >= deadline:
            telemetry.value("repair.deadline_stop", float(rounds))
            break
        rounds += 1
        victims = _destroy_set(layout, report, config, escalation)
        if not victims:
            escalation += 1
            continue
        candidate = layout.copy()
        freed = _remove(candidate, victims, grid)
        _refill(candidate, freed, grid, catalog, rng, config)
        candidate_report = _localize(candidate, solver_config, config)
        if candidate_report.q < report.q:
            layout.replace_with(candidate)
            report = candidate_report
            rebuilt += len(victims)
        else:
            escalation += 1
        q_history.append(report.q)
    stable = analyze(layout, solver_config).stable
    return RepairReport(
        stable=stable,
        rounds=rounds,
        q_history=tuple(q_history),
        bricks_rebuilt=rebuilt,
    )


def _localize(
    layout: Layout,
    solver_config: SolverConfig | None,
    config: RepairConfig,
) -> LinkReport:
    if config.localizer == "qp":
        report = localize_instability(layout, config=solver_config)
        if report.status == "optimal" and (report.stable or report.links):
            return report
    return _rbe_report(layout, solver_config)


def _rbe_report(layout: Layout, solver_config: SolverConfig | None) -> LinkReport:
    """Fallback localization from the RBE's per-brick stress scores."""
    from legolization.stability.links import LinkForce  # noqa: PLC0415 - cycle guard

    result = analyze(layout, solver_config)
    if result.stable:
        return LinkReport(q=0.0, links=(), status="optimal")
    links = [
        LinkForce(a_id=brick_id, b_id=brick_id, magnitude=1.0)
        for brick_id in sorted(result.unstable_ids)
    ]
    if result.weakest_pair is not None:
        a, b = result.weakest_pair
        links.append(LinkForce(a_id=a, b_id=b, magnitude=0.5))
    return LinkReport(
        q=float(len(result.unstable_ids)),
        links=tuple(links),
        status="optimal",
    )


def _destroy_set(
    layout: Layout,
    report: LinkReport,
    config: RepairConfig,
    escalation: int,
) -> set[int]:
    if not report.links:
        return set()
    strongest = report.links[0].magnitude
    beta = config.beta0 * config.gamma**escalation
    threshold = max(beta - config.epsilon, 0.0) * strongest
    victims: set[int] = set()
    for link in report.links:
        if link.magnitude >= threshold:
            victims |= {
                brick_id
                for brick_id in (link.a_id, link.b_id)
                if brick_id >= 0 and brick_id in layout.bricks
            }
    return victims


def _remove(layout: Layout, victims: set[int], grid: VoxelGrid) -> set[Cell]:
    freed: set[Cell] = set()
    for brick_id in victims:
        brick = layout.bricks[brick_id]
        freed |= set(layout.filled_cells_of(brick))
        layout.remove(brick_id)
    nx, ny, nz = grid.shape
    return {
        (x, y, z)
        for x, y, z in freed
        if 0 <= x < nx
        and 0 <= y < ny
        and 0 <= z < nz
        and int(grid.codes[x, y, z]) != EMPTY
    }


def _refill(  # noqa: PLR0913 - the filler owns the whole pipeline state
    layout: Layout,
    freed: set[Cell],
    grid: VoxelGrid,
    catalog: Catalog,
    rng: np.random.Generator,
    config: RepairConfig,
) -> None:
    if config.filler == "milp" and len(freed) <= config.milp_cell_limit:
        placements = _milp_fill(layout, freed, grid, catalog)
        if placements is not None:
            for part_key, anchor, yaw, colour in placements:
                layout.add(part_key, *anchor, yaw, colour)
            return
    _merge_fill(layout, freed, grid, rng)


def _merge_fill(
    layout: Layout,
    freed: set[Cell],
    grid: VoxelGrid,
    rng: np.random.Generator,
) -> None:
    """Re-atomize the freed cells and merge outward from the new atoms."""
    atom_ids: set[int] = set()
    for x, y, z in sorted(freed):
        code = int(grid.codes[x, y, z])
        atom = layout.add("plate_1x1", x, y, z, 0, code)
        atom_ids.add(atom.brick_id)
    compact_columns(layout, atom_ids)
    # compact_columns re-forms bricks under fresh ids: seed the merge from
    # whatever occupies the freed cells now.
    seeds = {
        brick.brick_id for cell in freed if (brick := layout.brick_at(cell)) is not None
    }
    regional_random_merge(layout, seeds, rng)
    compact_vertical(layout)


Placement = tuple[str, tuple[int, int, int], int, int]


def _milp_fill(
    layout: Layout,
    freed: set[Cell],
    grid: VoxelGrid,
    catalog: Catalog,
) -> list[Placement] | None:
    """Exact-cover the freed region with minimum parts (HiGHS MILP)."""
    candidates = _enumerate_placements(layout, freed, grid, catalog)
    if not candidates:
        return None
    cells = sorted(freed)
    cell_index = {cell: i for i, cell in enumerate(cells)}
    rows: list[int] = []
    cols: list[int] = []
    for col, (_, _, _, _, covered) in enumerate(candidates):
        for cell in covered:
            rows.append(cell_index[cell])
            cols.append(col)
    matrix = coo_matrix(
        (np.ones(len(rows)), (rows, cols)),
        shape=(len(cells), len(candidates)),
    ).tocsc()
    result = milp(
        c=np.ones(len(candidates)),
        constraints=LinearConstraint(matrix, lb=1.0, ub=1.0),
        integrality=np.ones(len(candidates)),
        bounds=Bounds(0, 1),
    )
    if not result.success or result.x is None:
        return None
    return [
        (part_key, anchor, yaw, colour)
        for chosen, (part_key, anchor, yaw, colour, _) in zip(
            result.x, candidates, strict=True
        )
        if chosen > 0.5
    ]


def _enumerate_placements(
    layout: Layout,
    freed: set[Cell],
    grid: VoxelGrid,
    catalog: Catalog,
) -> list[tuple[str, tuple[int, int, int], int, int, tuple[Cell, ...]]]:
    candidates = []
    seen: set[tuple[str, tuple[int, int, int], int]] = set()
    for part in catalog.by_category(Category.BRICK, Category.PLATE):
        for yaw in part.orientations:
            offsets = [rotate_offset(cell, yaw) for cell in sorted(part.occupied_cells)]
            for seed in freed:
                for ox, oy, oz in offsets:
                    anchor = (seed[0] - ox, seed[1] - oy, seed[2] - oz)
                    if anchor[2] < 0 or (part.key, anchor, yaw) in seen:
                        continue
                    seen.add((part.key, anchor, yaw))
                    cells = tuple(
                        (anchor[0] + dx, anchor[1] + dy, anchor[2] + dz)
                        for dx, dy, dz in offsets
                    )
                    if not all(cell in freed for cell in cells):
                        continue
                    colour = merge_colour(
                        *(int(grid.codes[x, y, z]) for x, y, z in cells)
                    )
                    if colour is None or not layout.can_place(part, *anchor, yaw):
                        continue
                    candidates.append((part.key, anchor, yaw, colour, cells))
    return candidates
