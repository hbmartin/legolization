# `render-ldraw`

**Render a model to images so you can see it.**

---

## Say something like

> Show me what heart.ldr looks like from the front and the top.
>
> What does this look like?
>
> Preview the model.
>
> Did that change actually look right?

**Accepts:** `.ldr`, `.mpd`, or a bundle directory.

---

## What it does

1. **Checks for a renderer.** Free, no side effects.
2. **If none is found**, explains the platform-appropriate install and
   **asks before doing anything**.
3. **Renders** front, iso, and top views.
4. **Opens the images and describes them** against what you asked for.

That last step matters. The skill does not just tell you files exist — it looks at them.

---

## Setting up a renderer

| Platform | Renderer | Installed with |
| --- | --- | --- |
| macOS | LDView | Homebrew |
| Windows | LeoCAD | winget |
| Ubuntu/Debian | LeoCAD + Xvfb | apt |

!!! warning "This one always asks"

    Installing software on your machine is not something a skill does unasked. It will
    also never improvise a different renderer than the one for your platform.

---

## What it checks in the images

- the silhouette matches what you started from,
- no unexpected holes or pits,
- colours are right,
- vertical seams are **staggered**, not stacked in tall aligned columns,
- nothing is floating off on its own,
- slopes step gently rather than terracing.

Staircase edges on curved surfaces are expected — that is what bricks do. Gross
terracing is not.

---

## Your existing renders are safe

Images are **never overwritten**. A new render gets a numbered name, and previous
renders are kept across renderer and parts-library changes — so you can compare how a
model looked before and after a change.

---

## When it stops

| Result | Meaning |
| --- | --- |
| **complete** | Every requested view rendered |
| **partial** | Some views rendered — it will say which |
| **error** | Nothing rendered |

!!! note "Success means files on disk"

    Headless renderers sometimes exit successfully while writing nothing. The skill
    judges by the images, not by the exit code, and it will not describe an image it
    has not opened.

---

## Where it fits

Called by [`legolize-model`](legolize-model.md),
[`optimize-lego-build`](optimize-lego-build.md),
[`repair-lego-assembly`](repair-lego-assembly.md), and
[`eval-corpus`](eval-corpus.md) whenever seeing the result is the point.

[`publish-lego-instructions`](publish-lego-instructions.md) borrows this skill's
consent-gated installer when you want a booklet with pictures.

If something looks structurally wrong rather than cosmetically wrong, hand it to
[`analyze-lego-assembly`](analyze-lego-assembly.md).
