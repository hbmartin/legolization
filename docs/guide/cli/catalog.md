# `catalog`

Infer and validate parts-catalog extensions.

```
legolization catalog infer    [--key KEY] [-o DIR] [--offline] [--json] part_id
legolization catalog validate [--json] path
```

The shipped catalog covers 58 parts. When a model needs one it does not know, this
command drafts support for it: research the part, estimate its geometry and mass,
validate the draft against five gates, and — only after it passes — activate it
explicitly.

---

## `catalog infer`

```sh
legolization catalog infer 4070
```

| Flag | Type | Default | Effect |
| --- | --- | --- | --- |
| `part_id` | str | required | LDraw part number, e.g. `3001`. |
| `--key KEY` | str | `part_<id>` | Catalog key for the drafted part. |
| `-o`, `--output DIR` | path | cwd | Parent directory for the support bundle. |
| `--offline` | flag | off | Skip keyed and network sources. The local dump is still consulted. |
| `--json` | flag | off | Single envelope on stdout. |

It writes a `<key>-legolization-support/` bundle:

```
part_4070-legolization-support/
  bundle.json
  catalog-extension.json     the drafted part definition
  draft-estimates.json       physical estimates with provenance
  sources.json               what was consulted, and what it said
  validation.json            gate results
  geometry/4070.dat          resolved LDraw geometry
  geometry/<occupancy>.json  derived cell occupancy
```

### Sources and provenance

Online lookups are budgeted — a 5-second per-request timeout inside a 15-second
total. Keyed sources are used when their API keys are present:

| Environment variable | Source |
| --- | --- |
| `LEGOLIZATION_BRICKLINK_DUMP` | Local BrickLink catalog export. Works offline. |
| `REBRICKABLE_API_KEY` | Rebrickable. Skipped under `--offline`. |
| `BRICKOWL_API_KEY` | BrickOwl. Skipped under `--offline`. |

!!! warning "Estimates are estimates"

    Drafted physical values carry recorded provenance and receive **no hidden safety
    adjustment**. Read `sources.json` before trusting a mass. A part whose mass came
    from a volumetric guess is not the same as one measured from a catalog dump, and
    the draft says which.

    Provenance methods are `volumetric`, `analogous-part`, `catalog-measured`, and
    `user-supplied`.

### Exit codes

| Code | Meaning |
| ---: | --- |
| 0 | Validated, measured, and confident. |
| 1 | Part not found. |
| 3 | Draft written, but not every gate passed. |

There is no exit 2. A draft is not a physics verdict.

---

## `catalog validate`

```sh
legolization catalog validate part_4070-legolization-support
```

| Flag | Type | Effect |
| --- | --- | --- |
| `path` | path | An extension JSON file **or** a `-legolization-support` directory. |
| `--json` | flag | Single envelope on stdout. |

Given a directory, it re-writes `validation.json` in place.

### The five gates

| Gate | Checks |
| --- | --- |
| `import` | The part's LDraw geometry loads and resolves. |
| `round-trip` | Writing and re-reading the part preserves it exactly. |
| `collision` | The exact-LDU collision boxes are well-formed and consistent with the occupancy. |
| `connector` | Declared top and bottom connectors sit at valid mating points. |
| `topology` | The part can participate in a connection graph without ambiguity. |

| Code | Meaning |
| ---: | --- |
| 0 | All gates pass. |
| 1 | Missing or invalid target. |
| 3 | Any gate fails. |

---

## Activation

**Nothing activates silently.** A validated bundle only takes effect when you name it:

```sh
legolization bundle model.vox   --catalog part_4070-legolization-support
legolization build  model.vox -o out.ldr --catalog part_4070-legolization-support
legolization analyze model.ldr  --catalog part_4070-legolization-support
```

`--catalog` is repeatable and appends to `catalog.extensions` in order. Labeled
physical estimates ride along on repeatable `--catalog-estimates PATH` sidecars.

Changes to the **built-in upstream catalog** always require explicit confirmation and
are not something a command does for you.

---

## Legacy voxel catalog extensions

Extensions declare `"schema": 2` and a `parts` list.

Rectangular bricks, plates, and tiles are simple — explicit `size`, `height_plates`,
and a measured `mass_g`:

```json
{
  "schema": 2,
  "parts": [
    {
      "key": "brick_1x10",
      "ldraw_part": "6111",
      "category": "brick",
      "size": [1, 10],
      "height_plates": 3,
      "mass_g": 2.51
    }
  ]
}
```

A **non-rectangular** part cannot be described that way and must declare its complete
geometry instead:

| Field | Meaning |
| --- | --- |
| `occupied_cells` | Every cell the part occupies (collision) |
| `filled_cells` | Cells contributing to the target shape |
| `top_connectors` | Stud positions |
| `bottom_connectors` | Anti-stud positions |
| `orientations` | Permitted yaws, reduced by symmetry |
| `origin_offset` | Local-frame offset |
| `height_plates` | Vertical extent |
| `mass_g` | Measured mass |

Extensions may not override existing keys and may not introduce an ambiguous LDraw
decode — both are validation errors.

---

## When you need this

You mostly do not. Reach for it when a build or analysis fails on an unknown part, or
when you want a specific part available to the SNOT and detail passes. Everything
here is opt-in by design: catalog changes alter physics results, so they are never
applied implicitly.
