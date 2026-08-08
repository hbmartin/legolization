# `bundle`

Run the complete generation pipeline into a portable bundle.

```
legolization bundle [-o DIR] [--fresh] [--retile]
                    [--quality {fast,balanced,exhaustive,direct}]
                    [--duration SECONDS] [--retry-materials]
                    [--render {auto,required,off}] [--cancel-pending]
                    [--config PATH] [--set KEY=VALUE]
                    [--catalog PATH] [--catalog-estimates PATH] [--json]
                    input
```

This is the command most people want. It accepts every supported input, runs the
candidate sweep, gates on buildability, publishes the winner, and produces the BOM,
instructions, and diagnostics.

---

## Input and what it means

| `input` | Behaviour | Bundle written |
| --- | --- | --- |
| `.vox`, `.npy`, `.obj`, `.stl`, `.ply` | Full generation | `<name>-legolization/` |
| `-prepared/` bundle directory | Full generation from a normalized target | `<name>-legolization/` |
| `.ldr`, `.mpd` | **Preserve** the assembly exactly as built; validate and publish instructions | `<name>-instructions/` |
| `.ldr`, `.mpd` with `--retile` | Convert to a coloured occupancy target and **regenerate** the brickwork | `<name>-optimized/` |

`--retile` is only valid for `.ldr`/`.mpd` inputs.

The source file is never modified.

---

## Options

| Flag | Type | Default | Effect |
| --- | --- | --- | --- |
| `-o`, `--output DIR` | path | operation-specific sibling | Bundle directory. |
| `--fresh` | flag | off | Force a fresh numeric-sibling run instead of resuming. |
| `--retile` | flag | off | Regenerate brickwork from an imported assembly's occupancy, keeping shape and colours. |
| `--quality` | `fast`, `balanced`, `exhaustive`, `direct` | `balanced` | Candidate policy — see [Quality tiers and budgets](../quality-and-budgets.md). |
| `--duration SECONDS` | float | tier default | Candidate time budget. **Required** for `exhaustive` and for `--retry-materials`. Overrides the tier default otherwise. |
| `--retry-materials` | flag | off | Run the four-plate → six-plate → solid ladder, sharing `--duration` between rungs. |
| `--render` | `auto`, `required`, `off` | `auto` | Booklet rendering policy — see below. |
| `--cancel-pending` | flag | off | Terminate this bundle's detached workers, keeping completed artifacts for a resume. |
| `--config PATH` | path | none | Project TOML configuration. |
| `--set KEY=VALUE` | repeatable | — | Override one dotted configuration key. |
| `--catalog PATH` | repeatable | `[]` | Parts-catalog extension. |
| `--catalog-estimates PATH` | repeatable | `[]` | Estimate sidecar with labeled provenance. |
| `--json` | flag | off | Single result envelope on stdout. |

---

## Rendering policy

`--render` decides what happens when no renderer is installed. The policy is
explicit because a booklet with placeholder pages is worse than no booklet.

| Value | No renderer available | Some steps fail to render |
| --- | --- | --- |
| `auto` *(default)* | HTML/PDF booklet is **omitted entirely** — no placeholders. The omission is recorded. Exit 0. | Booklet keeps explicit missing-step markers; exit 3. |
| `required` | Partial result, exit 3. | Partial result, exit 3. |
| `off` | Never renders. | n/a |

Under every policy the step-annotated `.mpd` and the instruction audit are still
produced — those do not need a renderer. See
[Rendering and parts](../rendering-and-parts.md) for installing one.

---

## Examples

```sh
# Balanced quality into a sibling bundle
legolization bundle data/examples/heart.vox

# Quick preview
legolization bundle model.obj --quality fast

# Final version, with a budget you chose
legolization bundle model.npy --quality exhaustive --duration 3600

# Project config, and insist on a booklet
legolization bundle model.vox --config legolization.toml --render required

# Preserve an existing assembly, just publish instructions for it
legolization bundle assembly.mpd

# Rebuild an existing assembly's brickwork
legolization bundle castle.ldr --retile

# Retry an unbuildable result down the material ladder
legolization bundle model.obj --retry-materials --duration 1800

# One specific strategy, no sweep
legolization bundle model.obj --quality direct --set placement.strategy=luo

# Stop the detached workers from a timed-out run
legolization bundle model.obj --cancel-pending
```

---

## Output

```console
$ legolization bundle data/examples/heart.vox
bundle: heart-legolization (complete)
  ingest: complete
  candidates: complete
  selection: complete
  model: complete
  bom: complete
  instructions: complete
```

The bundle contains:

```
heart-legolization/
  bundle.json                    authoritative record
  comparison/report.json         every candidate, scored
  model/model.mpd                the winner
  model/model.ldr
  bom/bom.json                   bill of materials
  instructions/audit.json        buildability audit (always)
  instructions/instructions.html only when a renderer is available
  instructions/instructions.pdf
  diagnostics/                   only when nothing was buildable
```

Full detail: [Bundles and artifacts](../bundles-and-artifacts.md).

---

## Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | Buildable and complete. |
| 1 | Operational error (bad input, unreadable config, stage failure). |
| 2 | Not buildable — no candidate passed the gate. Best rejected model retained under `diagnostics/`. |
| 3 | Partial: workers still running past the soft deadline, a non-certified instruction audit, a missing renderer under `--render required`, or a booklet with missing steps. |
| 4 | Exact-placement limit hit under the `fail` policy. |
| 130 | Interrupted after atomically recording resumable state. |

`--cancel-pending` exits 0 once the cancellation is recorded.

On exit 2, the useful next moves are `--retry-materials`, a different `--quality`,
or a different `--seed`. Note that an input made of several disconnected voxel
islands is reported as multiple components — and therefore exits 2 — even when every
island stands on the ground.
