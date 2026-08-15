# Warm solving

**Source:** `stability/prefix.py` · motivated by profiling, *measured*
{ .provenance }

Profiling found the sequencer's per-prefix LP at roughly **99% of large-model
runtime** — 299 solves at about 4.9 s each on an instance with $n \approx 1000$.
Model construction was under 1%. The conclusion was unambiguous: reduce solve *cost*
and solve *count*, do not cache model construction.

---

## The structure the sequencer hands you

Instruction sequencing asks the same question repeatedly about a **monotonically
growing** set of bricks: does the first $k$ chunks' partial assembly stand?

Each question differs from the last by one chunk. Re-solving from scratch throws away
everything the previous solve learned.

---

## `PrefixSolver`

One `highspy.Highs` model is kept alive across the whole growing prefix.

| Operation | Cost |
| --- | --- |
| `probe(chunk)` | Append the chunk's rows and columns, re-solve **warm from the retained simplex basis** — iterations proportional to the chunk, not to the model |
| `commit(chunk)` | Free (lazy rollback bookkeeping only) |
| reject | Delete the trailing rows/columns, restore the saved base basis |

Three implementation details are load-bearing:

**Presolve is off.** Required for basis reuse — presolve rewrites the problem, so the
retained basis would not correspond to it. A convenient side effect is immunity to the
degenerate-presolve failure that forces the cold path's retry chain.

**The solver owns its own numbering.** An append-order `(brick, contact) → (row,
column)` map, decoupled from `build_model`'s sorted-id ordering, because placement ids
interleave across chunks and appending in sorted-id order is impossible. Coefficients
still come from the same shared `force_entries`, so the arithmetic is identical.

**It shares the `ConnectionGraph` with the cold model.** Rebuilding contacts
independently would risk vertical-only SNOT mistakes and phantom cladding faces —
exactly the class of bug the shared graph exists to prevent.

---

## The floating shortcut

The single largest win, and it uses no LP at all.

!!! success "Graph reachability replaces an LP"

    A prefix containing a brick with **no stud path to ground** can never be in
    equilibrium — there is no force chain that could support it.

    So its verdict needs no solve: unstable, with the floater scored exactly 1.0.

The dominant class of unstable prefix — a chunk placed before the structure that will
eventually hold it up — collapses to a graph traversal. On models with any
floating-until-later-band structure, this removes most of the sequencer's LP calls
outright.

---

## The insertion press

`press_probe(chunk, extra_mass_kg)` implements Liu et al.'s virtual-brick insertion
model: the force a builder applies pressing a piece home.

The elegant part is that it changes **only row bounds** — the gravity vector $b$ —
never the matrix. So the warm basis stays valid and re-converges in a few dual-simplex
iterations.

!!! warning "Press bounds are always restored before returning"

    Otherwise a following `commit` would bake the press into the base model, and every
    subsequent prefix would be solved under a phantom load that no longer applies.

`press_probe_selection` presses a subset, which is what the chunk-split refinement uses
when a whole chunk fails the press but part of it would pass.

The adjacent-union refinement is bounded to 128 explored states because each admitted
state costs a press LP. If the bound is reached, the sequencer keeps the best evaluated
union and emits a warning so a potentially truncated ordering is never silent.

---

## `RemovalSolver`

The disassembly rescue has the opposite structure: states that stay large and
*shrink*.

Warm-starting does not help there. Removing a chunk deletes many basic variables at
once, and HiGHS effectively cold-starts. So `RemovalSolver` exploits a different
property.

!!! success "Block diagonality"

    The RBE has **no knob, side, or ground coupling between contact components**. The
    matrix is block-diagonal by contact component.

    Therefore the concatenation of per-block optima is an optimum of the joint LP, and
    per-component results merge exactly.

Consecutive rescue states differ in only a few components, so only the changed
components are re-solved. Components at or above `rescue_direct_min_bricks` (200) route
through the direct highspy path; smaller ones keep the scipy-exact path that the
equivalence tests pin at $10^{-6}$.

---

## Cross-engine exactness

The LP polytope is **identical** across engines. Verdicts agree except through
solver-tolerance-level alternative optima on degenerate states — the same drift scipy
shows across its own versions.

Three defences:

1. **A boundary guard.** Any prefix whose verdict sits near the stability threshold is
   cold-solved. `boundary_margin` (default 0.02) sets the band.
2. **Fallback on non-optimal status.** Every warm solve that does not return optimal
   falls back to the cold `analyze`.
3. **`engine_cross_check`.** Makes every warm probe *also* cold-solve, return the cold
   result, and record the drift. Debug and CI only — it removes the entire benefit.

---

## Caching in the sequencer

Two caches, keyed differently on purpose:

| Cache | Key | Why |
| --- | --- | --- |
| Stability | `frozenset(cumulative brick ids)` | The verdict depends only on the set |
| Press | `(frozenset(placed), frozenset(chunk))` | **Not** prefix-set-only |

The press cache key is the subtle one: the same final set, reached by pressing a
*different* chunk last, is a different load case. Keying it on the resulting set alone
would return a verdict for a press that never happened.

When the warm engine is disabled — during a rescue, a beam search, or a band fallback —
warm-authored cache entries are **evicted**, because those paths use float-order scores
that the warm entries were not computed under.

---

## What this buys, and what it costs

| | |
| --- | --- |
| **Buys** | The dominant runtime cost of large models, reduced from one full solve per step to an incremental solve per step, with the largest unstable class removed entirely |
| **Costs** | A second solver path to keep in sync with the cold one, its own row/column numbering, and a boundary guard to catch the disagreements |

The sync risk is managed by sharing the two things that must agree — `force_entries`
for the arithmetic and `ConnectionGraph` for the topology — and by the cross-check
mode for when you need to prove it.

The performance methodology, including what counts as a regression and the
pre-registered thresholds, is in the
[performance testing guide](../../guides/performance-testing.md).
