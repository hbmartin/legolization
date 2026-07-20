# Kollsker's per-layer optimum drifts upward downstream

The `kollsker` strategy solves each layer to a provably minimum brick
count, yet finished end-to-end WORSE than the constructive `bond`
heuristic on two corpus models. This report carries reproducible
evidence from the corpus and ranks the fix space. Written 2026-07-19 on
branch `roadmap-v4`; the measurement tooling
(`scripts/count_trajectory.py`, telemetry value gauges) landed with the
first draft, the fix landed separately — sections 2–4 are from the
pre-fix code state, section 7 records the fix and its measured effect.

## TL;DR

The drift is real but it is NOT kollsker's fault, and it is NOT in
repair or hollow-restore. Kollsker's tilings are simultaneously the
smallest (mushroom 112 vs bond's 130; thin-shell 146 vs 182) AND the
least fragmented (thin-shell 18 components vs bond's 26). The entire
inflation happens inside `improve_connectivity`: its random-maximal
region rewrite is accepted on component count alone, and a single
accepted rewrite added +179 bricks to mushroom's 112-brick minimum
tiling (+295 on thin-shell). The pass is essential — without it these
placements are 2–26 components and unstable — but its count-blind
acceptance taxed the strategy with the most structure to lose, which
is exactly the per-layer-optimal one. Best-of-k acceptance
(`bridge_draws=5`, layered strategies only) fixes the ranking:
mushroom kollsker 269 → 251 (now beats bond's 263), thin-shell
kollsker 417 → 386 (now beats bond's 398), with bond and fast
unchanged or slightly improved.

## 1. Mechanism (verified against the code)

- After tiling, `LayeredStrategy.place` runs `compact_vertical`
  (`merge.py` — reduce-only, plates only) then `improve_connectivity`
  (`merge.py`): while the layout has more than one stud component, a
  growing `k_ring` around the component border is split to 1×1 plates
  and re-tiled by `maximal_random_merge` — a RANDOM MAXIMAL tiling —
  and the candidate is accepted iff the component count strictly
  drops (Luo's Algorithm 5). Historically **brick count was never part
  of the acceptance test**, so each accepted iteration could trade the
  MILP-minimal local structure for random-maximal structure.
- `repair_stability` (`repair.py`) fires only on unstable placements
  and accepts purely on the artificial-link deficit `q`
  (`repair.py:90`) — also count-blind; `_merge_fill` refills freed
  regions with a random regional merge when they exceed the MILP cell
  limit.
- The hollow-restore loop (`pipeline.py`) adds material back to
  the working grid while unstable and re-runs the whole placement per
  round — monotone count growth wherever it fires.
- `final_remerge` (`merge.py`) accepts only candidates that are
  strictly smaller AND no worse in objective AND components — it can
  only claw back, never inflate, and runs identically for every
  strategy.
- Kollsker's stage 2 (`kollsker.py`) is pinned to the stage-1 minimum
  N\* by an equality row — it can re-shuffle which N\* rects are chosen
  but can never spend a brick to buy connectivity — and its
  `_bond_reward` sees only the below layer's `seam_priority`, whose
  `_seams_of` (`engine.py`) misses gap-separated seams and
  seams outside the problem's own footprint. If every minimum-count
  cover shares the same straight seam, no reward term can escape it.
- `bond` bakes stagger into construction (`bond.py`) and its
  layer repair only accepts refills that use FEWER parts
  (`bond.py:234`) — so its tilings hand less work to the count-blind
  passes.

## 2. Evidence: per-phase trajectories (pre-fix, seed 0)

Reproduce: `uv run python scripts/count_trajectory.py MODEL
--strategy NAME` (sha-stamped JSON artifact per run). Corpus
cross-check: end-to-end counts match the committed scorecard rows
(mushroom 269/265, thin-shell 417/398/370).

mushroom (solid stem, hollowed cap overhang):

| phase | kollsker | bond |
|---|---|---|
| tiled (per-layer minimum) | **112** | 130 |
| compact_vertical | 112 | 130 |
| improve_connectivity | 291 (**+179**) | 281 (+151) |
| post-place / repair | 291, stable | 281, stable |
| final_remerge | **269** | **265** |

thin-shell (hollow cylinder, annulus slices):

| phase | kollsker | bond | fast |
|---|---|---|---|
| tiled (per-layer minimum) | **146** | 182 | 203 |
| compact_vertical | 146 | 182 | 203 |
| improve_connectivity | 441 (**+295**) | 436 (+254) | 404 (+201) |
| post-place / repair | 441, stable | 436, stable | 404, stable |
| final_remerge | **417** | **398** | **370** |

Repair and hollow-restore contribute nothing on the default paths
(post-place equals post-connectivity and is already stable);
`final_remerge` claws back 16–38 bricks but cannot undo a +179..+295
inflation. The whole ranking flip lives in one pass.

## 3. Experiments: ablations (pre-fix, seed 0)

`--fail-max 0` disables `improve_connectivity`; `--solid` disables
hollow/restore; `--no-repair` disables ALNS repair. "Pure" =
`--solid --no-repair --fail-max 0` (tiling + compact only).

| model × variant | kollsker | bond | fast |
|---|---|---|---|
| mushroom pure | **84** (2 comps) | 103 (1 comp) | — |
| mushroom default | 269 | 265 | — |
| thin-shell pure | **146** (18 comps) | 182 (26 comps) | 203 (22 comps) |
| thin-shell default | 417 | 398 | 370 |

Conclusions the ablations force:

- **Per-layer optimality holds end-to-end when the count-blind pass is
  off**: pure kollsker beats pure bond by 19 bricks on mushroom and 36
  on thin-shell.
- **Kollsker does not fragment more** — the opposite: thin-shell
  kollsker leaves 18 components to bond's 26 and fast's 22, and its
  mushroom tiling is 2 components to bond's 1 (bond's baked stagger
  self-connects the solid variant). The §1 hypothesis "minimum tilings
  ⇒ straighter seams ⇒ more components" is REFUTED on these models.
- **The pass is essential**: every `--fail-max 0` run ends
  multi-component and unstable (e.g. thin-shell kollsker 21
  components). Removing it is not a fix.
- The inflation is roughly proportional to how much minimal structure
  enters the pass: +179 on a 112-brick tiling vs +151 on a 130-brick
  one (mushroom); +295/146 vs +254/182 vs +201/203 (thin-shell). A
  random-maximal rewrite has more to destroy in a minimal tiling.

## 4. Why per case

- **mushroom**: one accepted rewrite (1 attempt, 1 accept) bridged the
  2-component stem/cap split and re-tiled the k_ring randomly: +179
  bricks. Bond entered with a worse tiling (130) but its stagger left
  a 1-component solid body, so its rewrite touched less. Repair and
  hollow-restore never fired on either default run — they are
  bystanders, not causes.
- **thin-shell**: annulus slices fragment every strategy (18–26
  components). Bridging 18 components meant rewriting essentially the
  whole border ring; the random-maximal result carries none of the
  MILP structure. Fast ends best (370) not because its tiling is good
  (203, the worst) but because random-maximal output resembles a
  coarse heuristic tiling more than a minimal one — the pass
  regresses every strategy toward the same random-maximal mean, so
  entering with better structure bought nothing.

## 5. Ranked recommendations

1. **(a) Best-of-k acceptance in `improve_connectivity` — IMPLEMENTED**
   (`bridge_draws` parameter): per bridging step, draw k=5 candidates
   and accept the bridging draw minimizing `(components, bricks)`.
   Bridging guarantee and termination unchanged; deterministic (same
   rng stream). Applied to the layered strategies only — the greedy
   path keeps the historical single draw because its shipped goldens
   pin exact bytes and, measured, local best-of-k choices shift
   downstream refinement chaotically on tiny models (heart 12 → 29
   bricks, arch 32 → 23, pyramid 124 → 118: two improvements and one
   severe regression from the same change).
2. **(b) Kollsker stage-2 bridging term — NOT INDICATED by the
   evidence.** The hypothesis motivating it (kollsker fragments more)
   is refuted: kollsker already produces the fewest components. A
   below-component coverage bonus could only reduce the number of
   bridging steps, which is not what inflates counts — the accepted
   rewrite is. Recorded for reconsideration only if a future model
   shows kollsker-specific fragmentation.
3. **(c) Repair MILP cell-limit sizing / (d) multi-draw
   `final_remerge`**: no evidence to act on — repair contributed
   nothing on the default paths, and `final_remerge` already
   monotonically improves. Record-only.
4. **Non-recommendations**: MILP ring refill inside
   `improve_connectivity` can reproduce the very seam being repaired
   (an exact-cover minimum of the ring is exactly what fragmented);
   an [N\*, N\*+k] slack row in kollsker forfeits the strategy's
   defining per-layer-minimum guarantee.

## 6. Pointers

- Reproduce: `uv run python scripts/count_trajectory.py mushroom
  --strategy kollsker --seed 0` (and `bond`; `thin-shell` likewise;
  ablation flags in §3).
- `docs/self-evaluation-playbook.md`, `docs/performance-testing.md`,
  `ROADMAP.md` v4 progress notes.

## 7. Outcome (post-fix, seed 0)

`bridge_draws=5` for layered strategies (`engine.py`), default 1
(byte-identical historical behaviour) elsewhere; greedy goldens
byte-exact.

| model | strategy | before | after | Δ |
|---|---|---|---|---|
| mushroom | kollsker | 269 | **251** | −18 |
| mushroom | bond | 265 | 263 | −2 |
| thin-shell | kollsker | 417 | **386** | −31 |
| thin-shell | bond | 398 | 398 | 0 |
| thin-shell | fast | 370 | 370 | 0 |

### v5 addendum: MILP bridge synthesis (the §5.4 recommendation, built)

`BridgeSynthesizer` (placement/layered/bridge.py) re-tiles the repair
ring through the same absolute-slab decomposition the strategies place
with: per-slab exact-cover MILP, stage 1 minimum count subject to a
hard bridging row (≥1 chosen rect must touch two components — the
seam-reproduction trap is infeasible by construction), stage 2 pins
the count and maximizes crossings plus the shared stagger reward. Its
candidate **competes** in `improve_connectivity`'s best-of-k on the
same `(components, bricks)` key — never preempts (a preempting accept
measured +11 bricks on mushroom because the absolute-slab re-tiling
cannot re-phase plate columns).

Measured: on clean straight seams it is optimal — the two-tower
fixture bridges as two 1x4 **bricks** where the random rewrite needs
six 1x4 **plates**; corpus: fast's wide-arch improved (objective
0.4231 → 0.4002, taking the model). On interleaved shell fragmentation
(mushroom 23 components, thin-shell 18) per-slab covers with forced
crossings cannot stitch the stack — the assembled ring re-fragments
(measured mid-build label counts up to 24 vs 18) and the synthesizer's
own component guard declines; the random path still handles those, so
mushroom 251 / thin-shell 386 are unchanged. Closing the shell class
needs a cross-slab formulation with explicit bond variables —
recorded as future work, not attempted in v5.

Kollsker now finishes ahead of bond on both drift models
(251 < 263, 386 < 398); its `improve_connectivity` inflation dropped
from +179 to +155 (mushroom) and +295 to +280 (thin-shell), with
`final_remerge` clawing back more from the leaner accepted rewrites.
All runs stable, single-component. `fast` keeps thin-shell's overall
best (370) — regression-to-the-random-maximal-mean still caps how much
a minimal tiling is worth on heavily fragmented models; closing that
residual gap needs a structure-preserving bridge synthesis, out of
scope here.

### v7 addendum: re-phased candidate enumeration (built, not promoted)

The absolute-slab limitation is now an explicit ablation rather than
an unimplemented hypothesis. `BridgeSynthesizer(rephase=True)` tries
phases 0, 1, and 2 under one placement deadline, measures all cheap
per-slab candidates before flow, escalates the most promising partial
phase first, and chooses by `(components, bricks, phase)`.

The mechanism exposes the predicted shell candidates but does not beat
the existing best-of-k repair:

| model | best re-phased intermediate | competing random repair | final |
|---|---:|---:|---:|
| mushroom | phase 1: 3 components / 196 bricks | 1 / 267 | 251 bricks |
| thin-shell | phase 2: 17 / 380 | 1 component wins | 386 bricks |

Mushroom's other cheap candidates were phase 0 = 22 / 112 and phase 2
= 4 / 193. A larger flow probe reached 2_322 candidates / 44_162 arcs
on phase 0 without improving the final result. The feature therefore
ships opt-in as `--bridge-rephase`, with phase telemetry and
determinism tests, while default results remain unchanged. The
remaining blocker is not candidate visibility alone: the selected
minimal re-phased cover still loses to a heavier fully connected
random repair under the production acceptance key.
