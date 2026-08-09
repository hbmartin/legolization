# Bundles and artifacts

Every operation writes a self-describing directory rather than loose files. This page
documents the layouts and the JSON schemas you will actually open.

---

## Sibling naming

| Flavour | Directory | Produced by |
| --- | --- | --- |
| `legolization` | `<stem>-legolization` | `bundle` on a native input; also the retry bundle |
| `prepared` | `<stem>-prepared` | `input inspect --write` |
| `optimized` | `<stem>-optimized` | `bundle --retile` on `.ldr`/`.mpd` |
| `instructions` | `<stem>-instructions` | `bundle` on `.ldr`/`.mpd` without `--retile` |
| `analysis` | `<stem>-analysis` | `analyze` |
| `repair` | `<stem>-repair` | `analyze --repair` |
| `support` | `<key>-legolization-support` | `catalog infer` / `catalog validate` |

Collisions are **never** overwritten. A directory already holding different work
causes a numeric sibling — `model-legolization-2`, `-3`, … — to be used instead.

The corpus harness is the one exception to the sibling convention: it writes to
`./legolization-eval/runs/`, relative to the current directory.

---

## `bundle.json`

The authoritative record, validated against a packaged JSON Schema on **both read and
write**. A schema violation is an error, not a warning.

```jsonc
{
  "schema": "legolization.bundle/v1",
  "version": "0.6.0",

  "identity": {
    "input_sha256": "…",          // the four things that define this bundle
    "config_sha256": "…",
    "legolization_version": "0.6.0",
    "catalog_sha256": "…"
  },

  "source":   { "filename": "model.obj", "sha256": "…", "relative_path": "../model.obj" },
  "configuration": { "sha256": "…", "values": { /* effective config, minus cache+output */ } },
  "versions": { "legolization": "…", "python": "…",
                "libraries": {"numpy": "…", "scipy": "…", "trimesh": "…", "pyldraw3": "…"},
                "catalog_sha256": "…" },

  "quality": "balanced",
  "status":  "complete",          // in-progress | complete | partial | unbuildable
                                  // | error | cancelled | interrupted
  "exit_code": 0,

  "stages": {
    "<name>": { "status": "complete",   // pending | running | complete | partial
                                        // | failed | skipped | cancelled | interrupted
                "warnings": [], "artifacts": [], "error": null, "detail": {} }
  },

  "artifacts": [
    {"path": "model/model.mpd", "stage": "model", "kind": "model", "sha256": "…"}
  ],
  "warnings": [],
  "verdicts": { "buildable": true, "stable": true, "provisional": false,
                "winner": {"strategy": "bond", "seed": 0, "variant": "hard"} },
  "pending":  [ {"candidate_key": "luo-s0-soft", "pid": 12345, "status": "running"} ]
}
```

Three properties are worth knowing:

- **No absolute source path.** A relative path is recorded only when the source is
  within two `..` ascents of the bundle; otherwise just the filename and hash. Bundles
  stay portable.
- **Atomic per-stage rewrites.** The file is always internally consistent, even if a
  run is killed mid-stage.
- **Corruption is not fatal.** A `bundle.json` that fails to parse is treated as
  absent, so a damaged bundle re-runs rather than crashing.

### Stage details

| Stage | `detail` payload |
| --- | --- |
| `ingest` | `{format, mode, filled_count, shape}` — or `{shape_authority, brick_count}` for an imported assembly |
| `candidates` | `{quality, candidate_count, time_budget_s, exact_included, exact_skip_reason, variants, collapsed_variants}` |
| `selection` | `{reason, pending}` |
| `instructions` | `{render, audit_verdict, booklet, missing_steps}` |

`exact_skip_reason` is where you find out *why* global exact placement was not tried.

---

## Generation bundle

```text
model-legolization/
  bundle.json
  bundle.lock                     single-writer file lock
  work/pending/<candidate-key>/   detached worker scratch
      job.json  stamp.json  stamp.lock  result.json  model.ldr  log.txt
  comparison/report.json          every candidate, scored
  model/model.mpd                 the winner
  model/model.ldr                 the same model, flat
  bom/bom.json
  instructions/audit.json         always
  instructions/instructions.html  only when a renderer was available
  instructions/instructions.pdf
  diagnostics/best-rejected.ldr   only when nothing was buildable
  diagnostics/best-rejected.json
```

Candidate keys are `<strategy>-s<seed>-<variant>` — for example `bond-s0-hard`.

Stages run in order `ingest → candidates → selection → model → bom → instructions`
when sweeping, or `ingest → generate → model → bom → instructions` for
`--quality direct`. Resume skips a completed stage whose recorded artifact hashes
still match; a drifted artifact re-runs only its producing stage and records
`"regenerated": true`.

### Retry bundle

`--retry-materials` nests a complete bundle per rung and promotes the winner:

```text
model-legolization/
  bundle.json
  rungs/four-plate/   … complete nested bundle
  rungs/six-plate/
  rungs/solid/
  model/              promoted from the first buildable rung
  bom/
```

---

## `comparison/report.json`

```jsonc
{
  "schema": "legolization.bundle-comparison/v1",
  "winner": {"strategy": "bond", "seed": 0, "variant": "hard"},
  "reason": "buildable, best canonical objective 0.1234 (colour error 0.0, 118 bricks) among 5 buildable candidate(s)",
  "buildable": true,
  "candidates": [
    {
      "strategy": "bond", "seed": 0, "variant": "hard",
      "status": "complete", "seconds": 41.2, "error": null,
      "selection_objective": 0.1234,
      "cross_colour_error": 0.0,
      "metrics": { "brick_count": 118, "mass_g": 143.7, "stable": true,
                   "max_score": 0.31, "maximin_capacity": 0.62,
                   "component_count": 1, "floating_count": 0, "…": "…" }
    }
  ]
}
```

Every candidate is scored against the **canonical hard, no-dither reference grid**, so
a dithered candidate cannot win by grading itself against the softer target it
created.

Reading the metrics:

| Metric | Read as |
| --- | --- |
| `max_score` | Worst per-brick stress. `0` is effortless; `≥ 1.0` means a joint at or over capacity. `0.7–1.0` is standing-but-fragile. |
| `maximin_capacity` | Extra force (N) the weakest joint pair can still absorb. Higher is sturdier — the best way to compare two buildable layouts. |
| `selection_objective` | The weighted objective. Lower is better, **comparable only within one input**. |

!!! tip "`max_score` exactly 1.0 on every strategy usually means toppling"

    That is a *global* verdict — centre of mass outside the support polygon — not a
    joint problem. The shape needs a wider base; no placement strategy can fix it.

---

## `bom/bom.json`

```jsonc
{
  "model": "model",
  "brick_count": 118,
  "mass_g": 143.700,
  "total": [
    {"part_key": "brick_2x4", "ldraw_part": "3001", "colour_code": 4,
     "colour_name": "Red", "quantity": 12, "mass_g": 2.34}
  ],
  "steps": [
    {"step": 1, "brick_count": 7, "parts": [ … ]}
  ]
}
```

!!! note "`steps` is empty in the bundle pipeline"

    The bundle writes the BOM without a plan attached, so only `total` is populated
    there. The per-step view is produced by `instructions audit` and by the booklet's
    per-step callouts.

Masses come from BrickLink's published gram weights, ingested into the catalog.

---

## `instructions/audit.json`

```jsonc
{
  "schema": "legolization.instructions-audit/v1",
  "input": {"filename": "model.mpd", "sha256": "…", "brick_count": 118,
            "step_count": 24, "has_explicit_steps": true},
  "target_step_size": 7,
  "certification": {"valid": true, "violations": [],
                    "cold_prefix_count": 24, "earliest_failure": null},
  "steps": [
    {"index": 0, "size": 6, "prefix_stable": true, "prefix_max_score": 0.02,
     "components_after": 1, "floating_after": 0, "flags": []}
  ],
  "verdict": "certified",
  "render_warnings": []
}
```

`verdict` is `certified`, `findings`, or `infeasible`. `infeasible` means the final
step leaves more than one component or something floating — see
[`instructions audit`](cli/model-and-instructions.md#instructions-audit).

---

## Prepared bundle

```text
model-prepared/
  bundle.json
  normalized.npy       int16 colour codes on the plate lattice
  normalized.json      schema: legolization.input-normalized/v1
```

The sidecar records `source{filename, sha256}`, `npy_sha256`, `scale`,
`orientation{up, confidence}`, `colours{mode, summary}`, `warnings`, and `conditions`.
Resolving the bundle requires the `.npy` hash to match, so a corrupted target is
caught rather than silently used.

## Analysis bundle

```text
model-analysis/
  bundle.json
  report.json
  analysis.html
  renders/
  diagnostics/connections.json    the connection graph
  diagnostics/components.mpd      each connected component, separated
  diagnostics/floating.mpd        every brick with no route to ground
  diagnostics/callouts/           missing-connector callouts
```

`components.mpd` and `floating.mpd` are the fastest way to *see* a connectivity
problem — open them in a viewer rather than reading JSON.

## Repair bundle

```text
model-repair/
  bundle.json
  repair.json                      schema: legolization.repair/v1
  analysis/before.json
  analysis/after.json
  model/model.repaired.mpd         the fix
  diagnostics/best-rejected.mpd    retained when no fix validated
  diagnostics/best-rejected.json
```

A rejected candidate is retained for diagnosis. It is not a fix and must not be
presented as one.

## Catalog support bundle

```text
part_4070-legolization-support/
  bundle.json
  catalog-extension.json
  draft-estimates.json
  sources.json
  validation.json
  geometry/4070.dat
  geometry/<occupancy>.json
```

See [`catalog`](cli/catalog.md).

---

## Assembly manifests

Separately from bundles, `build` and `analyze` write a
`legolization.assembly-manifest` version 1 sidecar (default
`<output>.manifest.json` / `<input-stem>.manifest.json`).

Required sections: `status`, `source`, `configuration`, `coordinate_system`, `parts`,
`occurrences`, `contacts`, `action_graph`, `support`, `load_cases`, `stability`,
`capabilities`, `exactness`. Optional: `templates`, `bom`, `artifacts`, `warnings`,
`errors`.

Canonical JSON contains **no wall-clock timestamp**, so identical inputs produce
byte-identical manifests. That is what makes
[`legolization validate --against`](cli/parts-cache-validate.md) a real
reproducibility check.

---

## Worker artifacts

Under `work/pending/<candidate-key>/`, each detached worker keeps a `stamp.json`
(`legolization.worker/v1`) and, on success, a `result.json`
(`legolization.candidate/v1`).

Liveness is a **held file lock** on `stamp.lock`, never a bare PID probe — a recycled
PID cannot be mistaken for a live worker. `bundle --cancel-pending` sends `SIGTERM`,
then `SIGKILL` after a five-second grace, and only ever to identity-matched workers
of that bundle.
