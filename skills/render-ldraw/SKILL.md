---
name: render-ldraw
description: >-
  Render an LDraw model (.ldr or .mpd) or a bundle directory to PNG
  images from front, iso, and top views so it can be visually
  inspected. Use whenever asked to see, view, preview, screenshot,
  render, or visually check a generated model — or to confirm a
  placement, colour, or stability change looks right in the actual
  bricks rather than only in numbers.
license: GPL-3.0-or-later
---

# Render and visually inspect a model

Tests confirm geometry and physics, but they don't show what the model
*looks* like. This skill renders a model (or a bundle — the primary
model is found through its `bundle.json`) to PNGs and reads them back so
shape fidelity, colours, gaps, and floating bricks can be judged by eye.

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
- Never install a renderer without asking; `--check` costs nothing and
  is always safe.
- Run the CLI with `--json`; present the image paths, what the images
  show, warnings, and the next useful action.

## Workflow

1. Check for a renderer using this skill's script:

   ```sh
   bash scripts/setup-renderer.sh --check
   ```

   (Windows: `scripts/setup-renderer.ps1 -Check`.) It prints
   `renderer=NAME` or `renderer=NONE`.

2. If `NONE`: explain the per-platform install — LDView via Homebrew on
   macOS, LeoCAD via winget on Windows, LeoCAD plus Xvfb via apt on
   Ubuntu — and **ask for consent** before running the script's install
   mode (the same script with no flag). Report an install failure and
   stop; never improvise a different renderer.

3. Render:

   ```sh
   legolization model render MODEL --json
   ```

   The input may be a `.ldr`/`.mpd` file or a bundle directory. Useful
   flags: `--views iso` (single angle, faster; default is
   `front,iso,top`), `--size 1600` (wider images), `-o DIR`. Existing
   images are never overwritten — new renders take numeric suffixes, and
   prior renders are retained even when the renderer or parts library
   changes, so a change can be diffed against the previous look.

4. Read every produced PNG with the Read tool and apply the checklist:
   - silhouette matches the source shape (no missing limbs or regions)
   - no unexpected holes or pits in surfaces
   - colours match the input (no banding or stray colours)
   - seam pattern: running-bond staggering, not tall aligned stacks
   - no visibly detached or floating clusters
   - staircase artifacts on slopes are expected; gross terracing is not

5. Describe what the images show against what was intended, then name
   the next useful action (analysis for suspected instability,
   compare-style sweeps for a wrong-looking placement).

## Presenting results

The `--json` envelope (`legolization.result/v1`) lists each image under
`artifacts` with `status`, `exit_code`, `warnings`, and `error` on
failure. Judge success by the PNGs on disk, and read them — never
describe an image you have not opened.

| Exit | Status | Meaning |
| --- | --- | --- |
| 0 | complete | every requested view rendered |
| 1 | error | no renderer, unreadable input, or invalid usage |
| 3 | partial | some requested views rendered, some failed — say which |

## Advanced controls (only on request)

- The CLI honours a renderer-override environment variable (the package
  name upper-cased, plus `_RENDERER`; values `ldview`, `leocad`, or
  `none`) when detection must be forced for one shell.
- The renderer and parts-library versions are recorded alongside the
  renders for reproducibility.
