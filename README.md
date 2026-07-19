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

- **Input**: a MagicaVoxel `.vox` file or a numpy `.npy` array (LDraw colour
  codes or RGB(A) voxels — colours are quantized to the nearest solid LDraw
  colour, with optional Floyd-Steinberg dithering for gradients).
- **Placement**: covers every voxel with bricks and plates at true heights
  (plate = 8 LDU, brick = 24 LDU) using one of six strategies; tiles and 45°
  slopes are opt-in finishing passes (`--tiles`, `--slopes` — the catalogued
  33° slope and 2x2 slope are not yet placed by any pass):
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
  guarantees every prefix is stable and vertically insertable (or warns), and
  adds `0 ROTSTEP` view hints. When the greedy pass hits an unstable stretch
  it re-plans the remainder by assembly-by-disassembly along a
  maximal-stability path (Tian et al. / Luo); an opt-in beam search
  (`InstructionsConfig(search="beam")`) explores whole build orders. `--bom
  out.json` writes a bill of materials with per-step callouts.
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

## Setup

```sh
uv sync
uv run ldraw download   # once: fetch the LDraw parts library
uv run ldraw generate   # once: generate ldraw.library.* part/colour modules
```

## Usage

```sh
uv run legolization data/examples/heart.vox -o heart.ldr
uv run legolization model.npy --strategy beauty --beauty-preset aesthetics
uv run legolization model.vox --strategy bond --bom parts.json
uv run legolization model.vox --instructions booklet.pdf   # rendered booklet
uv run legolization model.npy --strategy luo --solid --seed 7
uv run legolization model.vox --slopes --tiles      # surface finishing passes
uv run legolization model.vox -o out.mpd --subassemblies  # separately built units
uv run legolization model.vox --aspect-correct      # keep cubic voxel aspect
uv run legolization model.vox --milp                # cross-check the exact LP
uv run legolization model.npy --strategy all --jobs 4 --report report.json
uv run legolization model.obj --up y --target-studs 24   # mesh input (M6)
```

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

The CLI reports brick count, mass, step count, and the physics verdict:

```text
wrote heart.ldr
  bricks: 12   mass: 17.7 g   steps: 7   slopes: 0   tiles: 0
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
from legolization import PipelineConfig, VoxelGrid, run, run_file

result = run_file(Path("model.vox"), Path("model.ldr"), PipelineConfig(seed=1))
print(result.buildable, result.step_count, result.stability.max_score)
```

## How the stability model works

Each mated stud contributes 3 or 4 contact points (per StableLego's measured
geometry) carrying a shared normal force and a friction (drag/pull) force, so
Newton's third law holds by construction; each knob adds four horizontal
knob-press forces, and laterally touching bricks exchange two side presses at
the shared face's vertical extremes so lateral load transfer carries torque.
Per brick, five equilibrium residuals (3 forces, 2 torques about the mass
centroid) are minimized rather than constrained. A brick scores `1` when it
cannot reach equilibrium or its friction demand exceeds T; otherwise
`drag_max / T` — so the score doubles as a stress heatmap. The default solver
is a hand-assembled LP on scipy/HiGHS, and the relaxation is provably exact
(each contact's press and pull columns are exact negatives, so no optimum ever
uses both); `--milp` re-verifies with explicit big-M complementarity via
cvxpy. The whole stack reproduces all nine verdicts of the StableLego
release's test fixtures (vendored under `tests/data/stablelego/`).

## Benchmark

`uv run python scripts/benchmark.py` compares all six strategies across the
example models (brick count, stability margin, seam/perpendicularity/symmetry
metrics, runtime). Highlights at seed 0: `bond` and `beauty` cover the arch in
13 bricks where largest-first greedy needs 32, and `beauty --beauty-preset
aesthetics` produces perfectly mirror-symmetric layers on symmetric models.
For picking one model right now rather than tabulating, `--strategy all` is
the CLI counterpart (see Usage).

## Development

```sh
uv run pytest          # analytic physics, placement invariants, golden pins
uv run ruff format --check . && uv run ruff check .
uv run ty check src tests
uv run pyrefly check src tests
```

## License

GPL-3.0-or-later (inherited from pyldraw3).
