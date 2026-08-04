# legolization

Turn a colored voxel model into a **physically buildable LEGO model** in LDraw
format, with step-by-step build instructions and a bill of materials.

This is the classic "LEGO construction problem" from the research literature
(see `references/`): voxelize → hollow → place bricks → check structural
stability → repair → export. The stability check is a full
**Rigid-Block-Equilibrium (RBE)** model (StableLego formulation, cross-validated
against its released test fixtures): per-brick force *and* torque balance with
knob-friction capacities, solved as a provably exact linear program on an open
solver stack — no Gurobi required.

## What it does

- **Input**: MagicaVoxel `.vox`, NumPy `.npy`, and `.obj`/`.stl`/`.ply` mesh
  files. Missing VOX RGBA data uses the MagicaVoxel default palette. PACK and
  scene-graph transforms are composed; conflicting overlaps and ambiguous
  unplaced multi-model files are rejected.
- **Placement**: covers every voxel with bricks and plates at true heights
  (plate = 8 LDU, brick = 24 LDU). The redesigned CLI defaults to `auto`:
  bounded whole-model exact placement is used below its cell and candidate
  caps and deterministic `bond` placement otherwise. Exact placement enforces
  target coverage, LDU collision, colour, and rooted stud connectivity, then
  cold-certifies physics. Shape-preserving slopes (including inverted slopes)
  and exposed-region tile splitting are default finishing passes; neither may
  change target occupancy. Plate caps are opt-in because they change layering.
  The available heuristic strategies remain:
  - `greedy` (default): largest-first bottom-up fill with Kollsker's
    remainder-lookahead h(r) and distance-decayed stretcher-bond scoring,
    then delete-and-rebuild reinforcement around the weakest bricks.
  - `luo`: Luo et al. (2015) maximal random merge with split-and-remerge
    refinement, accepted by Luo's maximin friction capacity C_M; supports
    soft colour constraints (`--colour soft`).
  - `bond`: Kollsker & Malaguti's constructive brick-bonding heuristic —
    remainder lookahead + staggering reward + per-layer repair.
  - `fast`: Bao et al.'s greedy per-layer merge with a dominant big-brick
    weight, perpendicularity term, and connectivity retries.
  - `smga`: Lee et al.'s split-and-merge genetic algorithm per layer
    (`--ga-generations`, `--time-budget`).
  - `beauty`: Min et al.'s objective-driven tiling with symmetry/balance,
    stability-priority, and big-brick terms (`--beauty-preset
    {balanced,stability,aesthetics,efficiency}`).
  - `kollsker`: Kollsker & Malaguti's exact set-partitioning MILP, solved
    per 4-connected component of each layer — stage 1 minimizes the part
    count, stage 2 maximizes stagger quality at that optimum; falls back
    to `bond` per component on timeout.
  - `global-exact`: bounded 3D exact placement with a brick-count or mass
    objective and explicit `fail`, `fallback`, and deadline-bound `continue`
    limit policies.
- **Physics**: every layout is scored by the RBE — gravity, support, press,
  drag/pull friction (capacity T = 0.98 N per contact point), knob presses,
  and torque-capable side presses at shared-face extremes (side-supported
  structures shed load like Luo's bridges). Equilibrium residuals sit *in the
  objective*, so even collapsing structures solve and failures localize to
  specific bricks.
- **Repair**: unstable layouts go through an ALNS destroy-and-repair pass
  (Kollsker's artificial-link QP pinpoints the deficit; the freed region is
  refilled by the merge engine or an exact-cover MILP) before any material is
  added back by the stability-aware hollow-restore loop.
- **Auto-hollow**: interiors are hollowed to a shell (~1 brick thick, tunable
  with `--shell-plates`); interior cells are colour-free so merges never
  fragment on invisible boundaries.
- **Instructions**: smart step sequencing (default) chunks each layer into
  ~7-brick spatially coherent steps, keeps mirror-symmetric halves together,
  prefers spatially adjacent steps (Ma et al.'s continuity heuristic),
  validates every expanded action for collision, insertion, connectivity,
  support, and cold prefix stability, and
  adds `0 ROTSTEP` view hints. When the greedy pass hits an unstable stretch
  it re-plans the remainder by assembly-by-disassembly along a
  maximal-stability path (Tian et al. / Luo); an opt-in beam search
  (`InstructionsConfig(search="beam")`) explores whole build orders. `--bom
  out.json` writes a bill of materials with per-step callouts.
- **SNOT cladding**: the opt-in SNOT pass clads tall flat wall faces with
  sideways tiles on exact connector geometry, including 87087 carriers, 4070
  headlight bricks, and 99781 inverted brackets — real receiving
  geometry, priced by the same RBE physics through genuine lateral stud
  contacts. Only free-standing 1x1 wall columns are converted (carving a
  bracket out of a wall-spanning brick would destroy its bonding), and
  the pass reverts wholesale if it would flip the stability verdict.
- **Subassemblies**: `--subassemblies` detects stretches that float in every
  build order (mushroom caps, arches), lifts them out as separately built
  units — each constructed stably on the table, then attached as one piece —
  and emits them as `.mpd` submodel FILE sections. Booklets get per-unit
  sections and attach callouts; the `.ldr` fallback flattens attach steps
  back to world-frame bricks.
- **Booklets**: `--instructions out.html` (or `.pdf`) writes a paginated
  instruction booklet — cover page with model stats, parts list, and one
  rendered image per step with new bricks highlighted and per-step part
  callouts. Step images render through LeoCAD (preferred; batched per-step
  export) or LDView, auto-detected from `$LEGOLIZATION_RENDERER`, PATH, then
  `/Applications`; the parts library is found via `$LDRAWDIR` or common
  install paths. Without a renderer the booklet is still written with
  placeholder boxes (`LEGOLIZATION_RENDERER=none` disables rendering
  explicitly, e.g. in CI).
- **Output**: a valid `.ldr` or `.mpd` written through
  [pyldraw3](https://pypi.org/project/pyldraw3/). Open it in
  [LDView](https://tcobbs.github.io/ldview/) or
  [BrickLink Studio](https://www.bricklink.com/v3/studio/download.page).
- **LDraw analysis**: `analyze` preserves exact arbitrary occurrence matrices
  and reports geometry, contact, physics, and planning capabilities separately.
  Supported pitch/roll poses are analyzed without snapping; unsupported
  capabilities retain completed evidence and produce a partial manifest.

## Setup

```sh
uv sync
uv run ldraw download --yes   # once: fetch the LDraw parts library
uv run ldraw generate --yes   # once: generate ldraw.library.* part/colour modules
```

## Usage

```sh
uv run legolization build data/examples/heart.vox -o heart.ldr
uv run legolization build model.npy -o model.ldr --strategy global-exact
uv run legolization build model.obj -o model.ldr --target-studs 24
uv run legolization build model.obj -o model.ldr --auto-scale 16 32
uv run legolization build model.vox -o model.ldr --config legolization.toml
uv run legolization analyze model.ldr
uv run legolization validate model.manifest.json --against model.ldr
uv run legolization cache inspect
uv run legolization cache clear --key SHA256
```

Builds write `<output>.manifest.json`; analyses write
`<input-stem>.manifest.json`. Use `--manifest PATH` or `--no-manifest` to
override that per run. Exit codes are `0` complete/feasible, `1` input or
runtime error, `2` completed but infeasible, `3` partial analysis, and `4` an
exact-solver limit under the `fail` policy.

Configuration is strict nested TOML. Built-in defaults are applied first,
then the file, then only command-line options explicitly supplied by the user.
Relative paths are resolved from the TOML file; unknown keys and incompatible
options fail before work starts.

```toml
[placement]
strategy = "auto"
objective = "bricks"
restarts = 1

[placement.exact]
max_cells = 256
max_candidates = 100000
time_limit_s = 60
limit_policy = "fail"

[stability]
profile = "corrected"
support = "baseplate"

[output]
manifest = true
emit_support = false
```

### Analyze an existing LDraw model

`analyze` is the non-generative feasibility workflow. It always builds a
geometry-first assembly from pyldraw's resolved occurrences, so arbitrary
parts, MPD transforms, SNOT/half-stud placements, angled mechanisms, and every
LDraw colour can produce topology even when the legacy voxel `Layout` adapter
rejects them. Confirmed and potential connector graphs remain separate, and an
unsupported connector produces partial evidence instead of aborting the model.

```sh
uv run legolization analyze model.ldr
uv run legolization analyze assembly.mpd --topology-only --no-repair
uv run legolization analyze vehicle.mpd --support wheels --scenario side-load
uv run legolization analyze model.mpd \
  --path-between pages:1-20 pages:80-100 --artifact-dir diagnostics
uv run legolization analyze model.ldr \
  --report evidence.json --output repaired.ldr --time-budget 120 --seed 7
uv run legolization analyze model.ldr \
  --connector-catalog connectors.json --ldcad-metadata shadow-library/
```

Connection evidence comes from pyldraw3's typed connection subsystem.
`--ldcad-metadata` registers an LDCad shadow library (a directory or a
ZIP/CSL archive) and `--studio-metadata` a Studio connectivity JSON export as
connection-feature sources on the parts catalog; unreadable sources exit with
an error before analysis starts. `--connector-catalog` (schema-1 JSON,
validated against the packaged `connector-catalog-v1` schema) remains the
only source for mass, centre of mass, inertia, collision proxies, region
tags, force capacities, and custom connector kinds — Studio `mass_g`/`tags`
fields are no longer read.

Normal runs write the canonical version-1 assembly manifest. The old schema-2
analysis and schema-1 assembly JSON are available only as explicitly requested
derived views through `--report` and `--assembly-report`. Graph, component MPD,
and floating MPD artifacts remain available; `--no-data-artifacts` suppresses
them. `--artifact-dir` additionally enables a
self-contained HTML report, missing-connector callouts, and before/after
renders when a renderer is installed. Only one JSON report may target stdout.

Support defaults are adaptive: strict voxel-compatible models use an anchored
baseplate, detected vehicles rest on their wheels, and other arbitrary models
use loose lowest-surface contacts. `--support free` deliberately reports no
static verdict. Unknown load-bearing capacities keep physics indeterminate
even when an optimistic equilibrium exists. Exit codes are assembly-driven:
`0` means connected/feasible, `2` means definitely disconnected/infeasible,
`3` means partial/indeterminate, and `1` means invalid input or runtime error.

On a definite failure, the repair budget searches one BOM-preserving source
edit at a time (orthonormal rotations/reflections and nearby stud/plate
translations). A suggestion must improve real connector topology, never just
bounding-box overlap. The best validated edit is written automatically to
`model.repaired.ldr` or `.mpd`; `--no-repair` disables the search.

The preserved legacy report uses schema 2. Its `ldraw` block records pyldraw's prepared
catalog state, tolerant-load diagnostics, exact transformed bounds, official
BOM, occurrence provenance, and renderer-neutral instruction sections. Exact
stud contacts and AABB gaps are included through 1,000 occurrences and marked
as skipped above that safety limit. The analyze command prepares the configured
catalog automatically; if the library is missing, run `ldraw download --yes`.

The assembly schema records exact occurrence transforms and provenance,
connector coverage, confirmed/optimistic component counts, detected grid
frames, resolved support, region-to-region cuts, load scenarios, and ranked
counterfactual evidence. Public occurrence IDs are one-based; pyldraw's
zero-based traversal index is retained separately.

LDraw analysis preserves the imported matrix and position exactly. Catalog and
assembly capabilities decide whether collision, connector, and physics
operations support that pose; no meaningful pitch, roll, or half offset is
silently snapped to the generated-placement lattice.

Catalog extensions declare `"schema": 2` and a `parts` list. Rectangular
bricks, plates, and tiles use explicit `size`, `height_plates`, and measured
`mass_g`. A custom non-rectangular part must instead declare its complete
`occupied_cells`, `filled_cells`, `top_connectors`, `bottom_connectors`,
`orientations`, `origin_offset`, `height_plates`, and measured `mass_g`.
Extensions cannot override keys or introduce an ambiguous LDraw decode.

Mesh inputs (`.obj`/`.stl`/`.ply`) are voxelized directly at plate
resolution (always aspect-correct): `--target-studs N` sets the footprint
width (or `--pitch` for explicit model-units-per-stud), `--up y` handles
the common Y-up convention, `--mesh-colour CODE` picks the uniform colour,
and `--no-fill` keeps shell meshes hollow. Disconnected mesh components are
preserved by default; `--largest-component-only` discards every smaller
voxel island and always reports how many voxels were removed.
`--mesh-colour-mode sampled` colours each voxel from the mesh's
texture/vertex colours (nearest-vertex, quantized to the LDraw palette),
falling back to `--mesh-colour` with a note when the mesh carries no
colour data — note a loose `.obj` without its `.mtl`/texture (e.g. the
corpus `spot.obj`) has none, so it stays uniform.

`--strategy all` runs every registered strategy on the same input (in
parallel worker processes; `--jobs 1` forces sequential) and keeps the best
model. Selection is lexicographic, following the reference papers: candidates
are first gated on buildability (stable, one connected component, nothing
floating), and the survivors are ranked by the weighted objective, with ties
broken by maximin friction capacity, then brick count. `--report` writes a
JSON comparison of every strategy, `--keep-candidates DIR` also writes each
strategy's model, and `--timeout SECONDS` sets a soft deadline for the overall
parallel sweep while also becoming the cooperative time budget for strategies
that support one. Workers already running at the deadline cannot be terminated
and may continue after the sweep returns.

Heuristic multi-seed restarts are opt-in (`--restarts`; default 1). Seeds are
deterministic, exact placement is never raced, and the winner is ordered by
buildability, components/floating parts, worst stability score, the configured
cost objective, and a canonical layout signature.

```text
restart race: seeds 0..2 -> seed 2
wrote heart.ldr
  bricks: 12   mass: 17.9 g   steps: 8   slopes: 0   tiles: 0
  stability: STABLE (worst score 0.001, min capacity 0.979 N)
```

Exit code 0 means the model is stable, one stud-connected component, and
ground-connected. Exit 2 means it is not buildable as-is (try another
`--strategy`, `--solid`, or a different `--seed`) — note that an input made of
several disconnected voxel islands is reported as multiple components even
when every island stands on the ground.

Python API:

```python
from pathlib import Path
from legolization import (
    AnalysisConfig,
    AssemblyAnalysisConfig,
    PipelineConfig,
    VoxelGrid,
    analyze_assembly,
    analyze_ldraw,
    run,
    run_file,
)

result = run_file(Path("model.vox"), Path("model.ldr"), PipelineConfig(seed=1))
print(result.buildable, result.step_count, result.stability.max_score)

analysis = analyze_ldraw(
    Path("existing.mpd"),
    AnalysisConfig(repair_time_budget_s=120, seed=7),
)
print(analysis.report.verdict, analysis.report.to_json())

assembly = analyze_assembly(
    Path("vehicle.mpd"),
    AssemblyAnalysisConfig(support="wheels", scenarios=("auto",)),
)
print(assembly.report.topology_verdict, assembly.report.physics_verdict)
```

## How the stability model works

Each mated stud contributes 3 or 4 contact points (per StableLego's measured
geometry) carrying a shared normal force and a friction (drag/pull) force, so
Newton's third law holds by construction; each knob adds four horizontal
knob-press forces, and laterally touching bricks exchange side presses at
exact shared-face extremes so lateral load transfer carries torque. The
corrected default enforces all three force and all three torque balance rows,
rotates contact patterns with part yaw, and uses the paper contact-point rule.
A named `stablelego-parity` profile retains reproduction behavior. A brick
scores `1` when it
cannot reach equilibrium or its friction demand exceeds T; otherwise
`drag_max / T` — so the score doubles as a stress heatmap. The default solver
is a hand-assembled LP on scipy/HiGHS, and the relaxation is provably exact
(each contact's press and pull columns are exact negatives, so no optimum ever
uses both). Complementarity MILP is not a production CLI mode; a private
small-instance oracle remains for equivalence tests.

## Benchmark

Existing evaluation and comparison utilities are preserved for regression and
research work. Expanding their corpora, metrics, or baselines is intentionally
deferred; they are not acceptance gates for the foundation program.

## Development

```sh
uv run pytest          # fast inner loop; slow integrations skip by default
uv run pytest --run-slow  # full suite, including benchmark/sweep/renderer tests
uv run ruff format --check . && uv run ruff check .
uv run ty check src tests
uv run pyrefly check src tests
uv run deptry .
uv run lizard --languages python --CCN 15 --length 120 --arguments 8 src/legolization
```

## License

GPL-3.0-or-later (inherited from pyldraw3).
