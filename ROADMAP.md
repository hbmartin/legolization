# Roadmap

Future work for legolization, picking up where the initial implementation
stopped. For the algorithms and formulas each item builds on, see the papers in
`references/` and the design notes in `CLAUDE.md`.

## Where things stand

Milestones **M1–M5** are implemented and tested (72 tests; ruff, pytest, ty,
and pyrefly all green): voxel input (`.vox`/`.npy`) → LDraw-quantized colour →
hollow → placement (greedy or Luo split/remerge) → full RBE stability analysis
→ refinement → `.ldr`/`.mpd` export with bottom-up `0 STEP` instructions.

Deliberately **not** done yet, and covered below:

- **M6** — mesh front-end (`.obj`/`.stl` voxelization)
- **SNOT** — sideways building (the data model is ready; nothing is built)
- **Rendered instruction booklets** — output is STEP metas only, no images/PDF
- Slope/tile surface finishing is minimal and **opt-in** (`--slopes`,
  `--tiles`) because the slope pass adds material outside the voxel shape
- Assorted physics-fidelity, placement-quality, performance, and tooling items

> **Repo state note:** nothing from the initial implementation has been
> committed — the entire pipeline lives in the working tree pending review.
> The first commit should probably land `src/`, `tests/`, `data/`,
> `pyproject.toml`/`uv.lock`, and the docs as a unit, since the tests and
> catalog data are load-bearing for everything else.

---

## M6 — Mesh front-end (`mesh.py`)

**Goal:** `legolization model.obj --target-studs 40 -o model.ldr` works
end-to-end, feeding the existing pipeline a `VoxelGrid`.

**Design sketch**

- `trimesh.load(path)` → optionally repair/fill → voxelize. For solid models
  use `mesh.voxelized(pitch).fill()`; for shells skip `fill()` and let
  auto-hollow's keep-fill logic handle support.
- **Scale control** (per the decisions log): either an explicit `--pitch` in
  model units, or `--target-studs N` with
  `pitch = mesh.extents.max() / N`.
- **Aspect correction:** LEGO cells are 20 LDU wide but only 8 LDU (one plate)
  tall. Rather than mapping cubic voxels to 3-plate bricks (a 24/20 = 1.2×
  vertical stretch), scale the mesh's z-axis by 20/8 = 2.5 before voxelizing
  and load the result with `plates_per_voxel=1`. That gives true plate-level
  vertical resolution and correct proportions for free.
- **Colour sampling:** for each filled voxel, query the nearest surface point
  (`trimesh.proximity.ProximityQuery`) and sample vertex colour / texture UV
  → RGB → the existing `Palette.quantize`. Interior voxels can inherit the
  nearest surface colour (they get hollowed anyway).
- Wire into `pipeline.run_file` as a third suffix branch and add
  `--pitch`/`--target-studs` to the CLI.

**Acceptance:** a textured `.obj` (e.g. a Stanford bunny) converts to a
stable, buildable `.ldr` at 20–60 studs; a pytest fixture with a tiny
generated mesh (trimesh primitives) covers pitch/target-stud math and colour
sampling. `trimesh` is already in the dependencies.

**Risks:** non-watertight meshes (voxelization artifacts → floating islands;
lean on `improve_connectivity`, but consider a largest-component filter on the
voxel grid); texture sampling needs meshes with materials — fall back to a
uniform colour with a warning.

---

## Surface finishing v2 — slopes and tiles that earn default-on

Today `--slopes` only replaces free-standing `1x1` **brick columns** at
descending steps with `slope_45_2x1`, and it **adds volume** where the voxel
shape is empty (that is why it is opt-in). `--tiles` only swaps whole exposed
plates whose footprint has an exact tile equivalent (1x1, 1x2, 1x4, 2x2).

**Slope work items**

1. **Shape-preserving mode (can become default).** Match the slope's own
   `filled_cells` against the target shape instead of requiring an empty
   neighbour: a `slope_45_2x1` exactly covers "full column + 1-plate toe", so
   any staircase voxelized at plate resolution contains real occurrences.
   Zero added material → safe to enable by default; the current
   volume-adding behaviour stays behind `--slopes=smooth`.
2. **Fit the full slope family.** `slope_45_2x2` (3039), `slope_33_3x1`
   (4286) are already in the catalog but never placed; add width-2 and 33°
   step patterns, and extend the catalog with the inverted slopes
   (3665, 3660) for overhangs.
3. **Normal-driven fitting (with M6).** When the input is a mesh, choose the
   slope angle from the local surface normal (the approach in the
   "vivid architectural sculptures" paper) and only accept a placement if it
   reduces surface error against the original mesh, not just the voxelization.
4. **Cooperate with placement, not after it.** The pass currently only sees
   `1x1` bricks that survived merging. Either run slope fitting *before*
   merging (reserve step cells), or teach the merge engine that slope-eligible
   cells are worth leaving unmerged.

**Tile work items**

- Split large exposed plates into tileable sub-rectangles instead of skipping
  them; add `tile_2x4` (87079) and `tile_1x3` (63864) to the catalog.
- Brick-topped surfaces cannot be tiled without changing height — consider an
  optional "plate-cap" mode that re-plans the top 3 plates of flat regions as
  plate+plate+tile.
- Re-verify stability after finishing (already wired in `pipeline.run`).

**Acceptance:** shape-preserving slopes are on by default with no
`_assert_exact_cover` regressions; a staircase fixture produces slopes with
zero colour/volume error; tile coverage on a flat-roofed test model exceeds
90 % of exposed top plates.

---

## Build instructions v2 — rendered booklets

Today the output is one model file with a `0 STEP` after each layer, which
Studio/LDView step through correctly. Missing: images, page layout, parts
lists, and smarter step granularity.

**Work items, in dependency order**

1. **Step semantics.** Split big layers into digestible steps (~5–10 bricks),
   keep symmetric halves together, and emit `0 ROTSTEP` metas when the build
   direction should rotate for visibility.
2. **Per-step stability check.** Every step prefix is itself a structure —
   run the existing RBE on each prefix and reorder bricks within a layer so
   the model is never unstable mid-build. (The assembly-sequence paper in
   `references/` goes further; a greedy prefix-stable ordering is enough
   here.) This is cheap now that the LP solves in milliseconds.
3. **Bill of materials.** pyldraw3 ships `ldraw.bom`; emit a total BOM and
   per-step part callouts (part id, colour, count) as text/JSON first.
4. **Rendering.** Options, in order of preference:
   - **LDView command line** (`-SaveSnapshot`): free and already installed,
     but it must be configured with an LDraw directory first — on this
     machine an unconfigured LDView exits 0 *without writing the file*, so
     the harness must verify the PNG exists. Point it at pyldraw3's cache
     (`~/Library/Caches/pyldraw3/<version>/ldraw`).
   - L3P + POV-Ray for print quality (heavier toolchain).
   - Studio's instruction maker as the manual fallback.
5. **Booklet assembly.** Compose step PNGs + BOM callouts into a PDF
   (`weasyprint` or `reportlab` — new dependency, decide at implementation
   time). One page per N steps, cover page with model stats from
   `PipelineResult`.

**Acceptance:** `legolization model.vox --instructions model.pdf` produces a
paginated booklet where each step image matches the STEP structure of the
`.ldr`, and a regression test asserts page/step counts (rendering itself can
be skipped in CI when LDView is absent).

---

## SNOT — sideways building

The Part abstraction was designed for this: every connector already carries a
**direction vector**, and the graph/physics read the direction rather than
assuming "up". None of the consumers exercise it yet. This is the largest
item on the roadmap; suggested staging:

1. **Orientation model.** Placements today are yaw-only. Introduce a proper
   orientation type (the 24 axis-aligned rotations, or at minimum the 6 "stud
   direction" cases × 4 yaws), rotate `occupied_cells`/connectors through it,
   and extend the LDU matrix construction in `ldraw_out.py`. Grid cells stay
   axis-aligned; the interesting problem is that a sideways brick occupies
   20-LDU-tall space that is not a whole number of plates (2.5) — sideways
   sections must be planned in 40-LDU (5-plate) vertical quanta, which is a
   real constraint the placement layer has to own.
2. **Catalog additions.** Side-stud parts are the on-ramp before fully
   sideways bricks: 87087 (1x1 with 1 side stud), 4070 (headlight), 99781
   (bracket). These keep the grid stud-up while exposing lateral connectors.
3. **Graph + physics.** Mating logic in `graph.py` currently matches top
   connectors against `cell + (0,0,1)`; generalize to `cell + direction`.
   In the RBE, a sideways knob contact rotates the contact-point pattern and
   the friction axis into the contact plane — `add_force` already takes
   arbitrary directions/positions, so this is mostly bookkeeping in
   `build_model`.
4. **Placement.** Start with a post-pass (like slopes): replace surface
   detail cells with side-stud assemblies. Full sideways *regions* (e.g.
   curved surfaces built studs-out) are research-grade; keep them out of
   scope until 1–3 are solid.

**Acceptance for stage 1–3:** a hand-authored layout with one bracket-mounted
sideways tile round-trips through graph, physics, and LDraw output; analytic
tests confirm a side-stud connection transmits the expected friction load.

---

## Physics fidelity backlog

- **Q×X knob rule.** The StableLego *paper* gives edge knobs 3 contact points
  and interior knobs 4 on Q≥3-wide bricks; their released code (and ours)
  uses 3 points for everything ≥2 wide. Implement the paper rule behind a
  config flag and A/B the verdicts.
- **Third torque axis.** Following StableLego we model τx/τy only. Adding the
  yaw torque row (τz) makes horizontal knob-press forces meaningful for
  twist loads; cheap to add in `stability/model.py` (levers already exist).
- **Contact triangle orientation.** The 3-point pattern is axis-aligned
  regardless of brick yaw (as in StableLego's code); rotate it with the
  brick's yaw for correctness on rotated wide bricks.
- **Ground model options.** Layer-0 contacts currently behave like studs on a
  baseplate (the ground can pull down, StableLego-style). Add
  `SolverConfig.ground_pull: bool` for loose-on-a-table analysis, and an
  option to emit an actual baseplate part (3811) under the model.
- **External loads.** Dead load only today. Add an API for point loads
  (StableLego's `external_weight` test cases model a 200 g block as a heavy
  brick — we can do the same or add per-brick `extra_mass_g`).
- **Cross-validation.** Run the solver against `StableLego-main/test_lego/*.json`
  and compare per-structure verdicts with their published results; encode a
  handful as pytest golden cases. This was the optional M2 deliverable and is
  the best available ground truth.
- **Targeted MILP.** `--milp` currently solves the whole model. Reserve the
  complementarity MILP for the k-ring around the weakest contacts and stitch
  it to the LP solution elsewhere — exactness where it matters, LP speed
  everywhere else.

## Placement quality backlog

- **Additive greedy scoring.** Candidate ranking is lexicographic (coverage,
  then bonding), which is why a 2x6 always beats a seam-breaking 2x4. Move to
  `cells + w·bond` with the Kollsker distance term evaluated at d ∈ {0,1,2}
  rather than only d=0, and tune on a corpus of shapes.
- **Cheaper repairs.** Connectivity repair splits to 1x1 plates and only
  `compact_vertical` re-forms bricks (exact-footprint trios). Add a local
  re-merge pass over repaired regions and track brick count as a regression
  metric — the hollow sphere currently lands at ~374 bricks where ~200 should
  be possible.
- **Cost term options.** Brick count is the proxy today; add mass and real
  price (BrickLink price guide) as alternatives, and an
  **inventory-limited catalog** (per-part availability counts) for building
  from an actual collection.
- **More strategies.** The `PlacementStrategy` protocol makes these drop-in:
  Kollsker's exact MILP for small models, and the SM-GA genetic algorithm
  from the references for quality-over-time searches.
- **Aesthetics.** Add Min-style symmetry/balance terms to the objective, and
  optional colour dithering for gradient regions instead of hard
  nearest-colour banding.

## Performance backlog

- **Vectorize `build_model`.** Contact assembly is Python loops over knobs ×
  points; batching into numpy per contact-type would cut model build time,
  which now rivals the LP solve on large layouts.
- **Incremental re-analysis.** Refinement changes a k-ring but re-solves the
  whole structure. Either warm-start HiGHS with the previous basis or analyze
  the modified subgraph with boundary forces frozen (approximate but fine for
  accept/reject decisions, with a full solve on acceptance).
- **Candidate caching in greedy `_fill`.** `_placements` recomputes rotations
  and validity per seed; memoize per (part, yaw) footprints and test cells
  against numpy masks instead of Python sets.
- **Parallel restarts.** Both strategies are seed-sensitive; run a few seeds
  in a process pool and keep the best objective (all state is already
  copyable via `Layout.copy`).

## I/O and tooling backlog

- **LDView snapshot regression.** The plan called for automated headless PNG
  snapshots per milestone. LDView.app exists on this machine but has never
  been configured, and its CLI silently writes nothing in that state — the
  harness must configure `-LDrawDir` explicitly and assert the file appears.
  Once working, add golden-image (or at least golden-geometry) checks for the
  three `data/examples/` models.
- **Stability heatmap export.** Per-brick scores already exist; add
  `--heatmap out.ldr` that recolours bricks by score (black → red → white,
  matching StableLego's visualization) for debugging weak structures.
- **`.vox` robustness.** Embed the documented default MagicaVoxel palette so
  paletteless files load; support multi-model scenes (currently first model
  only).
- **Richer CLI output.** `--report report.json` dumping `PipelineResult`
  (counts, mass, scores, BOM) for scripting.
- **TUI.** `textual` is already a dependency; a small terminal UI showing
  layer-by-layer placement, the stability heatmap, and refinement progress
  would make tuning weights far less blind. (The decisions log rules out a
  full GUI for v1; a TUI is the pragmatic middle ground.)

---

## Suggested order

1. **Commit the current tree**, then M6 mesh front-end (unlocks real inputs;
   small, self-contained).
2. Shape-preserving slopes + tile splitting (small; makes finishing
   default-on).
3. StableLego cross-validation + heatmap export (medium; hardens the
   physics core everything else trusts).
4. Instructions v2 through BOM + per-step stability (medium; no new deps
   until the PDF stage).
5. Performance items as models grow (incremental re-analysis first).
6. SNOT stages 1–3 (large; start after the orientation model design is
   reviewed).
