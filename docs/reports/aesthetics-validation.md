# Report: beauty-term validation

*2026-08-09. Closes ROADMAP's "validate the beauty scalar against human
judgement". Instruments: `scripts/aesthetics_baseline.py` (population
separation + promotion gates) and `scripts/aesthetics_drift.py` (permutation
drift). Reports quoted here regenerate under `eval/datasets/` (untracked);
this page records the standing verdicts and the decisions taken on them.*

Two independent methodologies were run against the same terms: a
**population comparison** (do official-set skeletons from the LDraw OMR score
better than our output, and our output better than StableText2Brick's
delete-and-rebuild contrast class?) and a **permutation drift** check adapted
from the graph-generative literature (progressively vandalize official-set
skeletons — delete/move/recolour/swap, validity-preserving — and ask whether
the term notices). A term must satisfy *both* to carry objective weight; the
weight's magnitude comes from the judged-preference program
(`scripts/fit_preference_weights.py`) rather than intuition.

## Verdict summary

| term | population ordering | drift detection | default weight | classification |
| --- | --- | --- | ---: | --- |
| `symmetry` (global-plane v2) | correct (human best) | **PASS** (mean ρ 0.77, 91% +, drift +0.56) | 0.25 | the one live beauty term |
| `layer_symmetry` (Min g_a, v1) | correct | PASS (0.79 / 99% / +0.36) | — | superseded; kept for comparison |
| `perpendicularity` | **inverted** | **fail** (0.10 / 55%) | 0.0 | structural diagnostic only |
| `speckle` (audition) | inverted | fail (0.44 / 77%) | 0.0 | measures palette richness, not beauty |
| `profile` (audition) | inverted | PASS (0.78 / 97%) | 0.0 | measures shape complexity, not beauty |

## Population medians (sample 200 per external population, min 20 bricks)

| term | human (OMR) | ours | algorithmic (S2B) | reading |
| --- | ---: | ---: | ---: | --- |
| `perpendicularity` | 0.649 | 0.542 | 0.468 | *backwards*: the worse a population looks, the "better" it scores |
| `symmetry` (v2) | 0.322 | 0.642 | 0.685 | right way; ~2× headroom to the human median |
| `speckle` | 0.373 | 0.027 | 0.000 | humans colour-block *more* junctions — deliberate multi-colour design |
| `profile` | 0.363 | 0.062 | 0.100 | human sets carry overhangs/greebles; our voxel fills are smooth |

The human `symmetry` distribution is strongly bimodal (p25 0.06, p75 0.93):
most official sets are almost perfectly globally mirror-symmetric
brick-for-brick, and the asymmetric tail is real asymmetric machines (the OMR
locomotives sit at 0.94–0.99). That spread is why the Mann-Whitney gate
(p < 0.01, human < ours) is not yet cleared even though the ordering is
right — the gate stays unmet, the weight stays at its long-standing 0.25, and
the preference program is the instrument that can justify moving it.

## Decisions taken

1. **`perpendicularity` demoted to weight 0.0** (was 0.25). Both
   methodologies agree it does not measure what makes a build look right:
   official sets use *more* parallel stacking (coherent walls and studs-up
   texture), and vandalising a set barely moves the term. The computation,
   report fields, and TOML key survive — re-enable with
   `[placement.weights] perpendicularity = 0.25` — and the term is
   reclassified as a structural bonding diagnostic beside `seam_alignment`.
2. **`symmetry_error` replaced in place with the global-plane form.** Min's
   per-layer g_a let every layer choose its own mirror axis *and* centre, so
   a staircase of individually symmetric layers scored perfect; the global
   form fixes both blind spots, keeps the field name (zero schema churn), and
   detects drift with a larger effect size (+0.56 vs +0.36). The v1 stays
   exported as `layer_symmetry_error` for side-by-side measurement. The
   `beauty` strategy's beam search now aims at the same global mirror centre
   the objective measures (`BeautyStrategy.place` fixes it from the target
   grid before tiling).
3. **Both audition terms stay weightless.** `speckle` was auditioned as a
   dithering detector but the populations show it measuring palette richness
   — a *target-conditioned* variant (charge only junctions the source model
   wanted same-coloured; needs the grid, so it would live in
   `placement/base.py`) is the credible successor. `profile` passes drift but
   measures shape complexity at population level; it stays a
   voxelization/finishing diagnostic. Neither may gain weight without
   passing all three gates: strict `human < ours < algorithmic` ordering,
   Mann-Whitney p < 0.01, drift PASS.
4. **Weight magnitudes come from judged preferences.** The paired-comparison
   program (playbook §4b; `judge-aesthetics` skill; tracked log in
   `references/aesthetic-preferences/`) accumulates forced-choice verdicts —
   Claude judges, the human settles escalations — and
   `scripts/fit_preference_weights.py` turns them into Bradley-Terry latent
   scores and *recommended* (never auto-applied) weights, adopting the
   methodology of Dev, arXiv:2505.12373, whose own dataset is unreleased
   (registry entry `shape-aesthetics-pairs`, `available = false`).

## Drift-harness design notes (why these numbers are trustworthy)

Trajectories run `min(300, 2 × bricks)` operations — beyond a couple of
operations per brick a layout is fully scrambled and every checkpoint samples
noise around the scrambled equilibrium, which erases rank correlation without
saying anything about detection (measured: a 48-brick model saturates by ~90
operations). Series whose *starting* value already exceeds 0.9 are excluded
per term and counted as saturated — a reference with no headroom cannot
exhibit detection; for `symmetry` those are exactly the locomotives. Verdict
criterion: mean Spearman ρ ≥ 0.6 and ρ > 0 in ≥ 80% of the informative
series, over 25 OMR skeletons × 3 seeds.

## Falsifiable expectations going forward

- Optimizing v2 symmetry harder (beauty presets, merge acceptance) should
  move our median from 0.64 toward the human 0.32 *without* moving
  `max_score` or buildability — re-run the baseline after each change.
- If the preference fit, at honest sample sizes, assigns `symmetry` a weight
  far from 0.25 or resurrects a demoted term with high sign-consistency, the
  fit wins — that is the calibration instrument this report exists to feed.
