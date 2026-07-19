"""Alternative sequencing searches: assembly-by-disassembly (Tian et al.).

The greedy forward pass in :mod:`sequencer` degrades in two places: a
readiness deadlock, and a step where no candidate prefix is stable. Both
formerly fell back to unchecked band order; ``disassembly_order`` instead
plans the whole remainder by walking backward from the complete (usually
stable) structure — remove the safest chunk, keep the rest as stable as
possible, reverse — which is Luo's "path of best stability" search grafted
onto Tian's assembly-by-disassembly reduction.

This module returns neutral :class:`ChunkVerdict` records; the sequencer
owns step construction, warnings, and strict-mode raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from legolization.instructions.blocking import chunk_ready
from legolization.instructions.chunking import chunk_centroid
from legolization.stability.solver import analyze

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from legolization.instructions.sequencer import InstructionsConfig
    from legolization.layout import Layout
    from legolization.stability.prefix import RemovalSolver
    from legolization.stability.solver import StabilityResult


@dataclass(frozen=True, slots=True)
class ChunkVerdict:
    """One chunk in forward build order with its analyzed prefix verdict."""

    chunk: tuple[int, ...]
    stable: bool
    max_score: float


def disassembly_order(  # noqa: PLR0913 - the search reads all sequencing state
    layout: Layout,
    *,
    placed: frozenset[int],
    chunks: Sequence[tuple[int, tuple[int, ...]]],
    supports: Mapping[int, set[int]],
    blockers: Mapping[int, frozenset[int]],
    config: InstructionsConfig,
    cache: dict[frozenset[int], StabilityResult],
    remover: RemovalSolver | None = None,
) -> list[ChunkVerdict]:
    """Order the remaining chunks by disassembling the complete structure.

    Starting from ``placed`` plus every remaining chunk, repeatedly remove a
    *removable* chunk — one supporting nothing that stays and with nothing
    above it — preferring removals that leave the most stable remainder;
    the reversed removal order is the forward build order, and each
    verdict is the analyzed forward prefix ending with that chunk (the LP
    for a remainder doubles as the verdict of the previous prefix, so the
    search costs about one extra LP per rescued step).

    Never deadlocks: some chunk in the highest still-present band is always
    removable — same-band bricks cannot support or block each other (bands
    share a base layer), higher bands are gone by maximality, and no
    *placed* brick depends on or blocks a remaining brick because every
    greedy emission passed ``_ready`` (its stranding condition keeps
    unplaced bricks blocked only by unplaced bricks).
    """
    dependents: dict[int, set[int]] = {}
    for above_id, below_ids in supports.items():
        for below_id in below_ids:
            dependents.setdefault(below_id, set()).add(above_id)

    alive = list(range(len(chunks)))
    present = set(placed).union(*(set(chunk) for _, chunk in chunks), set())
    removal: list[ChunkVerdict] = []
    state = _analyze_cached(
        layout, present, config=config, cache=cache, remover=remover
    )

    while alive:
        candidates = [
            position
            for position in alive
            if _removable(chunks[position][1], present, dependents, blockers)
        ]
        if not candidates:
            msg = "disassembly deadlocked; impossible for band-pure chunks"
            raise RuntimeError(msg)
        candidates.sort(
            key=lambda position: (
                -chunks[position][0],
                -_stress(chunks[position][1], state),
                position,
            )
        )
        chosen, next_state = _choose_removal(
            layout,
            candidates=candidates[: config.beam_width],
            chunks=chunks,
            present=present,
            config=config,
            cache=cache,
            remover=remover,
        )
        removal.append(
            ChunkVerdict(
                chunk=chunks[chosen][1],
                stable=state.stable,
                max_score=state.max_score,
            )
        )
        if remover is not None:
            remover.commit_without(chunks[chosen][1])
        present -= set(chunks[chosen][1])
        alive.remove(chosen)
        if next_state is not None:
            state = next_state
    return list(reversed(removal))


def _choose_removal(  # noqa: PLR0913 - candidate scoring reads all search state
    layout: Layout,
    *,
    candidates: list[int],
    chunks: Sequence[tuple[int, tuple[int, ...]]],
    present: set[int],
    config: InstructionsConfig,
    cache: dict[frozenset[int], StabilityResult],
    remover: RemovalSolver | None = None,
) -> tuple[int, StabilityResult | None]:
    """Pick the candidate whose removal leaves the stablest remainder.

    Returns the chosen position and the remainder's verdict (``None`` for
    an empty remainder, which is trivially stable and needs no LP).
    """
    best: tuple[float, int, StabilityResult] | None = None
    for position in candidates:
        rest = present - set(chunks[position][1])
        if not rest:
            return position, None
        result = _analyze_cached(
            layout,
            rest,
            config=config,
            cache=cache,
            remover=remover,
            removed=chunks[position][1],
        )
        if result.stable:
            return position, result
        if best is None or result.max_score < best[0]:
            best = (result.max_score, position, result)
    assert best is not None  # noqa: S101 - candidates is never empty
    _, position, result = best
    return position, result


def _removable(
    chunk: tuple[int, ...],
    present: set[int],
    dependents: Mapping[int, set[int]],
    blockers: Mapping[int, frozenset[int]],
) -> bool:
    """Whether the chunk supports nothing that stays and nothing sits above.

    Removability upward at a state is exactly insertability downward onto
    the remainder (the same column condition), so every forward insert in
    the reversed order is collision-free.
    """
    chunk_set = set(chunk)
    outside = present - chunk_set
    return all(
        not (dependents.get(brick_id, set()) & outside)
        and not (blockers[brick_id] & outside)
        for brick_id in chunk
    )


def _stress(chunk: tuple[int, ...], state: StabilityResult) -> float:
    """Highest per-brick stability score in the chunk (1 = collapsing)."""
    scores = state.scores
    return max(
        (score.score for brick_id in chunk if (score := scores.get(brick_id))),
        default=0.0,
    )


def _analyze_cached(  # noqa: PLR0913 - optional warm-removal plumbing
    layout: Layout,
    brick_ids: set[int],
    *,
    config: InstructionsConfig,
    cache: dict[frozenset[int], StabilityResult],
    remover: RemovalSolver | None = None,
    removed: tuple[int, ...] = (),
) -> StabilityResult:
    key = frozenset(brick_ids)
    if (hit := cache.get(key)) is None:
        if remover is not None and set(remover.scope) - set(removed) == key:
            hit = remover.probe_without(removed)
        else:
            hit = analyze(layout.subset(key), config.solver)
        cache[key] = hit
    return hit


@dataclass(frozen=True, slots=True)
class _BeamState:
    """One partial build order under consideration."""

    positions: frozenset[int]  # chunk positions already placed
    bricks: frozenset[int]
    emitted: tuple[ChunkVerdict, ...]
    order: tuple[int, ...]  # chunk positions in placement order
    unstable: int = 0
    score_sum: float = 0.0
    previous_centroid: tuple[float, float] | None = field(default=None, compare=False)

    @property
    def badness(self) -> tuple[int, float, tuple[int, ...]]:
        """Lexicographic rank: fewest unstable prefixes, lowest scores."""
        return (self.unstable, self.score_sum, self.order)


def beam_order(  # noqa: PLR0913 - the search reads all sequencing state
    layout: Layout,
    *,
    chunks: Sequence[tuple[int, tuple[int, ...]]],
    supports: Mapping[int, set[int]],
    blockers: Mapping[int, frozenset[int]],
    blocks: Mapping[int, set[int]],
    config: InstructionsConfig,
    cache: dict[frozenset[int], StabilityResult],
) -> list[ChunkVerdict]:
    """Maximal-stability beam search over whole build orders (Luo's path).

    Keeps the ``beam_states`` best partial sequences ranked by (number of
    unstable prefixes, summed prefix scores); unstable expansions are kept
    and accumulate badness rather than being pruned — that is what makes
    this a best-stability-path search instead of a feasibility search. LP
    spend is capped by ``lp_budget`` (default ``8 x chunk count``); at the
    cap the search degrades to greedy (single beam, first-stable early
    exit). A fully deadlocked beam finishes via :func:`disassembly_order`.
    """
    return _BeamSearch(
        layout=layout,
        chunks=chunks,
        supports=supports,
        blockers=blockers,
        blocks=blocks,
        config=config,
        cache=cache,
        centroids=[chunk_centroid(layout, chunk) for _, chunk in chunks],
    ).run()


@dataclass(slots=True)
class _BeamSearch:
    """Mutable state of one :func:`beam_order` run."""

    layout: Layout
    chunks: Sequence[tuple[int, tuple[int, ...]]]
    supports: Mapping[int, set[int]]
    blockers: Mapping[int, frozenset[int]]
    blocks: Mapping[int, set[int]]
    config: InstructionsConfig
    cache: dict[frozenset[int], StabilityResult]
    centroids: list[tuple[float, float]]
    lp_calls: int = 0

    def run(self) -> list[ChunkVerdict]:
        """Advance the beam one chunk per depth until the order is total."""
        budget = (
            self.config.lp_budget
            if self.config.lp_budget is not None
            else 8 * max(len(self.chunks), 1)
        )
        beams = [
            _BeamState(positions=frozenset(), bricks=frozenset(), emitted=(), order=())
        ]
        for _depth in range(len(self.chunks)):
            degraded = self.lp_calls >= budget
            if degraded:
                beams = beams[:1]
            expansions: dict[frozenset[int], _BeamState] = {}
            for state in beams:
                for extended in self._expansions_of(state, degraded=degraded):
                    _keep_best(expansions, extended)
            if not expansions:
                return self._finish_by_disassembly(beams)
            beams = sorted(expansions.values(), key=lambda state: state.badness)[
                : max(1, self.config.beam_states)
            ]
        return list(beams[0].emitted)

    def _expansions_of(self, state: _BeamState, *, degraded: bool) -> list[_BeamState]:
        ready = self._candidates_for(state)
        if not degraded:
            return [self._extend(state, position) for position in ready]
        # Greedy semantics at the LP cap: commit to the first stable
        # candidate, else the least-bad one.
        chosen: _BeamState | None = None
        for position in ready:
            extended = self._extend(state, position)
            if chosen is None or extended.badness < chosen.badness:
                chosen = extended
            if extended.emitted[-1].stable:
                chosen = extended
                break
        return [] if chosen is None else [chosen]

    def _candidates_for(self, state: _BeamState) -> list[int]:
        placed = set(state.bricks)
        ready: list[int] = []
        for position in range(len(self.chunks)):
            if position in state.positions:
                continue
            if chunk_ready(
                self.chunks[position][1],
                placed,
                self.supports,
                self.blockers,
                self.blocks,
            ):
                ready.append(position)
                if len(ready) >= self.config.beam_width:
                    break
        if (
            self.config.spatial_tiebreak
            and state.previous_centroid is not None
            and len(ready) > 1
        ):
            previous_x, previous_y = state.previous_centroid
            ready.sort(
                key=lambda position: (
                    (self.centroids[position][0] - previous_x) ** 2
                    + (self.centroids[position][1] - previous_y) ** 2,
                    self.chunks[position][0],
                    position,
                )
            )
        return ready

    def _extend(self, state: _BeamState, position: int) -> _BeamState:
        bricks = state.bricks | frozenset(self.chunks[position][1])
        result = self._analyze_set(bricks)
        verdict = ChunkVerdict(
            chunk=self.chunks[position][1],
            stable=result.stable,
            max_score=result.max_score,
        )
        return _BeamState(
            positions=state.positions | {position},
            bricks=bricks,
            emitted=(*state.emitted, verdict),
            order=(*state.order, position),
            unstable=state.unstable + (0 if result.stable else 1),
            score_sum=state.score_sum + result.max_score,
            previous_centroid=self.centroids[position],
        )

    def _analyze_set(self, brick_ids: frozenset[int]) -> StabilityResult:
        if (hit := self.cache.get(brick_ids)) is None:
            self.lp_calls += 1
            self.cache[brick_ids] = hit = analyze(
                self.layout.subset(brick_ids), self.config.solver
            )
        return hit

    def _finish_by_disassembly(self, beams: list[_BeamState]) -> list[ChunkVerdict]:
        # Every beam deadlocked: finish the best one by disassembly.
        best = min(beams, key=lambda state: state.badness)
        remaining = [
            self.chunks[position]
            for position in range(len(self.chunks))
            if position not in best.positions
        ]
        tail = disassembly_order(
            self.layout,
            placed=best.bricks,
            chunks=remaining,
            supports=self.supports,
            blockers=self.blockers,
            config=self.config,
            cache=self.cache,
        )
        return [*best.emitted, *tail]


def _keep_best(
    expansions: dict[frozenset[int], _BeamState],
    state: _BeamState,
) -> None:
    """Dedupe by placed set, keeping the lexicographically best path."""
    incumbent = expansions.get(state.positions)
    if incumbent is None or state.badness < incumbent.badness:
        expansions[state.positions] = state
