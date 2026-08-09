# Your first model

A complete walkthrough, using the example model that ships with the project. Every
output below is a real transcript, not an illustration.

---

## 1. Ask

> Turn `heart.vox` into a LEGO model I can actually build.

Your agent matches this to [`legolize-model`](skills/legolize-model.md) and starts by
looking at the file.

---

## 2. It inspects the input first

Before spending time on the pipeline, the skill checks that the input is usable and
that nothing needs deciding:

```console
$ legolization input inspect heart.vox
heart.vox: voxel (vox)
  grid: 7x2x7   filled: 60   colours: 1   recommended plates/voxel: 3
```

A small single-colour voxel model, 60 filled cells, nothing ambiguous. The skill
proceeds without asking you anything.

!!! tip "This is where questions come from"

    On a mesh file, this step is where "which way is up?" and "this model has no
    colours — which one should I use?" surface. Both come from *conditions* the
    inspection reports. See [`prepare-lego-input`](skills/prepare-lego-input.md).

---

## 3. It runs the pipeline

```console
$ legolization bundle heart.vox --quality fast
bundle: /path/to/heart-legolization (partial)
  ingest: complete   candidates: complete   selection: complete   model: complete   bom: complete   instructions: partial
```

The default is `balanced`, which tries the placement strategies under a fifteen-minute
budget — most models finish well before it, and the slowest strategy joins in only
when a quick pre-check says the model is small enough for it. `fast` was used here
for a quick preview.

The skill will recommend `balanced` unless you asked for speed. It will never pick
`exhaustive` for you, because that tier requires a time budget only you can choose.

---

## 4. It tells you what happened

The bundle finished, produced a buildable model, and flagged something in the
instructions. Here is what the agent reads to know that:

```json title="heart-legolization/comparison/report.json"
{
  "schema": "legolization.bundle-comparison/v1",
  "winner": {"strategy": "greedy", "seed": 0, "variant": "hard"},
  "reason": "buildable, best canonical objective 0.3361 (colour error 0.0000, 12 bricks) among 1 buildable candidate(s)",
  "buildable": true
}
```

**Buildable** is the word that matters. It means all three of:

- the model stands up under the physics check,
- it is one connected piece,
- nothing is floating.

The winning candidate's numbers:

| | |
| --- | --- |
| Bricks | 12 |
| Mass | 19.64 g |
| Worst brick stress | 0.0014 — essentially at rest |
| Weakest joint headroom | 0.98 N |
| Colour error | 0.0 |

---

## 5. What landed on disk

```text
heart-legolization/
  model/model.mpd              ← the model you would build
  model/model.ldr
  bom/bom.json                 ← every part you need
  comparison/report.json       ← what was tried, and why this one won
  instructions/audit.json      ← the buildability check
  instructions/instructions.html
  instructions/instructions.pdf  ← the printable booklet
  bundle.json                  ← the record of the whole run
```

The `.html` and `.pdf` appeared because a renderer was installed. Without one, you get
everything else and the booklet is cleanly omitted rather than filled with blank pages.

---

## 6. Understanding "partial"

The run reported `partial`. That is **not** a failure — the model is buildable and
published. It means something in the instructions is worth your attention:

```json title="from bundle.json"
"instructions": {
  "status": "partial",
  "detail": {"audit_verdict": "findings", "render": "auto", "missing_steps": []}
}
```

And from the audit itself: of four build steps, step 3 carries two flags —

| Flag | Meaning |
| --- | --- |
| `oversized` | The step adds more bricks than the target step size |
| `insertion-fragile` | The partial build is stable, but *pressing that piece home* could disturb it |

Neither makes the model unbuildable. They are the difference between "this works" and
"this works and every step is comfortable". A good next move is to ask:

> Have a closer look at those build steps.

which routes to [`inspect-instructions`](skills/inspect-instructions.md).

---

## 7. See it

> Show me what it looks like.

That is [`render-ldraw`](skills/render-ldraw.md). It checks for a renderer, asks before
installing one if there is none, and then produces front, iso, and top images — which
your agent opens and describes against what you asked for.

---

## Where to go from here

| You want | Ask for | Skill |
| --- | --- | --- |
| A printable booklet | "Make me printable instructions" | [`publish-lego-instructions`](skills/publish-lego-instructions.md) |
| To know if it is really sturdy | "How solid is this? Where is it weakest?" | [`analyze-lego-assembly`](skills/analyze-lego-assembly.md) |
| Fewer parts | "Can this be done with fewer bricks?" | [`optimize-lego-build`](skills/optimize-lego-build.md) |
| To fix a problem | "This falls apart at the arch — fix it" | [`repair-lego-assembly`](skills/repair-lego-assembly.md) |

---

## If it does not work the first time

The most common outcome worth knowing about is **unbuildable** — the physics rejected
every candidate. The skill will tell you plainly, keep the best failed attempt in
`diagnostics/` so you can look at it, and then **ask** before trying the material
ladder, because that step needs a time budget from you.

More: [Troubleshooting](troubleshooting.md).

---

## Working from a mesh instead

The same flow, with one extra decision. Most `.obj` files are **y-up** while the tool
defaults to z-up, so inspection often reports an ambiguous up-axis, and the skill asks:

> This looks like it should be Y-up — should I use that? Also, how wide should the
> brick version be? At 24 studs across it would use roughly a few hundred parts.

Answer those two and the rest is identical. You can also settle them once and reuse the
answer — see [`prepare-lego-input`](skills/prepare-lego-input.md).

!!! warning "Meshes take much longer"

    Small voxel models take seconds. A mesh at 28–36 studs can take **tens of minutes
    per strategy**. Ask your agent to run it in the background, and expect silence
    until it finishes.
