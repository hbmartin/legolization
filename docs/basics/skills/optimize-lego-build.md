# `optimize-lego-build`

**Improve an existing brick assembly.**

Takes an `.ldr` or `.mpd` you already have and either republishes it exactly as built,
or rebuilds its brickwork from scratch keeping the shape and colours.

---

## Say something like

> Can you rebuild castle.ldr with fewer bricks without making it fragile?

> Optimize this model.

> Strengthen this build.

> Compare some different ways of building this shape.

**Accepts:** `.ldr`, `.mpd`.

---

## The one question it will ask

**Who owns the shape?**

| Mode | What happens | Directory |
| --- | --- | --- |
| **Preserve** *(default)* | Your brickwork is kept exactly. It is validated and republished with instructions. | `castle-instructions/` |
| **Retile** | The brickwork is regenerated from the shape. Same silhouette, same colours, different bricks. | `castle-optimized/` |

It recommends **retile** when you said *optimize*, *strengthen*, *shrink*, or *fewer
parts* — because preserve cannot change any of those.

It recommends **preserve** when you just want instructions for what you built.

---

## What you get

For a retile, a full comparison of every candidate rebuild:

```
castle-optimized/
  model/model.mpd          the winning rebuild
  bom/bom.json
  comparison/report.json   every candidate, with brick count and stability
  instructions/
```

For each candidate, one line: which strategy, whether it is buildable, how many bricks,
and its overall score.

---

## It will tell you when it lost

!!! success "Losing a comparison is a valid answer"

    If the retiled rebuild is **not actually better** than what you had — more bricks,
    or weaker — the skill says so plainly.

    This is the point of the comparison. A skill that always reported an improvement
    would be useless.

---

## Your file is never touched

Everything is written into a new directory beside your input. Your original `.ldr`
stays exactly as you left it.

---

## When it stops

| Result | Meaning |
| --- | --- |
| **complete** | Done — model published |
| **partial** | Done, with a warning worth reading |
| **unbuildable** | No rebuild passed the physics gate. It will offer the material ladder, with your consent and a time budget. |
| **interrupted** | Saved its place; re-running continues |

---

## Where it hands off

- **See it** → [`render-ldraw`](render-ldraw.md)
- **Build from it** → [`publish-lego-instructions`](publish-lego-instructions.md)
- **Understand a weakness** → [`analyze-lego-assembly`](analyze-lego-assembly.md)

!!! note "Not for making a model from a mesh"

    That is [`legolize-model`](legolize-model.md). This skill starts from bricks.

---

??? info "Advanced controls (only if you ask)"

    Quality tiers apply to retile only — there is nothing to sweep when preserving.
    Drafted parts from [`extend-lego-part-support`](extend-lego-part-support.md) can be
    activated here too. See [`bundle`](../../guide/cli/bundle.md).
