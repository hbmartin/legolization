---
name: inspect-instructions
description: >-
  Audit a model's step-by-step build instructions: machine-check step
  ordering, per-step stability, insertion pressure, and dangling bricks
  not yet connected to ground, then read rendered per-step images for
  sensibility. Use when asked whether instructions make sense or are
  buildable, to review step ordering or sizes, to debug unstable-prefix
  warnings, or after changing sequencing behaviour.
license: GPL-3.0-or-later
---

# Inspect build instructions for sensibility

Tests pin the sequencing algorithm; this skill judges the *result* the
way a human builder would: does each step add bricks you can actually
place, onto structure that exists, without anything hovering in mid-air?

## Requirements

Needs `legolization` 0.6.0 or newer — check with `legolization --version`.
If it is missing or older, install the latest stable release with
`curl -LsSf https://uvx.sh/legolization/install.sh | sh`
(PowerShell: `irm https://uvx.sh/legolization/install.ps1 | iex`).
If installation fails, report the failure and stop.

## Conversation contract

- Explain the likely outcome in plain language before running anything.
- Ask only when a choice materially changes the result; the defaults
  audit everything worth auditing.
- Run the CLI with `--json`; present the verdict, the flagged steps
  with your visual confirmation or refutation, and the next useful
  action.

## Workflow

1. Audit (add `--render-dir` when a renderer is available for the
   visual pass):

   ```sh
   legolization instructions audit INPUT --render-dir steps/ --json
   ```

   `INPUT` is a step-annotated `.ldr`/`.mpd` or a bundle directory;
   `--report PATH` moves the JSON report.

2. Read the report first. Per step: `size`, `prefix_stable`,
   `prefix_max_score`, `floating_after` (bricks with no stud path to
   ground after this step), `components_after`, and `flags`;
   `flagged_steps` is the shortlist. Interpretation:
   - `floating` — the step leaves a brick dangling. Sometimes
     legitimate for islands that join later, with an explicit warning;
     always worth eyes on the image.
   - `unstable-prefix` — the half-built model needs support. One such
     step on a hard shape is tolerable *with* its warning; several mean
     the sequencer's rescue paths failed.
   - `insertion-fragile` — statically fine, but pressing the new bricks
     home would collapse the prefix.
   - `oversized` — the step exceeds the step-size cap; a chunking
     regression.
   - Invariant `violations` (verdict `infeasible`, exit 2) are never
     tolerable — coverage, support, or blocking errors are always a bug.

3. Read step images from `steps/`. Sampling rule: read **all** steps
   when there are 12 or fewer; otherwise read the first 3, the last 2,
   every flagged step, and evenly spaced fill to about 12 images.

   Per image: highlighted bricks rest on existing structure or the
   ground; nothing hovers unsupported (unless flagged and warned
   deliberately); the step is one coherent spatial region, not
   scattered singles; straight-down insertion is plausible; the view
   rotation between consecutive steps is not disorienting.

4. Report: a verdict (would a human build succeed following these
   steps?), each flagged step with your visual confirmation or
   refutation, and — when auditing after a code change — which
   subsystem to suspect for each real problem.

## Presenting results

The `--json` envelope (`legolization.result/v1`) carries `status`,
`exit_code`, `artifacts` (report and step images), `warnings`, `data`,
and `error` on failure.

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | complete | verdict `certified` — invariants hold, nothing flagged |
| 1 | error | unreadable input or invalid usage |
| 2 | unbuildable | verdict `infeasible` — invariant violations, always a bug |
| 3 | partial | verdict `findings` — flagged steps need the visual pass |

## Advanced controls (only on request)

- The publish-lego-instructions skill produces the end-user booklet;
  use this skill to audit the plan behind it.
