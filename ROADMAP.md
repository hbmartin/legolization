# Roadmap

Future work for legolization, picking up where the initial implementation
stopped. For the algorithms and formulas each item builds on, see the papers in
`references/` and the design notes in `CLAUDE.md`.

## v3 progress notes

Living log of the six-item v3 program (sequencer LP performance, MPD
subassemblies, shape-preserving slopes, SNOT finishing pass, per-layer
Kollsker MILP, LDraw model import). Every landed item appends a dated
entry here with its measured proof numbers; entries are append-only.

*(No items landed yet. Program plan approved 2026-07-18; evidence base:
the profiling campaign on PR #16 — 99% of large-model runtime is cold
HiGHS LP solves in sequencing, ~2 per step, superlinear per-solve cost —
and `docs/unstable-prefix-report.md` — the floating-until-later-band
step class is unorderable without subassemblies.)*

## Where things stand

Milestones **M1–M5** are implemented and tested (ruff, pytest, ty, and
pyrefly all green): voxel input (`.vox`/`.npy`) → LDraw-quantized colour
(optionally dithered) → hollow (anisotropic ~1-brick shell, colour-free
interiors) → placement (six registered strategies: greedy, luo, bond, fast,
smga, beauty) → full RBE stability analysis (cross-validated against all nine
StableLego release fixtures) → ALNS destroy-and-repair → stability-aware fill
restore → global re-merge → smart step sequencing (prefix-stable, mirror-
aware, ROTSTEP hints) → `.ldr`/`.mpd` export with a JSON/text bill of
materials.

The 2026-07 audit remediation closed every finding F1–F16: torque-capable
side presses (two per pair at the shared face's vertical extremes), real
plate/tile masses, brick-graph component semantics, a per-interface seam
metric, Kollsker h(r) + distance-decayed bond scoring, the IGNORE interior
colour label, phase-aware brick re-forming (`compact_columns` +
`final_remerge` — the hollow sphere dropped from ~374 to well under 200
parts), Luo's maximin C_M acceptance with importance-sampled seeds and
failMAX = 100, soft colour constraints, `.vox`/`.npy` robustness, and
`--aspect-correct` resampling.

Deliberately **not** done yet, and covered below:

- **SNOT** — sideways building (the data model is ready; nothing is built)
- Slope/tile surface finishing is minimal and **opt-in** (`--slopes`,
  `--tiles`) because the slope pass adds material outside the voxel shape
- Assorted physics-fidelity, placement-quality, performance, and tooling items

**Documented deviations from the papers** (deliberate, benchmark-arbitrated):
Min's A* is a capped-OPEN beam search with canonical-cell expansion; Min's
multi-height g_v term is reinterpreted at plate resolution (no 2-brick-tall
parts in the catalog); Kollsker's 1D remainder/stagger terms are measured
along a per-layer scan axis; the ALNS repair MILP runs on scipy/HiGHS with a
merge-engine filler for large regions instead of CPLEX.

---

## M6 — Mesh front-end (`mesh.py`) — DONE (2026-07-18)

Shipped as designed below, with two deviations: the pitch derives from the
**largest horizontal extent** (`max(extents[:2]) / target_studs`), not the
overall max — "target studs" means footprint, and the overall max would
shrink tall models; and colour sampling landed as **nearest-vertex** via
scipy's cKDTree (`--mesh-colour-mode sampled`; texture and vertex colours
both route through trimesh's `to_color`), not `ProximityQuery.on_surface`
— rtree is not a dependency, and vertex density exceeds voxel resolution
for every corpus mesh. Meshes without colour data fall back to the
uniform `--mesh-colour` with a note. A
largest-component filter (6-connected, with a "dropped N voxels" progress
warning) handles non-watertight meshes; `--up {x,y,z}` orients Y-up `.obj`
files via a proper rotation. See `src/legolization/mesh.py` and
`tests/test_mesh.py`.

**Original design sketch**

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

**Done** (all five items). Items 1–3 (the `instructions/` package): smart
step chunking with mirror-pair co-stepping and ROTSTEP view hints, greedy
prefix-stable sequencing over the vertical block graph (warn or strict
policy), and a hand-rolled JSON/text BOM with per-step callouts (`--bom`).
Items 4–5 shipped as `instructions/render.py` + `instructions/booklet.py`:

**4. Rendering** went LeoCAD-first rather than the LDView-first sketch:
   LeoCAD's CLI exports a whole run of steps per invocation (`-f/-t`,
   numbered by absolute step) and highlights each step's new bricks
   (`--highlight`); LDView (`-SaveSnapshot -Step=N -AutoCrop=0`) is the
   fallback, one process per step. Detection: `$LEGOLIZATION_RENDERER`
   (`none` disables) → PATH → `/Applications` app bundles; parts library
   via `$LDRAWDIR` → pyldraw3's cache → common install dirs. Success is a
   non-empty PNG on disk, never the exit code. The camera is driven from
   the plan's own RotStep data against a ROTSTEP-stripped temp copy, so
   framing stays constant and both backends agree.
**5. Booklet assembly** is HTML-first with a reportlab-canvas PDF writer:
   one `Booklet` pagination model feeds both, so page counts match by
   construction (cover + overflow parts pages + N steps/page, default 2).
   No renderer → placeholder boxes, identical page count, warning banner.

Acceptance holds: `legolization model.vox --instructions model.pdf` (or
`.html`) writes the booklet; regression tests assert booklet step sections
== `.ldr` `0 STEP` count == `PipelineResult.step_count` and pypdf page
counts, with rendering disabled via `LEGOLIZATION_RENDERER=none` in CI and
a `slow`-marked real-render test locally.

### Sequencing upgrades (landed with v2)

Paper-driven upgrades to the sequencer (`instructions/search.py`,
`metrics.py`): a spatial-continuity tiebreak orders ready candidates by
distance to the previous step before LP evaluation (zero extra LPs);
both degradation paths (deadlock, no-stable-prefix) now re-plan the
remainder by **assembly-by-disassembly** along a maximal-stability path
(`fallback="band"` is the legacy escape hatch); an opt-in **beam search**
(`search="beam"`, `beam_states`, `lp_budget`) explores whole build orders
ranked by (unstable prefixes, summed scores); `sequence_similarity`
(Kendall's τ + RLSD) and `plan_quality` quantify orders. Deferred: MPD
subassembly submodels (would change the `InstructionPlan` data model the
booklet consumes; `BuildStep` can gain `submodel` additively later) and
±X/±Y block edges (SNOT-only; the blocker-map `Mapping[int,
frozenset[int]]` seam in `blocking.py` is the plug-in point).

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
- ~~**Cross-validation.**~~ **Done:** all nine StableLego release fixtures are
  vendored under `tests/data/stablelego/` and every verdict reproduces
  (`tests/test_stablelego_cross.py`), alongside closed-form golden pins for
  the single-stud cantilever and the maximin capacity.
- **Targeted MILP.** `--milp` currently solves the whole model. Reserve the
  complementarity MILP for the k-ring around the weakest contacts and stitch
  it to the LP solution elsewhere — exactness where it matters, LP speed
  everywhere else.

## Placement quality backlog

- ~~**Additive greedy scoring.**~~ **Done differently:** greedy now ranks by
  (h(r) parts estimate, distance-decayed bond, coverage) — the Kollsker
  d-term is live over a 3-stud window and a 7-wide wall staggers with no
  repair pass. Weight tuning on a larger corpus remains open.
- ~~**Cheaper repairs.**~~ **Done:** `compact_columns` re-forms bricks on a
  region-voted phase inside every repair, `final_remerge` re-phases plate
  rafts globally, and `tests/test_examples_regression.py` pins brick counts.
- **Cost term options.** Brick count is the proxy today; add mass and real
  price (BrickLink price guide) as alternatives, and an
  **inventory-limited catalog** (per-part availability counts) for building
  from an actual collection.
- ~~**More strategies.**~~ **Done and exceeded:** four new registered
  strategies (bond, fast, smga, beauty) over a shared per-layer engine, plus
  the strategy-agnostic ALNS destroy-and-repair. Kollsker's *exact global*
  MILP for small models remains open (only the repair-region MILP exists).
- ~~**Aesthetics.**~~ **Done:** Min-style symmetry/balance and SM-GA/Bao
  perpendicularity are objective terms (`placement/aesthetics.py`), the
  beauty strategy optimizes them directly, and `--dither` provides
  Floyd-Steinberg gradients. Validating the beauty scalar against human
  judgement (the permutation-drift methodology) remains open.
- **Seed variance.** Layout quality varies noticeably across seeds on shell
  shapes (the r=4 hollow sphere spans ~135–205 parts over seeds); parallel
  restarts (below) would harvest the good tail.

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
  copyable via `Layout.copy`). *Partially delivered:* `--strategy all`
  (`compare.run_all`) fans out over a spawn process pool and keeps the best
  candidate — across strategies at one seed; the multi-seed sweep is now a
  small extension of the same runner.

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
  only). Malformed chunks and out-of-bounds voxels already fail cleanly.
- **Richer CLI output.** `--report report.json` dumping `PipelineResult`
  (counts, mass, scores) for scripting — `--bom` already covers the parts
  list; `scripts/benchmark.py` covers cross-strategy comparison. *Done for
  sweeps:* `--strategy all --report report.json` writes per-strategy metrics
  plus the winner; extending it to single-strategy runs remains.
- **TUI.** A small terminal UI showing layer-by-layer placement, the
  stability heatmap, and refinement progress would make tuning weights far
  less blind (re-add `textual` when this starts — it was dropped as an
  unused dependency). The decisions log rules out a full GUI for v1.

---

## Suggested order

1. M6 mesh front-end (unlocks real inputs; small, self-contained — the
   `--aspect-correct` resampling and `--target-studs` design are ready).
2. Shape-preserving slopes + tile splitting (small; makes finishing
   default-on).
3. ~~Instruction rendering~~ — **done** (`--instructions out.html|out.pdf`:
   LeoCAD/LDView step images + HTML/reportlab booklet, plus the
   disassembly-rescue/beam sequencing upgrades).
4. Performance items as models grow (incremental re-analysis first — the
   per-step prefix LPs and ALNS rounds would benefit most).
5. SNOT stages 1–3 (large; start after the orientation model design is
   reviewed).
