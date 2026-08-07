---
name: repair-lego-assembly
description: >-
  Search for a validated repair or redesign of an unstable or
  unbuildable LDraw assembly, writing the fix to a -repair sibling
  bundle and never overwriting the source. Use when asked to fix,
  repair, reinforce, stabilize, or redesign a failing brick model, or
  when analysis found problems and the user wants them solved rather
  than only reported.
license: GPL-3.0-or-later
---

# Repair a failing brick assembly

Runs the repair search on an `.ldr`/`.mpd` model: it diagnoses the
failure, then looks for a physics-validated fix — reinforcement first,
escalating to redesign — and writes the result to an `INPUT-repair/`
sibling bundle. The source file is never overwritten, and a failed
search still explains itself and keeps its best rejected candidate.

## Requirements

Needs `legolization` 0.6.0 or newer — check with `legolization --version`.
If it is missing or older, install the latest stable release with
`curl -LsSf https://uvx.sh/legolization/install.sh | sh`
(PowerShell: `irm https://uvx.sh/legolization/install.ps1 | iex`).
If installation fails, report the failure and stop.

## Conversation contract

- Explain the likely outcome in plain language before running anything.
- Recommend the **fast** effort tier first; ask before escalating to a
  longer tier, and for `exhaustive` the user must choose the
  `--time-budget` — never invent it.
- Run the CLI with `--json`; present the repair bundle path, verdict,
  warnings, and the next useful action (render the repaired model,
  publish its instructions).
- Never present a rejected candidate as a fix: if the search failed,
  say why, and point at the retained diagnostics and best-rejected
  model as material for a deeper attempt.

## Workflow

1. Start with the fast tier (60 s budget):

   ```sh
   legolization analyze MODEL.ldr --repair --effort fast --json
   ```

   A validated repair lands in the `MODEL-repair/` sibling bundle
   (numeric suffix if taken) along with the diagnostics that justify it.

2. If fast found nothing or the user wants a better fix, ask before
   escalating:
   - `--effort balanced` — 300 s budget, the default tier.
   - `--effort exhaustive --time-budget SECONDS` — requires the user's
     explicit budget.

3. Present the outcome:
   - Success: the repaired model path, what changed (added, moved, or
     redesigned bricks), and the validation verdict.
   - Failure: the recorded reason the search gave up, plus the retained
     diagnostics and best rejected candidate in the repair bundle —
     clearly labeled as not buildable.

4. Offer next actions: render the repaired model (render-ldraw), publish
   its instructions (publish-lego-instructions), or escalate the effort
   tier with consent.

## Presenting results

The `--json` envelope (`legolization.result/v1`) carries `status`,
`exit_code`, `artifacts`, `warnings`, `data`, and `error` on failure.

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | complete | validated repair written to the -repair sibling |
| 1 | error | operational error or invalid usage |
| 2 | unbuildable | no validated repair found; reason and best rejected retained |
| 3 | partial | indeterminate result — recommendations are unverified |
| 130 | interrupted | resumable — rerun the same command to continue |

## Advanced controls (only on request)

- `--repair-output DIR` picks the repair bundle location; `-o PATH`
  names the repaired model file itself.
- `--seed N` reproduces a specific search; `--support MODE` and
  `--scenario NAME` repair against specific handling loads.
- `--catalog` / `--catalog-estimates` let the search use extended
  parts, estimates carrying their recorded provenance.
