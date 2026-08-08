# `analyze`

Analyze LDraw geometry and, optionally, search for a valid repair.

```
legolization analyze [--config PATH] [--manifest PATH | --no-manifest]
                     [--report PATH] [--assembly-report PATH]
                     [--graph PATH] [--diagnostic-mpd PATH] [--floating-mpd PATH]
                     [--html-report PATH] [--callout-dir DIR] [--comparison-dir DIR]
                     [--artifact-dir DIR] [--no-data-artifacts]
                     [-o OUTPUT] [--time-budget SECONDS]
                     [--catalog PATH] [--catalog-estimates PATH]
                     [--connector-catalog PATH] [--ldcad-metadata PATH]
                     [--studio-metadata PATH] [--preserve-origin]
                     [--repair | --no-repair] [--effort {fast,balanced,exhaustive}]
                     [--repair-output DIR] [--no-step-check] [--seed SEED]
                     [--topology-only] [--support MODE]
                     [--path-between LEFT RIGHT]
                     [--scenario {auto,rest,lift-body,lift-chassis,front-torsion,rear-torsion,side-load}]
                     [--gravity-g G] [--side-load-g G] [--torsion-load-g G]
                     [--json]
                     input
```

`analyze` is the **non-generative** workflow. It takes an existing `.ldr` or `.mpd`
and answers: is this connected, is it stable, what carries the load, what falls off,
and — with `--repair` — can a single edit fix it.

It always builds a geometry-first assembly from pyldraw's resolved occurrences, so
arbitrary parts, MPD transforms, SNOT and half-stud placements, angled mechanisms,
and every LDraw colour can produce topology even when the voxel-lattice adapter would
reject the model.

---

## What it preserves

Imported geometry is preserved **exactly**. The matrix and position from the file are
kept as-is; no meaningful pitch, roll, or half offset is silently snapped onto the
generation lattice. Catalog and assembly capabilities then decide, capability by
capability, whether collision, connector, and physics operations support that pose.
An unsupported connector produces *partial evidence*, not an aborted model.

Confirmed and potential connector graphs stay separate throughout.

---

## Core options

| Flag | Type | Default | Effect |
| --- | --- | --- | --- |
| `input` | path | required | `.ldr` or `.mpd`. |
| `--config PATH` | path | none | Project TOML. **There is no `--set` on this command.** |
| `--topology-only` | flag | off | Skip mass, support, and equilibrium analysis. Conflicts with `--support`, `--scenario`, and the load-magnitude flags. |
| `--seed SEED` | int | `0` | Repair search seed. |
| `--no-step-check` | flag | off | Skip informational checks of source `STEP` prefixes. |
| `--preserve-origin` | flag | off | Keep LDraw layer zero authoritative; also switches the default support to `anchored-baseplate`. |
| `--json` | flag | off | Single envelope on stdout. Forbids `-` report targets. |

## Support and load scenarios

| Flag | Values | Default | Effect |
| --- | --- | --- | --- |
| `--support MODE` | `auto`, `free`, `wheels`, `auto-ground`, `anchored-baseplate`, `selected:IDS` | `auto` | How the assembly is seated. |
| `--scenario` | `auto`, `rest`, `lift-body`, `lift-chassis`, `front-torsion`, `rear-torsion`, `side-load` | `auto` | Load case. Repeatable. |
| `--gravity-g` | float ≥ 0 | `1.0` | Gravity multiplier. |
| `--side-load-g` | float ≥ 0 | `1.0` | Lateral load multiplier for `side-load`. |
| `--torsion-load-g` | float ≥ 0 | `0.5` | Torsion load multiplier. |

Support defaults are **adaptive**: strict voxel-compatible models get an anchored
baseplate, detected vehicles rest on their wheels, and other arbitrary models use
loose lowest-surface contacts.

`--support free` deliberately reports **no static verdict** — it is for topology and
load-path questions where seating is meaningless.

!!! note "Unknown capacities keep physics indeterminate"

    If a part's load-bearing capacity is not in the registry, the result stays
    indeterminate even when an optimistic equilibrium exists. Topology-only
    recommendations on an indeterminate result are explicitly marked **unverified** —
    that label is not decoration, and it should not be laundered into confident
    advice.

## Artifacts

| Flag | Default destination |
| --- | --- |
| `--artifact-dir DIR` | Aliases the analysis bundle directory; enables the HTML report, missing-connector callouts, and before/after renders when a renderer is installed |
| `--graph PATH` | `<bundle>/diagnostics/connections.json` |
| `--diagnostic-mpd PATH` | `<bundle>/diagnostics/components.mpd` |
| `--floating-mpd PATH` | `<bundle>/diagnostics/floating.mpd` |
| `--html-report PATH` | `<bundle>/analysis.html` |
| `--callout-dir DIR` | `<bundle>/diagnostics/callouts` |
| `--comparison-dir DIR` | `<bundle>/renders` |
| `--no-data-artifacts` | Suppresses graph, component MPD, and floating MPD |

`--path-between LEFT RIGHT` takes two region selectors — for example
`--path-between pages:1-20 pages:80-100` or `occurrences:20-30` — and reports the
load paths and minimum connector cuts between them. Repeatable.

## Reports

Normal runs write the canonical version-1 assembly manifest. The older schemas are
available only as explicitly requested derived views:

| Flag | Produces |
| --- | --- |
| `--report PATH` | Legacy schema-2 analysis JSON. `-` writes to stdout. |
| `--assembly-report PATH` | Legacy assembly schema-1 JSON. `-` writes to stdout. |
| `--manifest PATH` / `--no-manifest` | Canonical manifest, default `<stem>.manifest.json` |

Only one report may target stdout, every output path must differ from the input and
from each other, and `--json` forbids `-` entirely.

### `source_steps`

Unless `--no-step-check` is passed, reports carry a `source_steps` array: the
cumulative build prefixes implied by the model's own `STEP` lines, checked in
order. Each row describes one step of the root instruction section.

| Field | Meaning |
| --- | --- |
| `section` | The source section the step came from. |
| `step` | The step's index within that section. |
| `geometry_changed` | Whether the step added bricks the catalog supports. A step that adds none reuses the previous step's verdict. |
| `evaluated` | Whether a stability result is available for this prefix, including one reused from an unchanged step. |
| `stable`, `max_score` | The prefix's physics verdict, or `null` when `evaluated` is false. |
| `component_count`, `floating_count` | Prefix topology after the step. |
| `feasible` | `true`/`false` when `evaluated` is true — stable, connected, and nothing floating — and `null` otherwise. |

These checks are informational: a step that is not feasible does not by itself
change the command's exit code.

## Connection metadata sources

| Flag | Source |
| --- | --- |
| `--connector-catalog PATH` | Schema-1 JSON validated against the packaged `connector-catalog-v1` schema. **The only source** for mass, centre of mass, inertia, collision proxies, region tags, force capacities, and custom connector kinds. |
| `--ldcad-metadata PATH` | LDCad shadow library — a directory or a `.csl`/`.zip` archive. |
| `--studio-metadata PATH` | Studio connectivity JSON export (connections only; `mass_g`/`tags` are no longer read). |
| `--catalog PATH` | Legacy voxel catalog extension. |
| `--catalog-estimates PATH` | Estimate sidecar with labeled provenance. |

All are repeatable. An unreadable source exits with an error *before* analysis starts.

---

## Repair

```sh
legolization analyze model.ldr --repair --effort fast
```

On a definite failure, the repair search tries **one BOM-preserving source edit at a
time** — orthonormal rotations and reflections, and nearby stud/plate translations.
A suggestion must improve real connector topology; bounding-box overlap alone never
counts.

| Flag | Values | Default | Effect |
| --- | --- | --- | --- |
| `--repair` / `--no-repair` | | search runs by default | Enable or disable the repaired-model search. |
| `--effort` | `fast`, `balanced`, `exhaustive` | `balanced` | 60 s / 300 s / requires an explicit `--time-budget`. **Requires `--repair`.** |
| `--time-budget SECONDS` | float > 0 | `300` | Hard budget; always wins over the tier. |
| `--repair-output DIR` | path | `<stem>-repair` sibling | Repair bundle location. |
| `-o`, `--output` | path | `<stem>.repaired.ldr`/`.mpd` | The repaired **model file**. |

!!! success "Repair never overwrites the source"

    The best validated edit is written to a new file. A failed repair explains why
    and retains its best rejected candidate — which must never be presented as a fix.

---

## Examples

```sh
# Just tell me what's wrong
legolization analyze model.ldr --no-repair

# Topology only, no physics
legolization analyze assembly.mpd --topology-only --no-repair

# A vehicle, resting on its wheels, under a side load
legolization analyze vehicle.mpd --support wheels --scenario side-load

# Where does load travel between two regions?
legolization analyze model.mpd \
  --path-between pages:1-20 pages:80-100 --artifact-dir diagnostics

# Full diagnostics with HTML and renders
legolization analyze model.ldr --artifact-dir diagnostics

# Repair with an explicit budget and seed
legolization analyze model.ldr \
  --repair --output repaired.ldr --time-budget 120 --seed 7

# Bring in external connection metadata
legolization analyze model.ldr \
  --connector-catalog connectors.json --ldcad-metadata shadow-library/
```

---

## Exit codes

Exit codes are assembly-driven, not process-driven:

| Code | Meaning |
| ---: | --- |
| 0 | Connected and feasible. |
| 1 | Invalid input or runtime error. |
| 2 | Definitely disconnected or infeasible. |
| 3 | Partial or indeterminate — including unknown capacities and unverified recommendations. |
| 130 | Interrupted. |

With `--repair`, the repair outcome overrides: exit 2 means the search was exhausted
without a validated fix, and exit 3 means it timed out.

!!! tip "Parts library required"

    `analyze` prepares the configured catalog automatically. If the LDraw library is
    missing, run [`legolization parts sync`](parts-cache-validate.md).
