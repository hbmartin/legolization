---
name: render-ldraw
description: >-
  Render a legolization LDraw model (.ldr/.mpd) to PNG images from front, iso,
  and top angles so it can be visually inspected. Use whenever asked to see,
  view, preview, screenshot, render, or visually check a generated model — or
  to confirm a placement/color/stability change looks right in the actual
  bricks rather than only in tests.
---

# Render & visually inspect an LDraw model

`legolization` emits `.ldr`/`.mpd` files. Tests confirm geometry and physics,
but they don't show what the model *looks* like. This skill renders a model to
PNGs and then reads them back so you can judge shape fidelity, colors, gaps,
floating or missing bricks, and obvious instability.

## Files

- `render.py` — renders a model to `<prefix>.{front,iso,top}.png` next to it.
- `preflight.sh` — installs a headless renderer + LDraw parts library.

## Workflow

1. **Ensure a renderer exists** (once per environment). If `render.py` reports
   `renderer: NONE`, run:

   ```sh
   bash .claude/skills/render-ldraw/preflight.sh
   ```

   On Ubuntu/Debian this installs LeoCAD, Xvfb, and the `ldraw-parts` library
   (`/usr/share/ldraw`). On macOS it points you at LDView.

2. **Generate a model to inspect**, e.g.:

   ```sh
   uv run legolization data/examples/heart.vox -o heart.ldr
   ```

3. **Render it**:

   ```sh
   python .claude/skills/render-ldraw/render.py heart.ldr
   ```

   Useful flags: `--views iso` (single angle, faster), `--size 1600x1200`
   (higher resolution), `--prefix NAME`, `--ldraw-dir DIR` (override the
   parts-library location). The script prints `RENDERED: <path>` for each image
   and exits 0 if at least one was written.

4. **Read each `RENDERED:` PNG** with the Read tool to view it, then describe
   what you see and compare it against the intended model — silhouette matches
   the source voxels, colors are right, no unexpected holes, no bricks floating
   free of the structure.

## How it works

- Auto-detects a renderer (`ldview` → `leocad`) and the LDraw parts library.
  A model file only references parts by id, so the renderer needs that library
  to draw anything beyond the most basic bricks (slopes and tiles render blank
  without it).
- Success is decided by a non-empty PNG on disk, **not** the exit code: an
  unconfigured LDView exits 0 without writing a file, and LeoCAD under
  `xvfb-run` can exit non-zero after writing a valid one.
- Re-rendering moves the previous images into `previous/<UTC timestamp>/` so
  you can diff a change against the prior look.

## Notes

- Camera angles are `(latitude, longitude)` in degrees: `front (0,0)`,
  `iso (30,45)`, `top (89,0)`. Add or change views by editing `VIEWS` in
  `render.py`.
- To inspect the multi-step build sequence, render the `.ldr` as-is — the
  `0 STEP` metas don't affect a full-model snapshot; open it in LDView or
  BrickLink Studio to step through interactively.
- The generated `*.png` and `previous/` directories are build artifacts; don't
  commit them.
