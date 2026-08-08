# `model` and `instructions`

Two commands that operate on what generation produced: render it, and audit its build
steps.

---

## `model render`

Render a model or bundle to PNG images from requested views.

```
legolization model render [--views NAMES] [--size PIXELS] [-o DIR] [--json] input
```

| Flag | Type | Default | Effect |
| --- | --- | --- | --- |
| `input` | path | required | `.ldr`/`.mpd` model **or** a bundle directory. |
| `--views NAMES` | comma list | all | From `front`, `iso`, `top`. |
| `--size PIXELS` | int | `800` | Image **width**; height is 3/4 of the width. |
| `-o`, `--output DIR` | path | beside the model | Image directory. |
| `--json` | flag | off | Single envelope on stdout. |

Given a bundle directory, the primary model is resolved through `bundle.json`, with
`.mpd` preferred over `.ldr`.

```sh
legolization model render heart-legolization
legolization model render model.mpd --views front,iso --size 1024
legolization model render model.ldr --views iso -o renders/
```

Alongside the images it writes `<stem>.render-info.json` recording what was rendered
and how.

!!! success "Renders are never overwritten"

    Existing images get a numeric suffix rather than being replaced, and prior
    renders are retained across renderer or parts-library changes — so you can diff
    how a model looked before and after a code change.

!!! warning "A PNG on disk is the only success signal"

    Renderers are external programs invoked headlessly, and some of them exit 0 while
    writing nothing. Judge the result by the files, not the exit code — which is what
    the exit codes below encode.

| Code | Meaning |
| ---: | --- |
| 0 | Every requested view rendered. |
| 1 | Zero views rendered. |
| 3 | Some views rendered — the envelope says which. |

Installing a renderer: [Rendering and parts](../rendering-and-parts.md).

---

## `instructions audit`

Audit step ordering, stability, and insertion pressure.

```
legolization instructions audit [--report PATH] [--render-dir DIR] [--json] input
```

| Flag | Type | Default | Effect |
| --- | --- | --- | --- |
| `input` | path | required | A **step-annotated** `.ldr`/`.mpd`, or a bundle directory. |
| `--report PATH` | path | `<bundle>/instructions/audit.json`, or `<stem>.audit.json` | Report destination. |
| `--render-dir DIR` | path | none | Also render per-step PNGs for visual inspection. |
| `--json` | flag | off | Single envelope on stdout. |

This is the check that answers *"will a human actually be able to build this?"* It
re-derives every step's after-state and applies the plan invariants plus a cold
physics solve per step.

### What each step reports

| Field | Meaning |
| --- | --- |
| `size` | Bricks added in this step |
| `prefix_stable` | Does everything built so far stand on its own? |
| `prefix_max_score` | Worst per-brick stress in the prefix (≥ 1.0 means at or over capacity) |
| `floating_after` | Bricks with no stud path to ground after this step |
| `components_after` | Disconnected pieces after this step |
| `flags` | See below |

### Flags

| Flag | Meaning | Is it a bug? |
| --- | --- | --- |
| `floating` | Something placed this step has no route to ground yet | Sometimes legitimate — islands that join later. Must be warned. |
| `unstable-prefix` | The partial build does not stand | One, warned, on a genuinely hard shape is acceptable. Several mean the rescue paths failed. |
| `insertion-fragile` | Statically fine, but pressing the piece home collapses the prefix | Worth fixing; usually a sequencing choice. |
| `oversized` | Step exceeds `max_step_size` | Chunking regression. |
| **`violations`** | A plan invariant is broken | **Always a bug.** Verdict `infeasible`, exit 2. |

### Verdicts and exit codes

| Verdict | Code | Meaning |
| --- | ---: | --- |
| `certified` | 0 | Every invariant holds and every prefix stands. |
| `findings` | 3 | Warnings worth reading; the plan is still buildable. |
| `infeasible` | 2 | The final step leaves more than one component or something floating, or an invariant is violated. |
| — | 1 | Operational error. |

```sh
legolization instructions audit heart-legolization
legolization instructions audit model.mpd --report audit.json --render-dir steps/
```

### Reading the step images

With `--render-dir`, one PNG per step lands in the directory. For each image, check:

- new bricks rest on existing structure or on the ground — nothing hovers unless the
  step is flagged;
- the step is one coherent spatial region, not scattered singles;
- straight-down insertion is plausible;
- the view rotation between consecutive steps is not disorienting.

For a whole-model visual check instead of a per-step one, use `model render`.

!!! note "Insertion-press auditing is always on"

    It cannot be disabled here. The audit presses each step's new bricks home with a
    virtual mass (default 1 kg, `instructions.options.insertion_mass_kg`) and reports
    prefixes that survive statically but collapse under that press.
