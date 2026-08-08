# `repair-lego-assembly`

**Search for a validated fix to an unstable or unbuildable assembly.**

Where [`analyze`](analyze-lego-assembly.md) tells you what is wrong, this tries to make
it right.

---

## Say something like

> This model falls apart at the arch — fix it without changing how it looks.

> Repair this build.

> Reinforce the weak part.

> Analysis found problems — can you solve them?

**Accepts:** `.ldr`, `.mpd`.

---

## What it does

Tries **one change at a time** to the source model: rotations, reflections, and small
translations of existing parts. Each candidate is validated against the full physics
model before it can be called a fix.

The search is **BOM-preserving** — it never suggests parts you do not already have.
That is what makes a suggestion actionable rather than aspirational.

!!! important "A suggestion must improve real connections"

    Moving two parts so their bounding boxes overlap is not a repair. A candidate only
    counts if it improves genuine connector topology — actual studs meeting actual
    anti-studs.

---

## The effort ladder

| Effort | Time | When |
| --- | --- | --- |
| **fast** | 60 s | Where it starts |
| **balanced** | 300 s | If fast found nothing — it asks first |
| **exhaustive** | you decide | You supply the budget |

It works up the ladder rather than starting at the top, and asks before each escalation.

---

## What you get

```
model-repair/
  model/model.repaired.mpd      the fix
  analysis/before.json          what was wrong
  analysis/after.json           what changed
  repair.json
  diagnostics/best-rejected.mpd only when no fix was found
```

---

## Your file is never overwritten

The fix is written to a **new file**. Your original stays exactly as you left it, so you
can compare, revert, or ignore the suggestion entirely.

---

## When no fix is found

!!! danger "A rejected candidate is never presented as a fix"

    If the search was exhausted without finding a validated repair, the skill says so
    and explains why.

    It keeps the closest attempt for you to look at — **explicitly labelled as not
    buildable**. That is diagnostic material, not a solution, and it will always be
    described that way.

---

## When it stops

| Result | Meaning |
| --- | --- |
| **complete** | A validated fix was found and written — or nothing needed fixing |
| **unbuildable** | The search was exhausted. Reason given; best attempt retained and labelled |
| **partial** | The search timed out. More budget might find something |
| **error** | Invalid input or a runtime problem |

---

## Where it hands off

- **See the fix** → [`render-ldraw`](render-ldraw.md)
- **Build it** → [`publish-lego-instructions`](publish-lego-instructions.md)
- Or escalate the effort, with your consent.

---

??? info "Advanced controls (only if you ask)"

    The repaired **model file** and the repair **bundle directory** are separate
    settings. Support mode, load scenarios, seed, and catalog extensions all apply here
    too — see [`analyze`](../../guide/cli/analyze.md).
