# `prepare-lego-input`

**Check a source model before generating from it.**

Answers the questions that decide whether a run will produce what you expected —
before you spend the time.

---

## Say something like

> Is spot.obj oriented right, and how big should the brick version be?

> Will this file even work?

> What colours are in this model?

> This came out upside down — fix the orientation.

**Accepts:** `.vox`, `.npy`, `.obj`, `.stl`, `.ply`.

---

## What it does

Reads your file and reports what it found: the kind of input, the grid dimensions, how
many cells are filled, which way is up, a recommended size, and a colour summary.

Then it works through anything that needs deciding, and optionally writes a
**prepared bundle** that pins those decisions.

---

## What it will ask you

The inspection reports *conditions*. Each has a specific response:

| Condition | What it asks / does |
| --- | --- |
| **Ambiguous up-axis** | Asks which way is up, recommending the classifier's guess. Most `.obj` files are **y-up**. |
| **No colour data** | Asks for one colour to use throughout. |
| **Multiple components** | Reports it plainly. **Never drops or merges them** — that would silently change your model. |
| **Not watertight** | Reports it. The fill step may behave unexpectedly on a shell mesh. |
| **Empty grid** | **Stops.** There is nothing to build. |

!!! important "Conditions are not failures"

    Inspection succeeds and *lists* them. A model with three disconnected components is
    a perfectly valid thing to inspect — you just need to know, because it will be
    reported as three pieces later too.

---

## What you get

Inspection alone just reports. If you ask it to write the result:

```
spot-prepared/
  normalized.npy     the target, normalized
  normalized.json    orientation, scale, and colour decisions, with your source's hash
  bundle.json
```

Hand that directory to [`legolize-model`](legolize-model.md) and generation reproduces
exactly — the orientation and size cannot be re-guessed differently on a later run.

That reproducibility is the whole reason this skill exists as a separate step.

---

## Sizing

Two ways to decide how big the brick version will be:

| Approach | When |
| --- | --- |
| **Pick a width** — "make it 24 studs across" | You know what you want |
| **Give a range** — "somewhere between 16 and 32 studs" | You want the best fit chosen for you |

Either way, the number is the **footprint width**, not the height. A tall shape at 24
studs wide can be a hundred plate layers high.

!!! warning "Size drives runtime hard"

    Doubling the studs across does far more than double the work. If you are unsure,
    start small.

---

## When it stops

| Result | Meaning |
| --- | --- |
| **complete** | Inspection finished — including when conditions were reported |
| **error** | The file could not be read, or the format is unsupported |

There is no failure verdict here. Inspection has nothing to fail at.

---

## Where it hands off

→ [`legolize-model`](legolize-model.md), either with the prepared directory or with the
settled orientation and size.
