---
name: eval-corpus
description: >-
  Sweep the evaluation corpus (synthetic stress shapes by default,
  curated meshes opt-in) through the placement strategies, produce an
  aggregate scorecard in ./legolization-eval/, and diff it against the
  committed baseline to catch regressions or confirm improvements. Use
  before or after placement or pipeline changes, when asked how the
  project is doing overall, or to find the worst current case to
  improve next.
license: GPL-3.0-or-later
---

# Evaluate the corpus and diff the baseline

One model tells you about one shape; the corpus scorecard tells you
whether the *project* got better or worse. This skill materializes the
corpus, sweeps it through the placement strategies, and diffs the
aggregate scorecard against the committed baseline. Outputs land in
`./legolization-eval/runs/`.

## Requirements

Needs `legolization` 0.6.0 or newer — check with `legolization --version`.
If it is missing or older, install the latest stable release with
`curl -LsSf https://uvx.sh/legolization/install.sh | sh`
(PowerShell: `irm https://uvx.sh/legolization/install.ps1 | iex`).
If installation fails, report the failure and stop.

## Conversation contract

- Explain the likely outcome and rough cost first: synthetics take
  minutes; meshes take tens of minutes to hours — run those in the
  background.
- Ask only when a choice materially changes the result and always
  recommend one option; a synthetic-scope sweep is the right default.
- Run the CLI with `--json`; present the scorecard verdict, regressions,
  and the next useful action.
- **Never** pass `--write-baseline` unless the user explicitly confirms
  replacing the committed baseline in this conversation — and never to
  silence an unexplained regression. That is the one move that defeats
  the whole harness.

## Workflow

1. Materialize and check the corpus (idempotent; inputs live in
   user-data storage):

   ```sh
   legolization corpus list --json
   legolization corpus generate --json
   legolization corpus download --json   # meshes: opt-in, needs network once
   legolization corpus verify --json
   ```

   Synthetics regenerate locally and are never stale; meshes join the
   sweep only after `download`, and an unavailable mesh is reported as
   skipped, not failed.

2. Evaluate everything currently available:

   ```sh
   legolization corpus evaluate --json
   ```

   Scope controls: `--kind synthetic|mesh`, `--strategies NAME,...`,
   `--seeds N,N,...`, `--jobs 0` (one worker per candidate, CPU-capped).

3. For finer control, split the phases: `corpus collect` runs
   strategies into resumable candidate artifacts (`--models`,
   `--traits`, `--timeout`, `--fresh` to ignore cached successes), then
   `corpus assemble` builds the scorecard and diffs the baseline
   (`--runs`, `--baseline`, `--tolerance`).

4. Read the scorecard from the newest run. HARD regressions (a
   buildable-strategy count dropped, an expectation newly failing, a
   winner objective worsened beyond tolerance) must be explained or
   fixed before merging. `note:` lines (winner identity, brick drift)
   are context, not failures — but a winner flip on many models at once
   deserves a look. Expected-unbuildable models pin physics verdicts: a
   pass there means the pipeline correctly refused them.

5. Drill into the worst model with the other skills: render-ldraw for
   the visual field, inspect-instructions when the weakness is in
   sequencing. Only after an intentional, explained improvement — and
   explicit user confirmation in this conversation — refresh the
   baseline with `legolization corpus assemble --write-baseline --json`
   (requires the full-kind, every-strategy, seed-0 scope and a
   failure-free evaluation); commit the refreshed baseline afterwards.

## Presenting results

The `--json` envelope (`legolization.result/v1`) carries `status`,
`exit_code`, `artifacts`, `warnings`, `data`, and `error` on failure.

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | complete | sweep clean; no hard regression against the baseline |
| 1 | error | operational error or incomplete collection — say what is missing |
| 2 | unbuildable | an expectation failed or a HARD regression vs the baseline |
| 130 | interrupted | resumable — rerun to continue collection |

Skipped unavailable meshes surface as `note:` warnings with exit 0, not
as failures.
