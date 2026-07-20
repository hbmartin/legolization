# v5 pending measurements — circle back

Two long-running measurement jobs were cut short during the v5 program
(user call: too slow for the session); everything below is ready to
re-run verbatim. Neither blocks the v5 code that landed — they close
bookkeeping, not correctness.

## 1. Mesh-kind baseline cut (WS-0, not yet written)

`eval/baselines/scorecard-mesh.json` does not exist yet. The per-kind
baseline machinery landed and is tested; the sweep itself (6 meshes ×
7 strategies, ~30–60 min) kept getting interrupted. Run when a machine
can sit:

    uv run python scripts/eval_corpus.py --kind mesh --write-baseline

The guard writes only on a clean run. Run it AFTER any change that
moves placement or physics, on the state you want as the reference —
currently that is post-`rotate_contact_pattern`-flip (commit 867389c
or later).

## 2. U1 subassemblies-at-scale: the spot pair (WS-3)

`--subassemblies` measured post-physics-flip (seed 0,
`scripts/check_instructions.py`):

| model | default unstable | subassemblies unstable | max prefix score |
|---|---|---|---|
| mushroom | 17/41 steps | **0**/52 | 1.00 → 0.10 |
| heart | 2/7 | 1/8 | 1.00 → 1.00 |
| wide-arch | 2/14 | **0**/16 | 1.00 → 0.02 |
| cantilever | 1/15 | **0**/16 | 1.00 → 0.09 |
| suzanne@16 | 33/60 | 21/77 | 1.00 → 1.00 |
| spot@24 | 80/155 (v4 record) | **not measured** | — |

The missing cell (~20 min):

    uv run python scripts/check_instructions.py data/corpus/meshes/spot.obj \
        --target-studs 24 --up y --subassemblies

Five of six models show clear improvement with no downsides (three go
fully clean; pre-flip and post-flip tables are identical, so the
physics flip is orthogonal). The `InstructionsConfig.subassemblies`
default flip is pre-approved on this evidence; the spot cell is
verification coverage, not a gate — record its number when run.

## 3. spot@24 program-end profile (WS-5)

The v5-end profile set ran pyramid + suzanne only; spot (~9 min) is
deferred:

    uv run python scripts/profile_pipeline.py spot --target-studs 24 \
        --label v5-end --seed 0

Compare the result block against the v4 record (996 bricks / 155
steps); with subassemblies default-on the step count will move — that
is the U1 trade, not a regression. Wall belongs to the same session's
before-run per docs/performance-testing.md.
