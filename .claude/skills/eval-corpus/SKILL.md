---
name: eval-corpus
description: >-
  Sweep the evaluation corpus (curated meshes plus synthetic stress shapes)
  through every placement strategy, produce an aggregate scorecard, and diff
  against the committed baseline to catch regressions or confirm
  improvements. Use before or after placement/stability/pipeline changes,
  when asked how the project is doing overall, or to find the worst current
  case to improve next.
---

# Evaluate the whole corpus and diff against baseline

One model tells you about one shape; the corpus scorecard tells you whether
the *project* got better or worse. See
`docs/self-evaluation-playbook.md` for the full improvement loop.

## Workflow

1. **Materialize the corpus** (idempotent):

   ```sh
   uv run python scripts/corpus.py generate
   uv run python scripts/corpus.py download   # meshes; needs network once
   uv run python scripts/corpus.py verify
   ```

2. **Sweep.** While iterating on a change, use the fast slice; for the real
   verdict run everything (takes tens of minutes — run it in the
   background):

   ```sh
   # fast slice (~a minute): small synthetics only
   uv run python scripts/eval_corpus.py --traits fast

   # all synthetics (a few minutes) — the committed baseline's scope
   uv run python scripts/eval_corpus.py --kind synthetic

   # meshes too: EXPENSIVE (tens of minutes to hours) — always background
   uv run python scripts/eval_corpus.py --jobs 0 --timeout 900
   ```

   Exit 0 = clean; exit 1 = a manifest expectation failed or a HARD
   regression vs baseline.

3. **Read the output**: the printed markdown table plus
   `eval/runs/<UTC>/scorecard.json`. HARD regressions (buildable-strategy
   count dropped, expectation newly failing, winner objective worsened
   beyond tolerance) must be explained or fixed before merging. `note:`
   lines (winner identity, brick drift) are context, not failures — but a
   winner flip on many models at once deserves a look.

4. **Drill into the worst model** with the other skills:
   compare-strategies on that input for the visual field, then
   inspect-instructions if the weakness is in sequencing.

5. **After an intentional improvement**, refresh and commit the baseline
   at the same scope it was written with (currently synthetic-only):

   ```sh
   uv run python scripts/eval_corpus.py --kind synthetic --write-baseline
   git add eval/baselines/scorecard.json && git commit
   ```

   Never refresh the baseline to make a regression disappear — that is the
   one move that defeats the whole harness.

## Notes

- `--models a,b` / `--traits t1,t2` slice by manifest name/trait;
  `--strategies greedy,fast` limits strategies (slices are fine for
  iteration, but only full runs update the baseline).
- Synthetic models regenerate in memory and are never stale; skipped mesh
  rows mean `download` hasn't run in this checkout.
- `topple-arm` and `sparse-pillars` are *expected* unbuildable
  (`expect_min_buildable = 0`) — they pin physics verdicts; a "PASS" there
  means the pipeline correctly refused them.
- Timings are never compared; seed is fixed (default 0). Seed variance
  itself is a known issue on `thin-shell` (see the playbook).
