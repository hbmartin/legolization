# Self-evaluation playbook

How to judge this project's outputs — visually and quantitatively — and run
a repeatable self-improvement loop. Written for a future Claude session
that has this repo but not this context.

## 1. Symptom → tool

| You want to... | Use |
|---|---|
| just see what a model looks like | `render-ldraw` skill (front/iso/top PNGs) |
| know which strategy is best on one model, or sanity-check a sweep winner | `compare-strategies` skill |
| judge whether the build instructions make sense (dangling parts, step order/sizes) | `inspect-instructions` skill |
| know whether the project got better/worse overall; find the worst case to fix next | `eval-corpus` skill |
| check one metric fact quickly (is X stable? how many bricks?) | run the CLI; exit 0 = buildable, 2 = not |

Run everything with `unset VIRTUAL_ENV` first (a stale env var from another
project poisons `uv run`), and avoid `cd` in compound shell commands (the
shell's venv hook aborts them) — use absolute paths.

## 2. Tooling map

- **CLI sweep**: `uv run legolization INPUT --strategy all --jobs 0
  --report report.json --keep-candidates candidates/` — runs all 6
  strategies, gated lexicographic winner (`src/legolization/compare.py`).
- **Mesh inputs** (since M6): `.obj/.stl/.ply` accepted directly;
  `--up y` for most .obj files, `--target-studs N` for size
  (`src/legolization/mesh.py`). Mesh grids are always aspect-correct.
- **Corpus**: `data/corpus/manifest.toml` (committed truth: pinned mesh
  URLs + sha256, generator names, traits, `expect_min_buildable`);
  `scripts/corpus.py generate|download|verify|list`. Binaries gitignored.
- **Instruction audit**: `scripts/check_instructions.py INPUT --json r.json
  --render-dir steps/` — `verify_plan` invariants + per-step floating
  (dangling) and component counts + step PNG dump. Exit 1 = violation
  (always a bug), 2 = warnings.
- **Corpus sweep**: `scripts/eval_corpus.py` → `eval/runs/<UTC>/
  scorecard.{json,md}`, diffed against `eval/baselines/scorecard.json`
  (committed, currently synthetic-only scope).
- **Renderers**: LeoCAD at `/Applications/LeoCAD.app` works headlessly on
  this Mac (LDView's headless snapshot silently writes nothing); LDraw
  parts at `~/Library/Caches/pyldraw3/2018-02/ldraw`. A PNG on disk is the
  only success signal — never trust exit codes.

## 3. Interpreting the numbers

- **buildable** = `stable AND component_count == 1 AND floating_count == 0`.
  This is a hard gate: nothing rescues an unbuildable candidate.
- **objective_total** — weighted cost/stability/aesthetics/colour sum
  (`placement/base.py`), lower is better, **only comparable between
  candidates on the same input**, never across models.
- **max_score** — worst per-brick stress from the RBE solver. 0 is
  effortless; ≥ 1.0 means a joint at/over capacity (the model collapses);
  0.7–1.0 is standing-but-fragile. Exactly 1.0 on every strategy usually
  means a *global* verdict (toppling — CoM outside the support polygon),
  not a joint problem; see `topple-arm`.
- **maximin_capacity** (N) — Luo's C_M, the extra force the weakest joint
  pair can absorb. Higher is sturdier; use it to compare two buildable
  layouts.
- **unstable_steps** — must be 0 for a booklet you'd hand a human without
  "support this by hand" caveats. One warned unstable step on a genuinely
  hard shape is acceptable; several mean the sequencer's disassembly/beam
  rescues failed.
- **"least-bad" selection** — when no candidate is buildable the sweep
  still returns a winner (fewest components, then lowest stress). Its
  purpose is diagnosis, not shipping.
- **Seconds are noise.** Never compare timings across runs.

## 4. Visual checklists

Whole model (compare-strategies): silhouette matches source; no holes;
colours right; running-bond seams (not aligned stacks); no detached
clusters; slopes stair-step gently.

Per step (inspect-instructions): new bricks rest on structure/ground;
nothing hovers; one coherent region per step; straight-down insertion
possible; view rotations don't disorient.

Trust order: physics gate > your eyes > objective_total. Eyes may veto a
metrics winner; eyes-vs-objective disagreement is a finding about the
weights (`ObjectiveWeights`) worth reporting.

## 5. The improvement loop

1. `scripts/corpus.py verify` — corpus present and honest.
2. `scripts/eval_corpus.py --kind synthetic` (add meshes in a background
   run when the change could affect them).
3. Pick the worst row: expectation FAIL you didn't expect, lowest
   buildable_count, or highest winner objective.
4. Drill in: `compare-strategies` on that model (all six, rendered), then
   `inspect-instructions` if the weakness is sequencing.
5. Localize with §6 and fix the subsystem.
6. Re-run the slice, then the synthetic sweep. Improvements should show as
   higher buildable_count / lower objective; regressions elsewhere exit 1.
7. If the change *intentionally* moved placement output:
   - refresh the baseline: `--kind synthetic --write-baseline`, commit it
     with the code change;
   - regenerate the example goldens exactly as
     `tests/test_examples_regression.py` prescribes (counts and shipped
     `.ldr` together) — never hand-edit either.
8. Never refresh a baseline to silence a regression you can't explain.

## 6. Known failure signatures

| Signature | First suspect |
|---|---|
| scattered one-brick steps, oversized steps | `instructions/chunking.py` |
| dangling parts in steps (floating_after > 0) without warnings | `instructions/sequencer.py` / `blocking.py` |
| many unstable prefixes on a buildable model | usually a genuinely unorderable shape (floating-until-later-band) — see `docs/unstable-prefix-report.md`; search tuning does not help |
| floating shell fragments in final model | `hollow.py` restore / merge interplay |
| every strategy unbuildable, max_score exactly 1.0 | toppling (CoM outside base) — shape needs a base, or RBE torque bug |
| every strategy unbuildable on spanning shapes | connectivity/repair (`placement/repair.py`) |
| smga/beauty only ones timing out | `time_budget_s` plumbing, GA generations |
| brick counts vary wildly across seeds on shells | known seed-variance issue (`thin-shell` trait); multi-seed restarts are the roadmap answer |
| mesh model missing limbs | voxelization dropped small components — check the "dropped N voxels" progress warning, raise `--target-studs` |

## 7. Corpus maintenance

- Add a mesh: pin a raw URL at a specific commit of
  `alecjacobson/common-3d-test-models`, download once, record sha256 (from
  `shasum -a 256`), set `up`/`target_studs`/traits/expectation, note the
  licence (that's why meshes aren't vendored). `corpus.py verify` must
  pass.
- Add a synthetic: write a pure deterministic generator in
  `scripts/corpus.py` (no RNG, no dates), register it in `GENERATORS`, add
  a manifest entry naming the subsystem it stresses, and extend
  `tests/test_corpus.py` trait sanity if the trait is new.
- `expect_min_buildable` encodes *current reality*, not aspiration: 0 for
  physics-impossible shapes (`topple-arm`, `sparse-pillars`). Raise it when
  the pipeline genuinely improves — that ratchet is the point.

## 8. Determinism & runtime notes

- Everything is seeded (default 0); mesh voxelization and synthetic
  generators are fully deterministic — byte-identical reruns.
- Runtime scales hard with `target_studs` (voxels × 2.5 plate layers, an
  LP per instruction step). Synthetics: seconds each. Meshes at 28–36
  studs: tens of minutes *per strategy* is possible — always run mesh
  sweeps in the background, never on the main thread of a session.
- The spawn ProcessPool can't hard-kill a running worker: a `--timeout`
  sweep may return while workers still burn CPU.
- Progress lines print only when stderr is a TTY; background runs are
  silent until done. Check the scorecard file, not the console.

## 9. Current state (2026-07-18)

- Baseline scope: synthetic corpus (10 shapes), committed at
  `eval/baselines/scorecard.json`. Mesh rows are additive future work —
  run them in a long background sweep and widen the baseline when stable.
- Known-hard rows: `topple-arm`, `sparse-pillars` expect 0 by design;
  `thin-shell` has real seed variance.
- The heart example ships with two warned unstable steps (its lobes start
  as floating islands that join later) — the canonical example of a
  *tolerated* dangling step: machine-flagged, warned, buildable.
