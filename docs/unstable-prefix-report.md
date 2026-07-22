# Unstable-prefix steps and the limits of the disassembly rescue

Diagnosis of why "prefix unstable (score 1.00); support the overhang by
hand" steps survive the sequencer's rescue paths, with reproducible
evidence from the evaluation corpus. Written 2026-07-18 at the state of
`instructions/sequencer.py` / `search.py` on branch `build-instructions-v2`.
No sequencer changes accompany this report — it ranks the fix space.

## TL;DR

Every flagged step in the corpus belongs to one failure class: **a chunk
whose only route to ground arrives in a LATER band**. Reordering cannot
fix that class — and the experiments below show exactly that: beam
search, wider ready windows, and bigger beam budgets remove essentially
nothing. The rescue machinery is working as designed; the design's unit
of ordering (band-pure chunks, bottom-up) simply cannot express the
support these shapes need. The productive fixes are structural
(subassemblies, support-aware placement), not better search.

## 1. Mechanism (verified against the code)

- A step is emitted `prefix_stable=False` from `emit_verdicts`
  (sequencer.py) when a rescue/beam/band verdict is unstable, or from the
  greedy no-stable branch under `fallback="band"`. Under the default
  `fallback="disassembly"`, **both** degradation modes — readiness
  deadlock and no-stable-prefix-in-window — trigger the
  assembly-by-disassembly rescue (`search.py::disassembly_order`).
- `disassembly_order` walks backward from the complete structure,
  removing the chunk that leaves the stablest remainder (top band first,
  most-collapsing first, LP-testing the top `beam_width` candidates).
  `_choose_removal` never raises: when every candidate remainder is
  unstable it commits to the least-bad one, and the corresponding forward
  prefix is emitted with the warning.
- Chunks are **band-pure and atomic**: all bricks in a chunk share a base
  layer, and neither the greedy loop nor the rescue can split one.
  Forward order is strictly bottom-up by band.
- **Side contacts provide no support**: `graph.py` grounds bricks through
  knob (stud) reachability only. A brick that merely touches a grounded
  neighbour laterally is *floating*, its equilibrium residual cannot be
  zeroed, and `analyze` scores it exactly 1.0 (`in_equilibrium=False`).

Score exactly 1.0 on a prefix therefore means one of:
(i) a floating chunk — no stud path to ground *yet*; or
(ii) a grounded cantilever whose drag exceeds knob capacity *until its
counterweight (a later band) lands*. Both are prefix-time topology
problems, not solver noise.

## 2. Evidence (reproduce: `scripts/check_instructions.py INPUT --json -`)

Seed 0, default config, corpus at `data/corpus/` (`corpus.py generate`):

| model | steps | unstable | flagged steps (floating_after) |
|---|---|---|---|
| two-towers-bridge | 13 | **0** | — clean: deck chunks anchor into both towers at placement |
| heart.vox | 7 | 2 | 3 (1), 4 (1) — the two lobes start as floating islands |
| cantilever | 15 | 1 | 10 (5) — arm chunk before its bonding band |
| letter-t | 16 | 1 | 8 (1) — bar-end chunk beyond the stem |
| wide-arch | 14 | 2 | 7 (4), 8 (5) — mid-span lintel chunks |
| mushroom | 41 | **17** | 8–16, 28–35 — the whole cap ring, twice (floating_after up to 26) |
| suzanne @16 (mesh) | 60 | 3 | 49, 56, 57 — ears/brow cantilevers (from the recorded CLI run) |
| spot @24 (mesh) | 155 | 1 | 146 — head/ear overhang (from the recorded CLI run) |

The contrast case matters: **two-towers-bridge is sequenced perfectly**
because each deck chunk, when placed, rests on studs of *already-placed*
tower tops — the support exists in an earlier band. Every flagged model
lacks exactly that property.

## 3. Experiments: can better search fix it?

`plan_instructions` re-run on the same layouts (seed 0) under four
configurations — default greedy (`beam_width=4`), widened window
(`beam_width=16`), whole-order beam search (`search="beam"`), and beam
with more states (`beam_states=6`):

| model | default | width16 | beam | beam(states=6) |
|---|---|---|---|---|
| heart | 2/7 | 2/7 | 2/7 | 2/7 |
| cantilever | 1/15 | 1/15 | 1/15 | 1/15 |
| letter-t | 1/16 | 1/16 | 1/16 | 1/16 |
| wide-arch | 2/14 | 2/14 | 2/14 | 2/14 |
| mushroom | 17/41 | 16/41 | 16/41 (2.6× slower) | 16/41 (3.7× slower) |

**Conclusion: the search is not the bottleneck.** One mushroom step out
of seventeen is recoverable by wider search; everything else is invariant
across every search intensification. Classification: **genuinely
unorderable at chunk-within-band granularity** — class (a) of the two
failure modes, not heuristic misses. Making `search="beam"` the default
would buy nothing and cost 2–4× sequencing time.

## 4. Why these shapes are unorderable (per case)

- **heart lobes / letter-t bar ends**: the flagged chunk is an island in
  its band; the chunk that joins it to the grounded mass lies in a
  *higher* band (stretcher-bonded above the gap). No same-band order
  grounds it earlier. Legitimate-but-warned: one brick held by hand for
  one step; the sequencer already says so.
- **mushroom cap (the dominant case)**: the cap is 3 brick-bands of
  ring-shaped material overhanging a thin stem. Only the chunks directly
  above the stem have stud support; the ring's outer chunks float until
  the band above bridges them inward. This repeats per cap band — hence
  17 warned steps with floating counts up to 26. A human would build the
  cap as a **subassembly** on the table and seat it on the stem in one
  move; band-pure sequencing cannot express that.
- **wide-arch lintel**: mid-span chunks float until the lintel's own
  upper band ties the span together (contrast: two-towers-bridge's deck
  is a *single* band thick *and* chunk-wide anchored — its chunks reach a
  tower at placement time).
- **cantilever / suzanne ears / spot head**: grounded but over-capacity
  until the counterweight band above lands ("counterweight sits on the
  tail", as the sequencer docstring already admits).

## 5. Ranked recommendations (fix space, not fixes)

1. **Subassembly steps (MPD submodels)** — the only approach that
   resolves the mushroom/arch class outright: detect maximal floating
   clusters that later bond (the per-prefix `floating_after` sets in
   `check_instructions.py` output are exactly these), emit them as a
   sub-booklet built table-up, and one "attach subassembly" step in the
   main sequence. Already on the roadmap as deferred; this report
   supplies the trigger condition and the evidence that nothing cheaper
   works.
2. **Support-aware placement (upstream of sequencing)** — the placement
   engine could prefer merges that stretcher-bond overhang rings to their
   supported neighbours *within* the same band (mushroom's ring chunks
   would then be knob-connected to the stem-supported chunks via shared
   bricks). Changes brick layout, so it must be gated behind the
   corpus scorecard.
3. **Honest booklet framing (cheap, immediate)** — the warnings are
   correct; the booklet could aggregate consecutive warned steps into one
   "temporary support needed for this section" callout with the count of
   held bricks (per-step `floating_after` is already computed). Improves
   the user experience without touching sequencing.
4. **Not recommended**: making `search="beam"` the default (§3: no
   benefit, 2–4× cost); brick-level chunk splitting (helps only the
   sub-case where a chunk bundles deferrable same-band bricks — none of
   the corpus cases are in that sub-case).

## 6. Pointers

- Reproduce any table row: `uv run python scripts/check_instructions.py
  <input> --json - [--render-dir steps/]`.
- The experiment script pattern lives in this report's history (a
  15-line `plan_instructions` loop over `InstructionsConfig` variants).
- Related: `docs/self-evaluation-playbook.md` §6 failure signatures;
  `ROADMAP.md` deferred "MPD subassembly submodels".

## 7. v8 re-measurement: the class is resolved

Re-run 2026-07-22 at the v7 program end (subassembly steps landed in v3
and hardened through v6/v7; press-aware formation in WS-X), same
command, seed 0, current defaults:

| model | steps | unstable |
|---|---:|---:|
| mushroom | 52 (27 sub) | **0** (was 17) |
| wide-arch | 16 (4 sub) | **0** (was 2) |
| cantilever | 16 (3 sub) | **0** (was 1) |
| heart.vox | 8 | **0** (was 2) |
| letter-t | 17 | **0** (was 1) |
| two-towers-bridge | 13 | **0** |

Recommendation 1 (subassemblies) resolved the entire measured class;
recommendation 3 (honest booklet framing) also landed. Recommendation 2
(support-aware placement) is therefore **closed without implementation**
— it changes brick layouts and carries scorecard risk for a failure
class that no longer exists in the corpus. Reopen it only if a future
model reintroduces unstable steps that subassembly extraction cannot
absorb.
