---
name: inspect-instructions
description: >-
  Evaluate a model's step-by-step build instructions: machine-check plan
  invariants, per-step stability, and dangling bricks not yet connected to
  ground, then read rendered per-step images for sensibility. Use when asked
  whether instructions make sense, to review step ordering or sizes, to debug
  unstable-prefix warnings, or after changing the sequencer, chunking,
  blocking, or search code.
---

# Inspect build instructions for sensibility

Tests pin the sequencing algorithm; this skill judges the *result* the way
a human builder would: does each step add bricks you can actually place,
onto structure that exists, without anything hovering in mid-air?

## Workflow

1. **Machine check** (re-runs the deterministic pipeline, then audits):

   ```sh
   uv run python scripts/check_instructions.py INPUT \
       --json report.json --render-dir steps/
   ```

   Exit 0 = clean, 2 = warnings only, 1 = invariant violations (always a
   bug — `verify_plan` found coverage/support/blocking errors).

2. **Read `report.json` first.** Per step: `size`, `prefix_stable`,
   `prefix_max_score`, `floating_after` (bricks with no stud path to ground
   after this step — dangling parts), `components_after`, `flags`
   (`floating` / `unstable-prefix` / `oversized`). `flagged_steps` is the
   shortlist. Interpretation:
   - `floating` — the step leaves a brick dangling. Sometimes legitimate
     for islands that join later (the heart's lobes do this, with an
     explicit "support by hand" warning); always worth eyes on the image.
   - `unstable-prefix` — the LP says the half-built model needs support.
     One such step on a hard shape is tolerable *with* its warning; several
     mean the sequencer's rescue paths failed.
   - `oversized` — step exceeds `max_step_size`; chunking regression.
   - Violations (exit 1) are never tolerable.

3. **Read step images** from `steps/step-NNN.png` (rendered with the new
   bricks highlighted). Sampling: read **all** steps when there are ≤ 12;
   otherwise read the first 3, the last 2, every flagged step, and evenly
   spaced fill to ~12 images total.

   Per image checklist:
   - highlighted bricks rest on existing structure or the ground
   - nothing hovers unsupported (unless flagged + warned deliberately)
   - the step is one coherent spatial region, not scattered singles
   - straight-down insertion is plausible (no placing under an overhang)
   - the view rotation between consecutive steps is not disorienting

4. **Report**: verdict (would a human build succeed following these
   steps?), the flagged steps with your visual confirmation or refutation,
   and which subsystem to suspect for each real problem
   (`instructions/chunking.py` for scattered/oversized steps,
   `sequencer.py`/`search.py` for ordering, `blocking.py` for insertion
   conflicts).

## Notes

- Rendering needs LeoCAD (`/Applications/LeoCAD.app` works headlessly on
  macOS) or LDView; without one you still get the full machine check.
- The booklet path (`uv run legolization INPUT --instructions out.html`)
  produces the same step images embedded in HTML — use it to check the
  end-user artifact; use this skill to *audit* the plan.
- `--strategy`/`--seed`/`--step-size` reproduce any specific pipeline run.
