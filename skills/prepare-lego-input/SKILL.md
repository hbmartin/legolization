---
name: prepare-lego-input
description: >-
  Inspect, orient, scale, colour, and normalize a source model (.vox,
  .npy, .obj, .stl, or .ply) before brick generation. Use when asked
  whether an input is usable, which way is up, how big the build will
  be, what colours were detected, to fix an ambiguous up-axis, to pick a
  uniform colour for an uncoloured mesh, or to write a normalized
  -prepared bundle for later generation.
license: GPL-3.0-or-later
---

# Prepare a source model for generation

Reads a mesh or voxel file, reports exactly what generation will see —
orientation, scale, colours, components, machine-readable problem
conditions — and can write a `NAME-prepared/` sibling bundle holding a
normalized `.npy` target plus JSON sidecar that the generation skills
accept directly. Every component of the source is always preserved.

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

- Explain the likely outcome in plain language before running anything.
- Inspect before asking: run the inspection first and base every
  question on its reported `conditions`.
- Ask only when a choice materially changes the result — the up axis
  when ambiguous, one uniform colour when none exists — and always
  recommend one option.
- Run the CLI with `--json`; present the verdict, warnings, and the next
  useful action (usually handing the prepared bundle to generation).

## Workflow

1. Inspect: `legolization input inspect IN --json`. Read `data`:
   the input kind and grid shape, recommended studs or plates-per-voxel,
   the colour summary, and the `conditions` list.

2. Handle each reported condition:
   - `ambiguous-up-axis` — ask which way is up, recommending the
     classifier's pick, then rerun with `--up x|y|z`.
   - `no-colour-data` — ask the user for one uniform colour and apply it
     with `--set input.mesh.colour_code=N`.
   - `multiple-components` / `not-watertight` — report them plainly;
     components are always preserved, never dropped or merged.
   - `empty-grid` — the input produced no filled voxels; stop and say so.

3. Size the build only if the user cares about physical size:
   `--target-studs N` fixes the longest side, or
   `--auto-scale MIN MAX` lets generation pick within a range.

4. Write the normalized bundle when the input is settled:

   ```sh
   legolization input inspect IN --write --json
   ```

   This creates the `IN-prepared/` sibling (numeric suffix if it already
   exists) with the normalized `.npy` and its JSON sidecar.

5. Present the prepared path and offer the next step: generate with the
   legolize-model skill using the prepared bundle or the original input
   plus the flags you settled on.

## Presenting results

The `--json` envelope (`legolization.result/v1`) carries `status`,
`exit_code`, `artifacts`, `warnings`, `data` (the inspection report,
schema `legolization.input-inspection/v1`), and `error` on failure.

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | complete | inspection (and `--write`, if given) succeeded |
| 1 | error | unreadable or unsupported input, or invalid usage |

Conditions are not failures: inspection exits 0 and lists them in
`data.conditions` — surface every one of them to the user.

## Advanced controls (only on request)

- `--config PATH` and repeatable `--set KEY=VALUE` override any dotted
  configuration key, e.g. `--set input.plates_per_voxel=1`.
- The written sidecar records orientation, scale, and colour decisions
  so a later bundle run reproduces them exactly.
