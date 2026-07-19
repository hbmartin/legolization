# Kollsker's per-layer optimum drifts upward downstream

The `kollsker` strategy solves each layer to a provably minimum brick
count, yet finishes end-to-end WORSE than the constructive `bond`
heuristic on two corpus models. This report carries reproducible
evidence from the corpus and ranks the fix space. Written 2026-07-19 on
branch `roadmap-v4`; the measurement tooling
(`scripts/count_trajectory.py`, telemetry value gauges) lands with this
report, the fixes land separately — the evidence here is from the
pre-fix code state.

## TL;DR

TBD after measurement: attribution of the mushroom (+4 vs bond) and
thin-shell (+19 vs bond, worst of all seven strategies) inflation
across the count-blind downstream passes.

## 1. Mechanism (verified against the code)

- After tiling, `LayeredStrategy.place` runs `compact_vertical`
  (`merge.py` — reduce-only, plates only) then `improve_connectivity`
  (`merge.py:280-322`): while the layout has more than one stud
  component, a growing `k_ring` around the component border is split to
  1×1 plates and re-tiled by `maximal_random_merge` — a RANDOM MAXIMAL
  tiling — and the candidate is accepted iff the component count
  strictly drops (`merge.py:315-319`). **Brick count is never part of
  the acceptance test**, so each accepted iteration can trade the
  MILP-minimal local structure for random-maximal structure.
- `repair_stability` (`repair.py`) fires only on unstable placements
  and accepts purely on the artificial-link deficit `q`
  (`repair.py:90`) — also count-blind; `_merge_fill` refills freed
  regions with a random regional merge when they exceed the MILP cell
  limit.
- The hollow-restore loop (`pipeline.py:154-173`) adds material back to
  the working grid while unstable and re-runs the whole placement per
  round — monotone count growth wherever it fires.
- `final_remerge` (`merge.py:502-508`) accepts only candidates that are
  strictly smaller AND no worse in objective AND components — it can
  only claw back, never inflate, and runs identically for every
  strategy.
- Kollsker's stage 2 (`kollsker.py`) is pinned to the stage-1 minimum
  N\* by an equality row — it can re-shuffle which N\* rects are chosen
  but can never spend a brick to buy connectivity — and its
  `_bond_reward` sees only the below layer's `seam_priority`, whose
  `_seams_of` (`engine.py:252-264`) misses gap-separated seams and
  seams outside the problem's own footprint. If every minimum-count
  cover shares the same straight seam, no reward term can escape it.
- `bond` bakes stagger into construction (`bond.py:151-192`) and its
  layer repair only accepts refills that use FEWER parts
  (`bond.py:234`) — so its tilings hand less work to the count-blind
  passes.
- Hypothesis to be tested: minimum-count tilings prefer long bricks ⇒
  straighter seams and fewer vertical stud crossings ⇒ more
  disconnected components (and on overhang models, more instability) ⇒
  more count-blind repair work downstream.

## 2. Evidence (reproduce: `uv run python scripts/count_trajectory.py MODEL --strategy kollsker`)

TBD: per-phase brick/component/stability trajectory tables for
mushroom and thin-shell × {kollsker, bond} (+ fast on thin-shell),
seed 0.

## 3. Experiments

TBD: ablation matrix — default vs `--no-repair`, `--solid`,
`--fail-max 0`, and `--no-repair --fail-max 0` (pure tiling + compact,
where post-realize kollsker MUST be ≤ bond if per-layer optimality
holds).

## 4. Why per case

TBD after measurement: mushroom (solid stem + hollowed cap overhang —
repair/hollow-restore live) vs thin-shell (already-hollow annulus
slices fragmenting into many components — `improve_connectivity` prime
suspect; kollsker solves per 4-connected component).

## 5. Ranked recommendations

TBD, drawn from: (a) count-aware best-of-k acceptance in
`improve_connectivity`; (b) a bridging term in kollsker's stage 2 from
below-component coverage; (c) repair MILP cell-limit sizing; (d)
multi-draw `final_remerge`. Non-recommendations recorded with reasons
(MILP ring refill can reproduce the very seam being repaired; an
[N\*, N\*+k] slack row forfeits the strategy's defining guarantee).

## 6. Pointers

- Reproduce: `uv run python scripts/count_trajectory.py mushroom
  --strategy kollsker --seed 0` (and `bond`; `thin-shell` likewise).
- `docs/self-evaluation-playbook.md`, `docs/performance-testing.md`,
  `ROADMAP.md` v4 progress notes.
