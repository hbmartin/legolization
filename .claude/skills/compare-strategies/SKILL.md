---
name: compare-strategies
description: >-
  Run every placement strategy (greedy, luo, bond, fast, smga, beauty) on one
  model, render each candidate, and judge them side by side with metrics and
  images. Use when asked which strategy handles a model best, why the sweep
  picked its winner, whether a placement change helped or hurt, or when a
  generated model looks wrong and alternatives should be checked.
---

# Compare all placement strategies on one model

The `--strategy all` sweep already scores every strategy and picks a winner
by gated lexicographic rules (`src/legolization/compare.py`). This skill
adds the part the metrics can't do: **look at every candidate** and judge
silhouette fidelity, surface quality, and colour against the numbers.

## Workflow

1. **Sweep** (input may be `.vox`/`.npy` or, since M6, `.obj`/`.stl`/`.ply`
   with `--up y` for most meshes):

   ```sh
   uv run legolization INPUT -o out.ldr --strategy all --jobs 0 \
       --timeout 300 --report report.json --keep-candidates candidates/
   ```

   Corpus models come from `data/corpus/` (see `scripts/corpus.py list`);
   synthetic ones can be regenerated with `scripts/corpus.py generate`.

2. **Render every candidate** with the render-ldraw skill (iso view first;
   add `front` when shape fidelity is disputed):

   ```sh
   for f in candidates/*.ldr; do
     python .claude/skills/render-ldraw/render.py "$f" --views iso
   done
   ```

3. **Read `report.json`** — for each candidate note `buildable`,
   `objective_total` (lower = better, only comparable within this model),
   `maximin_capacity` (N, higher = sturdier), `brick_count`, `max_score`
   (≥ 1.0 means a collapsing joint), and the winner + reason.

4. **Read every iso PNG back-to-back** and apply the visual checklist:
   - silhouette matches the source shape (no missing limbs/regions)
   - no unexpected holes or pits in surfaces
   - colours match the input (no banding or stray colours)
   - seam pattern: running-bond staggering, not tall aligned stacks
   - no visibly detached or floating clusters
   - staircase artifacts on slopes are expected; gross terracing is not

5. **Judge.** Weighing rules:
   - Visual findings may **veto** a metrics winner (the objective does not
     see silhouette fidelity), but can never rescue a `buildable=false`
     candidate — physics gates first, always.
   - If your eyes and `objective_total` disagree about which buildable
     candidate is best, that disagreement is itself a finding about the
     objective weights (`placement/base.py::ObjectiveWeights`) — report it.

6. **Report** a short verdict: winner, whether you agree, per-candidate
   one-liners, and any weight/strategy bugs the comparison exposed.

## Notes

- `--jobs 0` = one worker per strategy; use `--jobs 1` when debugging (no
  process pool, clean tracebacks).
- smga/beauty honour `--time-budget`; a sweep `--timeout` folds into it.
- For a corpus-wide view instead of one model, use the eval-corpus skill.
