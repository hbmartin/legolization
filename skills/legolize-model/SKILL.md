---
name: legolize-model
description: >-
  Turn a 3D model (.vox, .npy, .obj, .stl, or .ply) into a physically
  stable brick-built model with a complete portable bundle: winning model,
  bill of materials, candidate comparison report, and instructions when
  rendering is available. Use when asked to legolize, brickify, or
  Lego-ify a model, convert a mesh or voxel file to bricks, make a
  buildable brick version of a shape, or run the whole generation
  pipeline end to end.
license: GPL-3.0-or-later
---

# Legolize a 3D model

Takes a mesh or voxel file and produces a `NAME-legolization/` bundle
beside it: the winning physics-validated model, its bill of materials,
the full candidate comparison, step instructions when rendering is
available, and diagnostics — one directory the user can build from,
share, or hand to the other skills.

## Requirements

Needs `legolization` 0.6.0 or newer — check with `legolization --version`.
If it is missing or older, install the latest stable release with
`curl -LsSf https://uvx.sh/legolization/install.sh | sh`
(PowerShell: `irm https://uvx.sh/legolization/install.ps1 | iex`).
If installation fails, report the failure and stop.

## Conversation contract

- Explain the likely outcome in plain language before running anything.
- Inspect the input first; ask only when a choice materially changes the
  result, and always recommend one option. The one standing material
  question is the exhaustive tier's `--duration`.
- Run the CLI with `--json`; present the bundle path, verdict, warnings,
  and the next useful action (render it, publish instructions, analyze).
- Rerunning the same command resumes identity-matched incomplete work by
  default; use `--fresh` only when the user asks for a from-scratch rerun.

## Workflow

1. Refresh the managed parts library — silent, no consent needed — by
   running this skill's `scripts/setup-ldraw-library.sh` (Windows:
   `scripts/setup-ldraw-library.ps1`). A valid existing library survives
   a failed weekly check.

2. Inspect the input and act on its reported `conditions`:
   `legolization input inspect IN --json`. On `ambiguous-up-axis`, ask
   which way is up and recommend the classifier's pick; on
   `no-colour-data`, ask for one uniform colour
   (`--set input.mesh.colour_code=N` on the bundle run).

3. Generate with a quality tier — recommend **balanced**:

   ```sh
   legolization bundle IN --quality balanced --json
   ```

   `fast` = greedy preview, about 2 minutes. `balanced` = full strategy
   sweep plus eligible exact, about 15 minutes (default). `exhaustive` =
   seeds 0-2 and requires the user to choose `--duration SECONDS` —
   never invent that number for them.

4. Present the result: the `IN-legolization/` path, the winner and its
   reason from `comparison/report.json`, warnings, and one next action.

5. On exit 2 (unbuildable), present the failure and the best rejected
   candidate kept in diagnostics, then ask for consent **and** a total
   budget before running the material ladder:

   ```sh
   legolization bundle IN --retry-materials --duration N --json
   ```

   It tries a four-plate shell, a six-plate shell, then solid, sharing
   the budget fairly. Never run it unasked.

## Presenting results

The `--json` envelope (`legolization.result/v1`) carries `status`,
`exit_code`, `artifacts` (path, kind, sha256), `warnings`, `data`, and
`error` on failure.

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | complete | bundle finished; winner published |
| 1 | error | operational error or invalid usage |
| 2 | unbuildable | physics rejected every candidate — offer the retry ladder |
| 3 | partial | provisional best at deadline; resuming may adopt a better result |
| 4 | error | exact-placement limit hit |
| 130 | interrupted | resumable — rerun the same command to continue |

## Advanced controls (only on request)

- `--cancel-pending` stops this bundle's detached workers while keeping
  completed artifacts for a later resume.
- `-o DIR` overrides the sibling; `--render auto|required|off` controls
  booklet rendering; `--config PATH`, `--set KEY=VALUE`, `--catalog`,
  and `--catalog-estimates` tune generation.
