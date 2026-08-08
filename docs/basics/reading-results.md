# Reading the results

Every operation produces a directory beside your input and a short verdict. This page
explains both.

---

## The bundle

```
dragon-legolization/
  model/model.mpd            the model you would build
  model/model.ldr            the same model, flattened
  bom/bom.json               every part, with quantities and colours
  comparison/report.json     what was tried, and why this one won
  instructions/audit.json    the buildability check
  instructions/*.html *.pdf  the printable booklet, when a renderer exists
  bundle.json                the record of the whole run
  diagnostics/               only when nothing worked — the best failed attempt
```

Open `model/model.mpd` in [LDView](https://tcobbs.github.io/ldview/) or
[BrickLink Studio](https://www.bricklink.com/v3/studio/download.page). Open
`instructions/instructions.pdf` to build from.

The directory is **portable**. Move it, archive it, email it. It records no absolute
paths, and every skill accepts a bundle directory anywhere it accepts a model.

### Directory names tell you what happened

| Suffix | Produced by |
| --- | --- |
| `-legolization` | Generating a model from a mesh or voxel file |
| `-prepared` | Normalizing an input before generation |
| `-optimized` | Rebuilding an existing assembly's brickwork |
| `-instructions` | Publishing instructions for an assembly, kept as-is |
| `-analysis` | Diagnosing an assembly |
| `-repair` | Searching for a fix |
| `-legolization-support` | Drafting support for an unknown part |

A directory already holding different work is never overwritten — you get
`dragon-legolization-2` instead.

---

## Buildable

The single most important word. It means **all three** of:

| | |
| --- | --- |
| **Stable** | It stands up under the physics model — every brick balances, and no joint is asked to hold more than it can |
| **One piece** | The whole model is connected through studs |
| **Nothing floating** | Every brick has a path down to the ground |

This is a hard gate. A model that fails any of the three is never published as the
answer — it is kept under `diagnostics/` so you can look at it, and the run reports a
failure.

!!! note "Two towers on one baseplate are two pieces"

    Resting on the same ground does not count as being connected. If your source shape
    is several separate islands, expect "multiple components" — that is a correct
    description of the shape, not a bug.

---

## The verdict words

| Word | What it means for you |
| --- | --- |
| **complete** | It worked. |
| **partial** | It worked, and something is worth reading. See below. |
| **unbuildable** | The physics rejected every attempt. Nothing was published. |
| **error** | Something went wrong — a bad file, a missing dependency. |
| **interrupted** | Stopped part-way, and it saved its place. Re-running continues from there. |

### "Partial" is usually fine

`partial` most often means one of:

- the instruction audit found something worth flagging (the model is still buildable),
- there was no renderer and you asked for a booklet anyway,
- a few booklet steps failed to render — and they are **explicitly marked** in the
  booklet rather than silently blank,
- the run hit its time limit and published the best result it had, while background
  work continues. Re-running may pick up a better one.

Your agent will say which.

---

## The numbers worth knowing

From `comparison/report.json`:

| Number | Read it as |
| --- | --- |
| **Bricks** | Part count. |
| **Mass** | Grams, from real published part weights. |
| **Worst brick stress** | 0 means at rest. Under 0.7 is comfortable. 0.7–1.0 is standing but fragile. 1.0 means at or beyond capacity. |
| **Weakest joint headroom** | Extra force (newtons) the weakest connection could still take. **Higher is sturdier** — the best number for comparing two working models. |
| **Colour error** | How much of the surface is the wrong colour. 0 is exact. |
| **Objective** | The overall score used to pick the winner. Lower is better — but it is **only meaningful between candidates for the same model**. Never compare it across two different models. |

!!! tip "If every attempt scores exactly 1.0, the model is toppling"

    That is a whole-model verdict — the centre of mass is outside the base — not a
    problem with any one joint. No amount of rebuilding fixes it; the shape needs a
    wider base.

---

## The build steps

`instructions/audit.json` scores the plan. Flags you might see:

| Flag | Meaning | Should you worry? |
| --- | --- | --- |
| `floating` | Something placed in this step has nothing under it yet | Sometimes fine — islands that join up later. It must be warned about, and it is. |
| `unstable-prefix` | The partial build does not stand on its own at this point | One, on a genuinely hard shape, is acceptable. Several mean the sequencer struggled. |
| `insertion-fragile` | Stable, but *pressing the piece home* could disturb it | Worth a look. Usually a sequencing choice, not a design flaw. |
| `oversized` | The step adds more pieces than the target | Cosmetic — a chunkier step than intended. |
| **violations** | A rule of the plan is broken | **Always a bug.** Please report it. |

The overall verdict is `certified` (all good), `findings` (warnings worth reading), or
`infeasible` (the plan does not work — a bug).

---

## Why re-running is cheap

Re-running the same request **resumes** rather than starting over.

A bundle is identified by four things: your input file's contents, the settings used,
the software version, and the parts catalog version. Not timestamps, not file paths. So
an identical request finds the existing work and continues it; a *changed* request
correctly gets a fresh directory instead of quietly reusing the old answer.

Ask for a from-scratch rerun explicitly if you want one.

---

## When nothing worked

An unbuildable result still gives you something:

```
dragon-legolization/
  diagnostics/best-rejected.ldr    the closest attempt
  diagnostics/best-rejected.json   why it failed
```

The retained model is chosen for **diagnosis, not shipping** — fewest disconnected
pieces, then lowest stress. Open it and you will usually see the problem immediately:
a thin neck, an unsupported overhang, a shape that cannot stand.

The usual next step is more material. Your agent will offer the retry ladder — thicker
shell, then thicker still, then solid — and will ask for a time budget before running
it. See [Troubleshooting](troubleshooting.md).

---

## The record of what ran

`bundle.json` holds the full provenance: your source file's name and hash, every
setting used, the software and library versions, every output file with its hash,
per-stage status, and the final verdicts.

It is the answer to "what exactly produced this?" six months from now — and it is why
a bundle can be handed to someone else and still be meaningful.

For the complete schema, see
[Bundles and artifacts](../guide/bundles-and-artifacts.md).
