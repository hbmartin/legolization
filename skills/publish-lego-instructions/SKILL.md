---
name: publish-lego-instructions
description: >-
  Produce step-by-step building instructions for a model: a
  step-annotated MPD plus an instruction audit always, and an HTML/PDF
  booklet when a renderer is available. Use when asked for build
  instructions, a building guide or booklet, printable steps, Letter or
  A4 output, or to publish or export instructions for a generated or
  imported model.
license: GPL-3.0-or-later
---

# Publish building instructions

Turns a model — a native 3D input or an existing `.ldr`/`.mpd` assembly —
into a `NAME-instructions/` bundle: a step-annotated MPD and an
instruction audit always, and an HTML/PDF booklet with rendered step
images when a renderer is available. Subassemblies and insertion-press
auditing are always on; step density adapts to model size.

## Requirements

Needs `legolization` 0.6.0 or newer — check with `legolization --version`.
If it is missing or older, install the latest stable release with
`uv tool install legolization@latest`
(or `pip install --upgrade legolization`). Only if neither package
manager is available, fall back to the standalone installer:
`curl -LsSf https://uvx.sh/legolization/install.sh | sh`
(PowerShell: `irm https://uvx.sh/legolization/install.ps1 | iex`).
If installation fails, report the failure and stop.

## Conversation contract

- Explain the likely outcome in plain language: the MPD and audit always
  arrive; the booklet depends on a renderer being available.
- If no renderer is installed and the user wants the booklet, offer the
  render-ldraw skill's setup flow (it asks consent before installing).
- Ask only when a choice materially changes the result and always
  recommend one option; `--render auto` is the right default.
- Run the CLI with `--json`; present the bundle path, verdict, warnings,
  and the next useful action (inspect the instructions, print, render).

## Workflow

1. Publish with the default rendering policy:

   ```sh
   legolization bundle MODEL --render auto --json
   ```

   For an `.ldr`/`.mpd` assembly this writes the `MODEL-instructions/`
   sibling with the assembly preserved exactly as built. `--render`
   semantics:
   - `auto` — no renderer means the HTML/PDF booklet is omitted
     entirely, cleanly, exit 0; the MPD and audit still arrive.
   - `required` — an unavailable or failed renderer makes the run
     partial (exit 3); use when the booklet is the point.
   - `off` — never render; intentionally MPD-and-audit only.

   If only some steps render, the booklet is published as partial with
   explicit missing-step markers — never silent placeholders. Say which
   steps are marked.

2. Verify the published steps:

   ```sh
   legolization instructions audit MODEL-instructions --json
   ```

   Surface `flagged_steps` and per-step flags verbatim; hand deep
   step-quality review to the inspect-instructions skill.

3. Present the bundle path, whether the booklet was produced, page size
   (Letter or A4 follows the user's locale, Letter fallback; booklets
   are English-only), and any warnings.

## Presenting results

The `--json` envelope (`legolization.result/v1`) carries `status`,
`exit_code`, `artifacts`, `warnings`, `data`, and `error` on failure.

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | complete | instructions published (booklet omitted cleanly under `auto` with no renderer) |
| 1 | error | operational error or invalid usage |
| 2 | unbuildable | the model failed physics; instructions cannot be certified |
| 3 | partial | `--render required` without a working renderer, or marked missing steps |
| 130 | interrupted | resumable — rerun the same command to continue |

## Advanced controls (only on request)

- `-o DIR` overrides the sibling; `--fresh` forces a from-scratch rerun.
- `--set KEY=VALUE` tunes instruction construction; `--catalog` /
  `--catalog-estimates` bring extended parts into certification.
- `instructions audit --render-dir DIR` also emits per-step PNGs for
  visual review.
