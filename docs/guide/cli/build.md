# `build`

Build an LDraw model from 3D input using a single strategy.

```
legolization build -o OUTPUT
                   [--strategy {auto,global-exact,greedy,luo,bond,fast,smga,beauty,kollsker}]
                   [--seed SEED]
                   [--target-studs N | --auto-scale MIN MAX]
                   [--grid-phases {1,2,4,8}] [--restarts N] [--jobs N]
                   [--objective {bricks,mass}]
                   [--exact-limit-policy {fail,fallback,continue}]
                   [--manifest PATH | --no-manifest]
                   [--config PATH] [--set KEY=VALUE]
                   [--catalog PATH] [--catalog-estimates PATH] [--json]
                   input
```

`build` is the low-level entry point: one strategy, one output file, no candidate
sweep, no bundle directory. Use it for benchmarking a specific strategy, for
scripting, or when you want exactly one `.ldr` and nothing else.

For normal use, prefer [`bundle`](bundle.md) — it runs the sweep, picks a winner on
evidence, and produces the BOM and instructions.

---

## Options

| Flag | Type | Default | Effect |
| --- | --- | --- | --- |
| `input` | path | required | `.vox`/`.npy`/`.obj`/`.stl`/`.ply` model, or a `-prepared` bundle directory. |
| `-o`, `--output` | path | **required** | Must end in `.ldr` or `.mpd`. |
| `--strategy` | see choices | `placement.strategy` (`auto`) | See [Choosing a strategy](../choosing-a-strategy.md). |
| `--seed` | int | `placement.seed` (`0`) | Deterministic RNG seed. |
| `--target-studs` | int > 0 | none | Footprint width. **Mutually exclusive** with `--auto-scale`; setting it clears `input.mesh.auto_scale`. |
| `--auto-scale MIN MAX` | 2 ints > 0 | none | Search a stud range and pick the best voxelization. |
| `--grid-phases` | `1`,`2`,`4`,`8` | `input.mesh.grid_phases` (`8`) | Half-cell sampling offsets tried. |
| `--restarts` | int > 0 | `1` | Race seeds `[seed, seed+restarts)`. |
| `--jobs` | int > 0 | `1` | Parallelism for the restart race. |
| `--objective` | `bricks`, `mass` | `bricks` | Minimize part count or mass. |
| `--exact-limit-policy` | `fail`, `fallback`, `continue` | `fail` | Behaviour at an exact-placement limit. `continue` requires `placement.time_budget_s`. |
| `--manifest PATH` | path | `<output>.manifest.json` | Manifest destination. Mutually exclusive with `--no-manifest`. |
| `--no-manifest` | flag | off | Skip the manifest sidecar. |
| `--config`, `--set`, `--catalog`, `--catalog-estimates`, `--json` | | | See [shared options](index.md#shared-option-groups). |

---

## How `auto` resolves

`--strategy auto` is decided by grid size, not by guesswork:

```
filled_count <= placement.exact.max_cells  →  global-exact
otherwise                                  →  placement.exact.fallback_strategy  (bond)
```

Both caps are configurable — see [`[placement.exact]`](../configuration.md#placementexact).

---

## Multi-seed restarts

Heuristic restarts are **opt-in**. With `--restarts N`, seeds `[seed, seed+N)` are
run and the winner is ordered by:

1. buildability,
2. component count and floating parts,
3. worst stability score,
4. the configured cost objective,
5. a canonical layout signature (so ties are deterministic).

```console
$ legolization build data/examples/heart.vox -o heart.ldr --restarts 3
restart race: seeds 0..2 -> seed 2
wrote heart.ldr
  bricks: 12   mass: 17.9 g   steps: 8   slopes: 0   tiles: 0
  stability: STABLE (worst score 0.001, min capacity 0.979 N)
```

!!! danger "Exact placement is never raced"

    `--restarts > 1` raises a configuration error if the strategy resolves to
    `global-exact`. Exact placement is deterministic, so extra seeds would repeat
    identical work.

---

## Examples

```sh
# Simplest possible build
legolization build data/examples/heart.vox -o heart.ldr

# Mesh at a chosen footprint width
legolization build model.obj -o model.ldr --target-studs 24

# Let the tool pick the size
legolization build model.obj -o model.ldr --auto-scale 16 32

# One named strategy, three seeds, in parallel
legolization build model.npy -o model.ldr --strategy luo --restarts 3 --jobs 3

# Minimize mass instead of part count
legolization build model.vox -o model.mpd --objective mass

# No sidecar
legolization build model.vox -o model.ldr --no-manifest
```

---

## Output and manifest

Successful builds print a summary and write `<output>.manifest.json` unless
`--no-manifest` is passed or `output.manifest = false`.

The manifest is the canonical `legolization.assembly-manifest` version 1 document:
hashes, algorithms, exact LDU poses, normalized contacts, capability results,
stability evidence, action relations, instructions, BOM data, artifacts, and cache
provenance. It contains no wall-clock timestamp, so identical inputs produce
identical manifests.

Check one against a model with [`legolization validate`](parts-cache-validate.md).

Under `--json`, the envelope's `data` block carries `strategy`, `brick_count`,
`mass_g`, `step_count`, `stable`, and `buildable`.

---

## Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | Buildable: stable, one stud-connected component, ground-connected. |
| 1 | Operational error. |
| 2 | Not buildable as built. |
| 4 | Exact-placement limit under the `fail` policy. |
| 130 | Interrupted. |
