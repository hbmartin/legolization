# Metaheuristics

Three layered strategies that search rather than construct: a greedy merge to fixpoint,
a genetic algorithm, and a beam search.

---

## `fast`

**Source:** Bao, Zhang, Fan, Simeone — *Streamlining LEGO model design*, Procedia CIRP
126:945–950, 2024 · `placement/layered/fast.py`
{ .provenance }

### Cost

$$
C = w_s\,\overline{\text{size}} + w_n\,\frac{|S|}{|\text{columns}|} + w_d\,\mathrm{parallel\_fraction}
$$

$$
\text{size}(r) = \frac{A_{\max} - \text{area}(r)}{A_{\max}}, \qquad A_{\max} = 16\ (2{\times}8)
$$

Defaults $w_s = 0.6$, $w_n = 0.2$, $w_d = 0.2$ — the size term must dominate.
`parallel` is true when a rect's long axis matches any covered column's support long
axis, so perpendicular stacking is rewarded.

### Merge to fixpoint

This is the interesting engineering. Naively, choosing the best merge among $N$ rects
means recomputing $C$ for each of $O(N^2)$ candidate pairs — $O(N)$ work per evaluation.

The observation: **every merge drops the count by exactly one.** So the $w_n$ term
changes identically for every candidate merge, and among the current rects the merge
minimizing the new cost is exactly the merge minimizing the **pair-local delta**

$$
\Delta = w_s\,\Delta\text{size} + w_d\,\Delta\text{parallel}
$$

which is $O(1)$ to compute per pair.

That delta keys a **lazy heap** with stale-entry skipping. Candidate pairs come from a
column-owner adjacency index — only rects tiling their joint bounding box can merge at
all — so the pair set is sparse rather than quadratic.

The loop **breaks** when the best remaining merge no longer improves the cost, rather
than running to exhaustion.

### Escapes and retries

`_split_and_remerge` makes three escape attempts: split a random rect, `random_fill`
the hole, re-merge to fixpoint, keep if cheaper.

`tile` implements Bao's regenerate-on-disconnect: up to 10 regenerations with a fresh
RNG substream, keeping the lexicographic best

$$
(\text{unsupported},\;\; \text{unanchored},\;\; \text{cost})
$$

where `unanchored` is a grounding term ($w_g = 0.2$ per rect that covers a
floating-supported column without anchoring it — i.e. `grounding_gain == 0`).

Deliberately simplified: no full DFS connectivity search. The engine's connectivity
repair backstops it, and duplicating that work per layer would be wasted.

---

## `smga`

**Source:** Lee, Kim & Myung — *Split-and-Merge-Based Genetic Algorithm*, IEEE Access 6,
2018 · `placement/layered/smga.py`
{ .provenance }

### Representation

A chromosome is a whole-layer list of rects, and it is **always a feasible exact
cover**. Every operator maintains that invariant, so there is no repair-for-validity
step and no infeasible individuals in the population.

### Fitness (maximized)

The paper's eq. 7:

$$
f = \frac{c_1}{n_b} + c_2\left(1 - \frac{1}{1 + n_u}\right) + c_3\left(1 - \frac{1}{1 + n_p}\right)
$$

| Symbol | Meaning |
| --- | --- |
| $n_b$ | Brick count |
| $n_u$ | Distinct lower-layer bricks connected to |
| $n_p$ | Perpendicular coverings |

Defaults $c_1 = 5$, $c_2 = 1$, $c_3 = 1$; population 50, 200 generations, patience 30.

!!! success "The constraint $c_1 > 2(c_2 + c_3)$ is enforced in code"

    `SmGaConfig.__post_init__` raises on violations:
    `"SM-GA requires c1 > 2*(c2 + c3) so brick count dominates"`.

    Both bracketed terms are bounded above by 1, so the connectivity and
    perpendicularity contributions together can never exceed $c_2 + c_3$ — while the
    brick-count term ranges over $c_1/n_b$. The condition is the paper's, and its
    intent is that brick economy stays the dominant signal rather than being traded
    away for cosmetic connectivity.

    Note this is a *weighting discipline*, not a proof that one fewer brick always
    wins: for large $n_b$ the marginal gain $c_1/(n_b(n_b-1))$ from dropping a brick is
    small in absolute terms. The bound keeps the terms in the intended proportion; it
    does not make the objective lexicographic.

The paper ran 1000 generations; 200 plus a fitness-plateau stop covers the same ground.

### Operators

**Selection** is rank-based: ranks $1..n$ over the fitness-sorted order with
$p_i = \text{rank}_i / \sum \text{rank}$, plus elitism. Rank selection rather than
fitness-proportional because the fitness scale varies wildly between layers.

**Crossover** is one-point and **directional**: pick a random axis and a random cut
coordinate, keep parent A's strictly-low-side rects and parent B's strictly-high-side
rects, then `random_fill` the holes left in between. Delete-and-refill is the conflict
resolution — rects straddling the cut are simply dropped.

**Mutation** is split-and-merge: pop a random rect, explode it to 1×1s, pick a random
target and greedily grow it to its largest mergeable union until it cannot grow. The
probability decays linearly from 0.7 to 0.1 over the generations — broad exploration
early, refinement late.

**Termination:** generation cap, fitness plateau (`stale >= patience`), or the layer
deadline.

---

## `beauty`

**Source:** Min, Park, Yang, Yun — *Legorization from silhouette-fitted voxelization*,
KSII TIIS 12(6), 2018 · `placement/layered/beauty.py`
{ .provenance }

!!! note "The paper calls it A*; this is a beam search"

    The module says so in its own docstring. The OPEN list is capped at
    `beam_width = 512`, and the guidance heuristic is **not admissible**. Neither
    condition for A*'s optimality guarantee holds, so calling it A* would be an
    overclaim.

### Search

Nodes expand only through placements covering the **first uncovered column in scan
order**. That restriction is what makes the search tractable: it collapses all
permutations of the same tiling into a single path, so the search explores *tilings*
rather than *orderings of tilings*.

The balance term's mirror **centre and axis are fixed globally before any
tiling**. `BeautyStrategy.place` computes the whole-model footprint's `min + max`
sums once, tiles once with the x axis fixed and once with the y axis fixed, and
post-processes only the lower-cost tiling. Costs equal within `1e-12` are treated as
tied so non-dyadic floating-point accumulation cannot suppress a mathematical tie.
Tied tilings are both finalized, dividing the remaining clock between them as each
axis search does.

Final selection first prefers a **buildable** result: no floating bricks and no more
stud-connected components than the input grid's face-connected island count. If no
finalist reaches that geometric lower bound, a fully ground-reachable layout beats a
floating one. Within the same feasibility tier, `symmetry_error` leads before
floating count, excess component count, and brick count. The tiering matters for flat
mosaics: ground-level bricks have no inter-brick stud edges, so raw component count is
just brick count and must not silently outrank the aesthetics objective. This makes
the search cost agree with the one-axis `symmetry_error` objective without allowing a
lean floating finalist to beat grounded towers or a disconnected finalist to beat a
buildable one.

Allowing each layer to take its own `min(x, y)` would undercharge layouts that flip
axes between layers. The overall deadline is shared between the two runs, and both
start from the same RNG state so the axis is the controlled difference. (The paper
balances each layer about its own bbox; a directly driven `tile()` call still retains
that per-layer centre and better-axis fallback.)

The priority queue is keyed `(accumulated cost, counter, covered, rects)`. On overflow,
the beam is truncated with `nsmallest(beam_width, ...)` and re-heapified. Pruning:
`if best is not None and priority >= best[0]: continue`.

### Cost, in two places

**Per rect, at expansion:**

$$
g_h = w_h\,\frac{A_{\max} - \text{area}}{A_{\max} - 1}
$$

$$
g_{\text{ground}} = w_g\,\frac{\max(0,\; \mathrm{ungrounded\_covered} - \mathrm{grounding\_gain})}{A_{\max}}
$$

$$
g_v = w_v \quad\text{when the rect fails to complete a stackable 3-plate footprint}
$$

The grounding term is clamped **positive-only**, which is required for the best-first
prune to stay sound — a negative cost increment would let a node's priority decrease as
it deepened, invalidating the pruning comparison.

$g_v$ carries a careful colour caveat: a rect that is *colour-incompatible* with the
stackable footprint never had the vertical merge available, so it forfeits nothing and
is not charged.

**At completion:**

$$
g_a = w_a\,\big|\{\text{unbalanced rects}\}\big|, \qquad
g_s = \sum_{\text{seams not bridged by a single rect}} p(\text{seam})
$$

$g_a$ is evaluated for **both** mirror axes with the tiling taking the minimum — a
layer symmetric about $x$ is not punished for asymmetry about $y$. A rect counts as
balanced if it is centred on the layer's mirror axis, or if a same-rect, same-colour
mirror partner exists.

$g_s$ uses the engine's `seam_priority`, so failing to bridge a component-joining seam
costs 1.0 while failing to bridge a redundant one costs 0.1.

### Presets

`placement.beauty_preset` selects $(w_s, w_a, w_h, w_v)$:

| Preset | $w_s$ stability | $w_a$ balance | $w_h$ efficiency | $w_v$ vertical |
| --- | ---: | ---: | ---: | ---: |
| `balanced` | 0.25 | 0.25 | 0.25 | 0.25 |
| `stability` | 0.55 | 0.15 | 0.15 | 0.15 |
| `aesthetics` | 0.15 | 0.55 | 0.15 | 0.15 |
| `efficiency` | 0.10 | 0.10 | 0.40 | 0.40 |

On deadline expiry with no completed node, it falls back to `random_fill` — a feasible
answer beats no answer.

---

## Choosing among them

| | `fast` | `smga` | `beauty` |
| --- | --- | --- | --- |
| Cost | Low | High | High |
| Optimizes | Big bricks, perpendicular | Parts + connectivity | Symmetry, balance, stability |
| Deterministic given seed | ✅ | ✅ | ✅ |
| Deadline-aware | ✅ | ✅ | ✅ |
| Fallback on timeout | Best of 10 retries | Best individual so far | `random_fill` |

All three are deadline-aware, which is why they are the ones that time out first on
large models. If only these are timing out, `placement.time_budget_s` is the knob —
see [Quality tiers and budgets](../../guide/quality-and-budgets.md).
