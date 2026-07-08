# legolization

Turn a colored voxel model into a **physically buildable LEGO model** in LDraw
format, with step-by-step build instructions.

This is the classic "LEGO construction problem" from the research literature
(see `references/`): voxelize → hollow → place bricks → check structural
stability → refine → export. The stability check is a full
**Rigid-Block-Equilibrium (RBE)** model (StableLego / BrickGPT formulation):
per-brick force *and* torque balance with knob-friction capacities, solved as
a linear program on an open solver stack — no Gurobi required.

## What it does

- **Input**: a MagicaVoxel `.vox` file or a numpy `.npy` array (LDraw colour
  codes or RGB(A) voxels — colours are quantized to the nearest solid LDraw
  colour).
- **Placement**: covers every voxel with real parts — bricks, plates, tiles,
  and 45°/33° slopes — using true heights (plate = 8 LDU, brick = 24 LDU) and
  a pluggable strategy:
  - `greedy` (default): largest-first bottom-up fill with stretcher-bond
    scoring, then delete-and-rebuild reinforcement around the physically
    weakest bricks.
  - `luo`: Luo et al. (2015) maximal random merge with split-and-remerge
    refinement for connectivity and stability.
- **Physics**: every candidate layout is scored by the RBE — gravity, support,
  press, drag/pull friction (capacity T = 0.98 N per contact point), and
  knob/side press forces, with equilibrium residuals *in the objective* so
  even collapsing structures solve and the failure localizes to specific
  bricks.
- **Auto-hollow**: interiors are hollowed to a shell, but fill is restored
  wherever the physics says the shell would collapse.
- **Output**: a valid `.ldr` or `.mpd` with bottom-up `0 STEP` build
  instructions, written through [pyldraw3](https://pypi.org/project/pyldraw3/).
  Open it in [LDView](https://tcobbs.github.io/ldview/) or
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
uv run legolization model.npy --strategy luo --solid --seed 7
uv run legolization model.vox --slopes --tiles      # surface finishing passes
uv run legolization model.vox --milp                # exact complementarity physics
```

The CLI reports brick count, mass, and the physics verdict:

```
wrote heart.ldr
  bricks: 31   mass: 18.1 g   slopes: 0   tiles: 0
  stability: STABLE (worst score 0.000, min capacity 0.980 N)
```

Exit code 0 means the model is stable, single-component, and ground-connected;
2 means it is not buildable as-is (try `--strategy luo`, `--solid`, or a
different `--seed`).

Python API:

```python
from pathlib import Path
from legolization import PipelineConfig, VoxelGrid, run, run_file

result = run_file(Path("model.vox"), Path("model.ldr"), PipelineConfig(seed=1))
print(result.buildable, result.stability.max_score)
```

## How the stability model works

Each mated stud contributes 3 or 4 contact points (per StableLego's measured
geometry) carrying a shared normal force and a friction (drag/pull) force, so
Newton's third law holds by construction; each knob adds four horizontal
knob-press forces and laterally touching bricks exchange side presses. Per
brick, five equilibrium residuals (3 forces, 2 torques about the mass centroid)
are minimized rather than constrained. A brick scores `1` when it cannot reach
equilibrium or its friction demand exceeds T; otherwise `drag_max / T` — so the
score doubles as a stress heatmap. The default solver is a hand-assembled LP
on scipy/HiGHS (fast enough to sit inside refinement loops); `--milp` adds
big-M complementarity (a contact point cannot press and pull at once) via
cvxpy for final verification.

## Development

```sh
uv run pytest          # analytic physics cases, placement invariants, round-trips
uv run ruff format --check . && uv run ruff check .
uv run ty check src tests
uv run pyrefly check src tests
```

## License

GPL-3.0-or-later (inherited from pyldraw3).
