# `input`

Inspect and normalize source models before generating.

```
legolization input inspect [--write] [--up {x,y,z}]
                           [--target-studs N | --auto-scale MIN MAX]
                           [--config PATH] [--set KEY=VALUE] [--json]
                           input
```

One operation: `inspect`. It answers the questions that decide whether a generation
run will produce what you expect — which way is up, how big the brick version will
be, and what colours were found — *before* you spend an hour on the pipeline.

---

## Options

| Flag | Type | Default | Effect |
| --- | --- | --- | --- |
| `input` | path | required | `.vox`, `.npy`, `.obj`, `.stl`, or `.ply`. |
| `--write` | flag | off | Also write the `-prepared` sibling bundle with a normalized `.npy` target and JSON sidecar. |
| `--up` | `x`, `y`, `z` | auto-classify | Override the mesh vertical axis instead of classifying it. |
| `--target-studs N` | int | none | Fix the footprint width. Mutually exclusive with `--auto-scale`. |
| `--auto-scale MIN MAX` | 2 ints | none | Search a stud range. |
| `--config`, `--set`, `--json` | | | See [shared options](index.md#shared-option-groups). |

---

## What it reports

Under `--json`, the envelope's `data` block carries the inspection report
(`legolization.input-inspection/v1`): input kind, grid shape, recommended target
studs and plates-per-voxel, a colour summary, and a list of **conditions**.

Conditions are not failures. Inspection exits 0 and lists them; it is on you (or your
agent) to decide what each one means.

| Condition | What it means | What to do |
| --- | --- | --- |
| `ambiguous-up-axis` | Orientation could not be classified confidently | Re-run with `--up x`, `--up y`, or `--up z`. Most `.obj` files are **y-up**. |
| `no-colour-data` | The mesh carries no vertex or texture colour | Choose a uniform colour: `--set input.mesh.colour_code=N` |
| `multiple-components` | The source is several disconnected pieces | Expect multiple components in the result — that is not a bug. Components are preserved by default, never silently merged or dropped. |
| `not-watertight` | The mesh has holes | Voxelization fill may behave unexpectedly; consider `--set input.mesh.fill=false` for genuine shell meshes. |
| `empty-grid` | Nothing to build | Stop. Check the file, the scale, and the up-axis. |

---

## Sizing

`--target-studs N` sets the **footprint width**, not the height. `--auto-scale MIN MAX`
searches the inclusive range instead, and the selector picks the voxelization
minimizing, in order:

1. surface error — mean distance from surface-voxel centres to the mesh, normalized
   by pitch,
2. detail retention (higher is better),
3. constructibility — the fraction of filled cells with a face-adjacent neighbour
   (higher is better),
4. then smaller size and lower grid phase, for determinism.

Independently of size, `input.mesh.grid_phases` (default **8**) controls how many
half-cell sampling offsets are tried. More phases means a better fit at more cost.

---

## The prepared bundle

`--write` produces a `<name>-prepared/` sibling:

```
model-prepared/
  bundle.json
  normalized.npy      int16 colour codes on the plate lattice
  normalized.json     schema: legolization.input-normalized/v1
```

The sidecar records the source filename and hash, the `.npy` hash, the chosen scale,
the resolved orientation with its confidence, the colour mode and summary, and every
warning and condition. Resolution requires the `.npy` hash to match the sidecar, so a
tampered or truncated target is caught rather than silently used.

Hand the directory straight to generation:

```sh
legolization input inspect model.obj --write --up y --target-studs 24
legolization bundle model-prepared/
```

The point of the two-step form is reproducibility: the prepared bundle pins the
orientation, scale, and colour decisions, so a later run cannot quietly re-classify
them differently.

---

## Examples

```sh
# Just look
legolization input inspect model.obj

# Look, as JSON, for scripting
legolization input inspect model.obj --json

# Fix an ambiguous up-axis and pin a size
legolization input inspect model.obj --write --up y --target-studs 24

# Let it search a size range
legolization input inspect model.stl --write --auto-scale 16 32

# Uncoloured mesh: pick one colour
legolization input inspect model.obj --write --set input.mesh.colour_code=4

# Sample colours from the mesh instead
legolization input inspect model.obj --write --set input.mesh.colour_mode=sampled
```

---

## Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | Always, when inspection completed — including when conditions were reported. |
| 1 | Operational error: unreadable file, unsupported format. |

There is no exit 2 or 3 here. Inspection does not have a verdict to fail.
