# Roadmap

Future work for legolization, picking up where the initial implementation
stopped. For the algorithms and formulas each item builds on, see the papers in
`references/` and the design notes in `CLAUDE.md`.

## v4 progress notes

Living log of the v4 program (PR #17 review remediation, residual
rescue-LP performance + `docs/performance-testing.md`, kollsker
downstream-drift investigation/report/fixes, SNOT v2). Every landed
workstream appends a dated entry with measured proof; append-only.

### 2026-07-19 — PR #17 review remediation (12 commits)

All findings from `docs/pr-17-review.md` addressed: the CI lizard
CCN-18 gate is green again (`verify_plan` 33 → dispatcher +
`_PlanVerifier`; `_validate_args` 27 → three validators; `_mount` 22 →
plan/mutate split); the three P1s (positional dataclass compatibility
restored with a field-order pin test; the 3070b reverse-map collision
that broke flat-tile import fixed with candidate-list decoding; one
monotonic deadline over the sequential strategy sweep); the P2s (snot
clads only faces whose straight slide-in ray reaches outside the model
— enclosed cavities and pits rejected; sideways cladding excluded from
generic side contacts — no phantom presses; blocker rays derive extent
from layout bounds, not a 64-cell cap; strictness judged after the
subassembly rewrite; every subassembly must attach exactly once and
the final world must equal the layout; MPD stems case-insensitive;
kollsker MILP calls guarded, tuning validated finite, deadlines honest
across both stages; `--profile` rejected where it was silently
ignored; attach steps are checker metadata, not warnings) and both P3s
(floating shortcut defers to cross-check mode; finishing spans are
`phase.finish_surfaces` + nested children). 486 tests at that point,
all gates green.

### 2026-07-19 — Rescue-LP performance + docs/performance-testing.md

Shipped `docs/performance-testing.md` (tools, schemas, the same-session
before/after protocol, regression definitions, drift policy, measured
dead ends), sha-stamped schema-2 CLI profiles sharing
`telemetry.git_sha()` with the profiling script, two regression pins
for the user's underneath-decoration constraint (warm grown-base probe
≡ cold at 1e-9 incl. rollback; the rescue orders a hanging decoration
before its overhang identically on both engines), and the committed
optimization: **direct-highspy cold rescue solves**, size-gated at
`SolverConfig.rescue_direct_min_bricks = 200` so small components keep
the scipy-exact path the 1e-6/dual-engine tests pin. Shared
`_lp_arrays()` guarantees both engines solve the byte-identical
polytope; near-boundary or failed solves fall back under
`stability.rescue.cold_fallback`.

Proof (same-session sha-stamped profiles, seed 0):
| model | before | after | migration | notes |
|---|---|---|---|---|
| spot@24 | 511.4 s | 490.3 s | 74 of 80 solves → `cold_direct`, 0 fallbacks | result block identical |
| suzanne@16 | 37.7 s | 31.7 s | 14 solves → direct | result block identical |
| pyramid | 1.03 s | 1.02 s | untouched (no rescue) | — |

Deviations from plan: the C′ experiment — warm rescue by **bound
deactivation** on one persistent model — was built dark, measured, and
**killed by its own criteria**: correctness perfect (walk drift
~1e-18) but warm re-solves ran ~4× SLOWER than presolved cold solves
(23 s vs 5.6 s at n≈1000; spot 588 s vs 490 s) because the persistent
model must keep presolve off for basis reuse while one-shot solves
presolve the RBE down dramatically. Mechanism reverted; dead end
recorded in `docs/performance-testing.md` §6 with the same candor as
the LP-deletion note. The SNOT decline gate on the warm solvers stays
(documented revisit trigger: when SNOT contact semantics stabilize).

*(Future note — mesh-kind eval baseline: the committed scorecard
baseline covers synthetics only. Now that v3's sequencing speedups make
mesh sweeps feasible (suzanne@16 ≈ 30 s), commit a mesh-kind baseline
via `uv run python scripts/eval_corpus.py --kind mesh --write-baseline`
after a clean run, per the self-evaluation playbook's "widen baseline
scope only on a clean run" rule. Not done in v4 — the drift fixes land
first so the baseline is cut once, not twice.)*

## v3 progress notes

Living log of the six-item v3 program (sequencer LP performance, MPD
subassemblies, shape-preserving slopes, SNOT finishing pass, per-layer
Kollsker MILP, LDraw model import). Every landed item appends a dated
entry here with its measured proof numbers; entries are append-only.

*(Program plan approved 2026-07-18; evidence base: the profiling
campaign on PR #16 — 99% of large-model runtime is cold HiGHS LP solves
in sequencing, ~2 per step, superlinear per-solve cost — and
`docs/unstable-prefix-report.md` — the floating-until-later-band step
class is unorderable without subassemblies.)*

### 2026-07-19 — Item 1: sequencer LP performance

Shipped `stability/prefix.py` behind `SolverConfig.engine` (default
`"highspy"`; `"scipy"` preserves the legacy path bit-for-bit): a
warm-started incremental `PrefixSolver` for the greedy loop (probe =
append rows/cols + re-solve from the retained basis, ~20-90 simplex
iterations per step instead of a full cold solve; commit of the probed
chunk is free), an LP-free **floating shortcut** (a prefix with a
stud-unreachable brick is unstable by definition — verdict + 1.0 score
without any solve), and a component-decomposed `RemovalSolver` for the
disassembly rescue (per-contact-component verdict cache; the RBE is
block-diagonal across uncoupled components).

Proof (same sha-pinned inputs as the PR #16 campaign, seed 0):
| model | before | after | speedup | notes |
|---|---|---|---|---|
| pyramid.npy | 1.26 s | 0.99 s | 1.3x | clean greedy path |
| suzanne @16 | 81.3 s | 30.3 s | 2.7x | 72 of 104 solves shortcut |
| spot @24 | 1465.5 s | 468.6 s | **3.1x** | 215 of 287 rescue probes LP-free |

Byte-exact goldens unchanged with the new default (dual-engine plan/byte
equality pinned by tests on every shipped example); synthetic corpus
scorecard row-for-row identical to the committed baseline; 408 tests.

Deviations from plan: LP-deletion warm starts for the rescue do NOT pay
(HiGHS discards too much basis on deletion — measured, not assumed);
the rescue's win came from the floating shortcut + component
decomposition instead. Byte-identity across engines is guaranteed for
plans that never enter the rescue (includes all goldens); rescued plans
are verdict-equivalent (equal unstable counts, `verify_plan` clean) with
solver-tolerance-level score drift on degenerate optima — the same drift
class scipy exhibits across its own versions. Remaining headroom: 80
cold LPs on grounded-stable rescue states at n≈1000.

### 2026-07-19 — Item 2: MPD subassembly steps

Shipped `instructions/subassembly.py` behind `--subassemblies`
(`InstructionsConfig.subassemblies=False` default — flag-off is
byte-identical to before, pinned by the goldens): a post-pass on the
finished plan that walks per-prefix `ConnectionGraph.floating_ids()`
(no extra LPs), finds persistent floating runs, closes them into
stud-connected clusters of the run's placement window, validates each
cluster (grounding on attach: `floating_ids(P∪S) ∩ S = ∅`; unit
vertical insertability: no placed brick overhangs any cluster brick),
and rewrites the plan: cluster bricks become a separately built unit —
sequenced by a recursive `plan_instructions` on the grounded, table-
level translated sub-layout — plus one attach step that places the unit
as a whole. Emission writes real multi-`FILE` `.mpd` submodels (attach
= one colour-16 reference line at `-8·anchor_layer`; `.ldr` falls back
to world-frame flattening), the booklet gets per-unit sections, attach
callouts, and "support while attaching" labels, `verify_plan` audits
sub steps in the sub's own grounded frame, and step images render per
submodel section (no renderer CLI flags needed).

Proof (`scripts/check_instructions.py --subassemblies`, seed 0,
before-JSONs from the pre-item baseline):
| model | unstable steps | subs | attach steps | violations |
|---|---|---|---|---|
| mushroom | **17 → 0** | 5 | 5 | 0 |
| heart.vox | 2 → 1 | 1 | 1 | 0 |
| letter-t | 1 → 0 | 1 | 1 | 0 |
| cantilever | 1 → 0 | 1 | 1 | 0 |
| wide-arch | 2 → 0 | 1 | 1 | 0 |
| two-towers-bridge | 0 → 0 | 0 | 0 | 0 |

Mushroom (the worst instruction-quality case in the corpus, 17 unstable
steps with up to 26 bricks floating mid-build) now builds with zero
unstable steps: the cap is assembled as five table-built units. Heart's
one residual is *inside* a sub build (the lobe overhang floats even on
the table until the next sub step ties it) and carries the honest
"support the overhang by hand" warning. Two-towers-bridge extracts
nothing and its flag-on JSON is byte-identical to flag-off on the same
code. Booklet renders verified visually: sub steps build flat on the
table with highlights, the attach step seats the whole highlighted unit
on the stem. 422 tests; goldens untouched.

Deviations from plan: `max_subassemblies` defaults to 6, not the
planned 4 — measured on mushroom, cap 4 leaves one floating window
uncaptured (1 unstable step); cap 6 captures all five. The planned
"attach steps stay warned" caveat mostly did not materialize: attach
prefixes re-analyze as stable everywhere in the proof set (the RBE sees
the same final geometry; it was the *intermediate* orders that were
unstable). Old roadmap "Deferred: MPD subassembly submodels" marked
DONE.

### 2026-07-19 — Item 3: shape-preserving slopes

Shipped preserve-mode slope fitting: `--slopes` (= `--slopes preserve`)
matches each catalogued slope's own `filled_cells` profile — stud
columns at full height plus 1-plate toes — against cells inside the
target shape (sloped-void cells must be *outside* it), carves out the
same-colour donor bricks covering the profile, places the slope, and
MILP-refills any donor cells beyond the profile (exact cover, colours
inherited per cell from the carved donors; the tiling is computed
before any mutation so a failed candidate costs nothing). Zero
material added or removed — the filled-cell set is asserted identical
on every proof run. All three catalogued slopes now place (45° 2x1
`3040b`, 45° 2x2 `3039`, 33° 3x1 `4286` — the latter two had never
been placed by any pass), swept largest profile first. The legacy
add-outside pass is `--slopes smooth`; `PipelineConfig.slopes=True`
still means smooth for API back-compat.

Two safety rails, both measured in: the carve is capped at 4 freed
cells per swap (on suzanne@16, caps 0/2/4/6/8 place 2/10/29/44/51
slopes and the structure collapses at 8 — fragmenting load-bearing
bricks into weak stacks), and the pipeline snapshots the layout before
the pass and reverts wholesale if the RBE verdict flips stable →
unstable, so preserve mode can never trade stability for looks.

Proof (seed 0; "fill-identical" = union of filled cells unchanged):
| model | bricks | slopes placed | fill-identical | stable |
|---|---|---|---|---|
| suzanne@16 | 331 → 330 | 29 | yes | yes → yes (worst 0.137) |
| teapot@16 | 270 → 270 | 10 | yes | yes → yes |
| homer@16 | 190 → 190 | 0 (guard reverted) | yes | yes → yes |
| heart.vox | 12 → 12 | 0 (no sites) | yes | yes → yes |
| pyramid.npy | 124 → 124 | 0 (no sites) | yes | yes → yes |

On homer even the cap-4 carve trips the RBE verdict, the snapshot
guard reverts the whole pass, and the model ships exactly as the
baseline — the guard is load-bearing, not theoretical.

Voxel models quantized at 3 plates/voxel (heart, pyramid) have no
1-plate treads, so preserve mode correctly never fires there — the
profile only exists on plate-resolution surfaces, i.e. mesh imports.
Renders confirm visibly smoother ramps on suzanne's brow, nose, and
chin. Off by default: goldens and corpus baseline untouched (428
tests).

Deviations from plan: the plan's expectation that pyramid-style
brick-step models would gain slopes was wrong — their sloped-void
cells are *inside* the shape, which shape preservation must reject;
the win lives on mesh surfaces instead. The carve-and-refill
extension (planned as a follow-up) shipped in v1 because measurement
demanded it: exact-donor matching alone fired on only 2 of 82
shape-valid sites on suzanne (0 of 68 on teapot) — merged bricks
almost always span the profile boundary. Unguarded carving at a large
cap breaks stability (suzanne/teapot/homer all flip unstable at cap
24); the cap-4 + snapshot-revert combination is what ships. Old
roadmap "Slopes" section marked DONE.

### 2026-07-19 — Item 6: LDraw model import (strict)

Shipped `ldraw_in.layout_from_ldraw`: an existing `.ldr`/`.mpd` model
becomes a `Layout` — the exact inverse of `ldraw_out.piece_for`'s
transform (yaw decoded from the four canonical rotation matrices,
`layer = -Y/8 - height`, x/y from position minus the rotated-footprint
centroid and un-rotated slope `origin_offset`). MPD submodels flatten
through `iter_occurrences`' composed world transforms (`iter_pieces`
yields local frames — measured, not assumed). Strict by decision: any
part outside the catalog, non-yaw rotation, off-grid position,
out-of-palette colour, collision, or below-ground brick errors; ALL
problems are aggregated into one `LdrawImportError` so a user sees the
model's full distance to importable. CLI: `.ldr`/`.mpd` input skips
placement entirely — import, analyze, sequence, emit (`legolization
model.ldr -o out.ldr --instructions b.pdf`); placement/voxel/mesh
flags and a defaulted output (which would overwrite the input) are
argparse errors. `PipelineResult.grid` became optional (imported
models have no voxel grid); `write_outputs` is reused unchanged.

Proof (seed 0):
| example | round-trip | steps (native → imported) | unstable | violations |
|---|---|---|---|---|
| heart.ldr | exact | 7 → 7 | 2 → 2 | 0 |
| pyramid.ldr | exact | 21 → 21 | 2 → 2 | 0 |
| arch.ldr | exact | 8 → 8 | 1 → 1 | 0 |

"Round-trip exact" = import → write → re-import brick-multiset
equality with nothing dropped (byte-identity is not the bar — shipped
examples are emitted in plan-step order with ROTSTEPs, a bare
re-emission rasters by layer). Slope parts round-trip at all four yaws
(origin-offset inversion); a subassembly `.mpd` flattens back to the
exact source layout. Live CLI run: heart.ldr → booklet + BOM, exit 0.
A hand-built pathological file reports all five problem kinds in one
error. 436 tests; goldens untouched.

Deviations from plan: none. The planned "identical plan_quality to the
natively-generated plan" held exactly (table above) — brick ids differ
between import order and placement order, yet the sequencer's
tiebreaks land on the same plan shape.

### 2026-07-19 — Item 5: per-layer Kollsker MILP (`--strategy kollsker`)

Shipped `placement/layered/kollsker.py`: the paper's exact
set-partitioning model (eqs. 1-3) solved per 4-connected component of
each layer problem — the tractable scope; the whole-model MILP is
exponential and the paper's own matheuristic re-optimizes regions the
same way (eqs. 26-39). Two-stage lexicographic solve: stage 1
minimizes part count (N*), stage 2 pins Σx = N* and maximizes a
perimeter-normalized stagger reward (+`seam_priority` per straddled
below-seam, −0.5·priority per border-aligned one) with a 1e-6 rank
tiebreak for determinism. `h3` lookahead deliberately dropped — it
guides sequential commitment, which the simultaneous cover subsumes.
Fallback to the constructive bond pass per component on candidate
blowup (>20k), solver failure, or timeout (`layer_time_s`, default
10 s, deadline-aware). Registered as `kollsker` → CLI, `--strategy
all` sweeps, and eval tooling pick it up automatically;
`PipelineConfig.milp_layer_time_s` / `milp_bond_weight` tune it.

Proof (seed 0, end-to-end pipeline, layer-steps mode):
| model | bond | kollsker | | model | bond | kollsker |
|---|---|---|---|---|---|---|
| cantilever | 62 | **36** | | thin-shell | 398 | 417 |
| staircase-overhang | 42 | **16** | | mushroom | 265 | 269 |
| wide-arch | 41 | **28** | | sparse-pillars | 20 | 20 |
| letter-h | 26 | **18** | | letter-t | 14 | **13** |
| topple-arm | 14 | **8** | | arch | 14 | **13** |
| two-towers-bridge | 81 | **75** | | pyramid | 136 | **128** |
| letter-h-bicolour | 28 | **22** | | | | |

11 of 13 better or equal, up to 2.6× fewer bricks; stability verdicts
identical to bond on every model. Tests pin the per-layer optimality
claim exactly: brute-force DFS minimum matched on small shapes,
never-worse-than-bond on seeded random layers, stage-2 border
avoidance on a two-course wall, fallback and determinism. Runtime
bounded and small (≤ 8.6 s on the largest synthetic).

Corpus scorecard: zero hard regressions, all 11 manifest expectations
PASS, and kollsker immediately *wins* cantilever outright in the
all-strategies sweep (objective 0.6245 → 0.5555 vs the old beauty
winner).

### 2026-07-19 — Item 4: SNOT sideways finishing pass (`--snot`)

Shipped the first sideways building in the pipeline, end to end
through the data model rather than as an emission hack. New
`Category.SNOT` parts: the 1x1 side-stud bracket (87087 — a normal
column plus a lateral stud at mid-height) and the sideways 1x1 tile
(3070b — conservative 3-plate collision volume, token centre fill, one
lateral anti-stud). The six planned seams all took the change:
`graph` sockets are now keyed (cell, direction) with mates at cell +
direction and a ground guard for down-only anti-studs (`KnobContact`
gains `normal`); the RBE lays the FOUR_POINT diamond in the *vertical*
mating plane, with pull-off riding the same T-bounded drag machinery
and vertical stud-shear presses carrying the tile's weight;
`piece_for` emits the bracket as yaw+90 about Y (the physical side
stud points LDraw -Z — read from 87087.dat, not assumed) and the tile
via four probed axis rotations; the LDraw importer round-trips both at
every yaw; blockers for lateral-mount parts follow the outward
slide-in ray instead of the vertical sweep; and the warm
PrefixSolver/RemovalSolver decline SNOT layouts (their contact
discovery is z-up-only) so physics falls back to the always-correct
cold engine. The carve-and-refill surgery moved to shared
`placement/carve.py` (slopes and snot both use it).

The pass (`placement/snot.py`): brick-aligned wall windows whose
outward neighbour is strictly outside the shape, in vertical runs of
≥2 windows, get a bracket + outward-facing tile. Two measured safety
rails: only free-standing 1x1 wall columns are converted — carving a
bracket out of a wall-spanning brick demolishes its bonding (the first
live run converted an entire 1x4 wall into disconnected bracket
towers), and mounts re-check the target column at mount time (two
perpendicular faces share an inside-corner column — collided on
suzanne). The pipeline snapshot guard reverts wholesale on a
stability flip, same as preserve slopes.

Proof (seed 0): fire counts and physics —
| model | mounts | stable | components |
|---|---|---|---|
| two-towers-bridge | 23 | yes | 1 |
| mushroom | 41 | yes | 1 |
| thin-shell | 12 | yes | 1 |
| suzanne@16 | 6 | yes | 1 |
| pyramid / letter-t / sparse-pillars | 0 | yes | unchanged |

Renders confirm visibly smooth clad tower faces on two-towers-bridge.
Off by default: goldens byte-exact, StableLego cross-validation
untouched, 467 tests. Sequencing orders every tile with or after its
bracket (lateral contacts are support edges), `verify_plan` clean.

Deviations from plan: the carve+refill mount path was narrowed to
single-column donors after the wall-demolition measurement — the
plan's "1x1-swap or carve+refill" became "1x1-column carve only"; the
5-plate/2-stud quantum never arises, as designed. Old roadmap "SNOT —
sideways building" section: v1 scope DONE (bracket + sideways tile);
full sideways regions/orientation field remain future work.

Deviations from plan: end-to-end brick counts on mushroom (+4) and
thin-shell (+19) came out slightly worse than bond — the per-layer
bound is structural, but remerge/repair/hollow-restore interact with
the different layer seams downstream, exactly the drift the plan said
to report empirically. `test_compare`'s hardcoded strategy counts now
derive from `strategy_names()`. The committed synthetic baseline was
regenerated in this commit to include the seventh strategy.

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
(Kendall's τ + RLSD) and `plan_quality` quantify orders. MPD
subassembly submodels: **DONE in v3 item 2** (`--subassemblies`, see the
progress notes above). Still deferred: ±X/±Y block edges (SNOT-only; the
blocker-map `Mapping[int, frozenset[int]]` seam in `blocking.py` is the
plug-in point).

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
