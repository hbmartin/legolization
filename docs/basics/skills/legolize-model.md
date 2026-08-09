# `legolize-model`

**Turn a 3D model into a physically stable brick-built model.**

This is the main event: mesh or voxel file in, a complete buildable bundle out.

---

## Say something like

> Turn dragon.stl into a LEGO model I can actually build.
>
> Brickify this shape.
>
> Make a buildable brick version of spot.obj.

It also triggers on *legolize*, *Lego-ify*, *convert this mesh to bricks*, and *run the
whole pipeline*.

**Accepts:** `.vox`, `.npy`, `.obj`, `.stl`, `.ply` — or a `-prepared` directory from
[`prepare-lego-input`](prepare-lego-input.md).

---

## What it does

1. **Refreshes the parts library** — silently, no consent needed.
2. **Inspects your input** and acts on what it finds. This is where questions come
   from.
3. **Runs the pipeline** at a quality tier, trying multiple placement strategies in
   parallel and picking the winner on evidence.
4. **Reports back**: the bundle path, which strategy won and why, any warnings, and one
   suggested next step.

---

## What you get

```text
dragon-legolization/
  model/model.mpd            the model you would build
  bom/bom.json               every part you need
  comparison/report.json     what was tried, and why this one won
  instructions/              build steps; a booklet too, if a renderer is set up
  bundle.json                the record of the run
```

Everything is physics-validated. A model that could not stand up is never published
here — it goes to `diagnostics/` with an explanation.

---

## What it will ask you

| Situation | Question |
| --- | --- |
| The up-axis is ambiguous | Which way is up? It recommends the classifier's pick. |
| Your mesh has no colour data | Which single colour should it use? |
| You asked for `exhaustive` quality | How long may it run? **It will not invent this number.** |
| The result was unbuildable | May it try the material ladder, and for how long? |

Anything else it decides itself and tells you about.

### The quality tiers

| Tier | Time | What it does |
| --- | --- | --- |
| `fast` | ~2 min | One strategy — a quick preview |
| **`balanced`** | ~15 min | Every strategy plus exact placement when it qualifies. **Recommended, and the default.** |
| `exhaustive` | you decide | Everything, three times over with different seeds |

---

## When it stops

| Result | What it means |
| --- | --- |
| **complete** | Done. Buildable model published. |
| **partial** | Done, with something worth reading — an instruction warning, a missing booklet, or a time limit reached. Still buildable. |
| **unbuildable** | Physics rejected every candidate. Nothing published; the best failed attempt is kept in `diagnostics/`. It will offer the retry ladder — and ask first. |
| **interrupted** | Stopped part-way, saved its place. Re-running continues. |
| **error** | Installation failed, or the input could not be read. It reports and stops rather than continuing broken. |

---

## Where it hands off

- **See it** → [`render-ldraw`](render-ldraw.md)
- **Build from it** → [`publish-lego-instructions`](publish-lego-instructions.md)
- **Check how solid it is** → [`analyze-lego-assembly`](analyze-lego-assembly.md)

And it accepts handoffs *from* [`prepare-lego-input`](prepare-lego-input.md) (a
prepared bundle) and [`extend-lego-part-support`](extend-lego-part-support.md) (a
drafted part).

!!! note "Not for existing brick models"

    If you already have an `.ldr` or `.mpd`, you want
    [`optimize-lego-build`](optimize-lego-build.md). That boundary is drawn on
    purpose.

---

## Re-running

Re-running the same request **resumes** rather than starting over — bundle identity is
your input's content, the settings, the software version, and the parts catalog. Ask
explicitly if you want a from-scratch rerun.

---

??? info "Advanced controls (only if you ask)"

    - `-o DIR` — put the bundle somewhere other than beside your input
    - `--render auto|required|off` — booklet rendering policy
    - `--cancel-pending` — stop background workers, keep what finished
    - `--config` / `--set` — tune any generation setting
    - `--catalog` / `--catalog-estimates` — activate a drafted part

    All of these are covered in [`bundle`](../../guide/cli/bundle.md).
