# Basics

This track is for using Legolization **through a coding agent**. You describe what you
want in ordinary language; the agent runs the right commands and reports back.

You will not need to type a command line. Where a page mentions one, it is so you can
recognize what your agent is doing — not because you have to run it.

---

## What a skill is

A **skill** is a set of instructions your coding agent loads when your request matches
it. Legolization ships ten of them, one per job. Installed together, they give your
agent a working understanding of the whole tool: which command fits your request, what
to check first, what to ask you before acting, and how to read the result.

```sh
npx skills add hbmartin/legolization --all
```

That is the only setup step. The skills install or upgrade the `legolization` CLI
themselves when they need to.

---

## Start here

<div class="grid cards" markdown>

-   :material-download: **[Install the skills](install.md)**

    ---

    One command, plus what happens the first time a skill runs.

-   :material-play: **[Your first model](first-model.md)**

    ---

    A complete walkthrough from a file to something you could build, with the real
    output at each step.

-   :material-sign-direction: **[Choosing a skill](choosing-a-skill.md)**

    ---

    Which skill owns your job, how they hand off to each other, and what each will ask
    permission for.

-   :material-file-document: **[Reading the results](reading-results.md)**

    ---

    What lands on disk, what "buildable" means, and how to tell a warning from a
    failure.

-   :material-wrench: **[Troubleshooting](troubleshooting.md)**

    ---

    Symptom to fix.

</div>

---

## The ten skills

| Skill | Say something like |
| --- | --- |
| [`legolize-model`](skills/legolize-model.md) | "Turn dragon.stl into a LEGO model I can actually build." |
| [`prepare-lego-input`](skills/prepare-lego-input.md) | "Is spot.obj oriented right, and how big should the brick version be?" |
| [`optimize-lego-build`](skills/optimize-lego-build.md) | "Can you rebuild castle.ldr with fewer bricks without making it fragile?" |
| [`publish-lego-instructions`](skills/publish-lego-instructions.md) | "Make a printable instruction booklet for my mushroom model." |
| [`analyze-lego-assembly`](skills/analyze-lego-assembly.md) | "Why does my spaceship model feel wobbly at the wings?" |
| [`repair-lego-assembly`](skills/repair-lego-assembly.md) | "This model falls apart at the arch — fix it without changing how it looks." |
| [`inspect-instructions`](skills/inspect-instructions.md) | "Do these build steps make sense, or will something fall off halfway through?" |
| [`render-ldraw`](skills/render-ldraw.md) | "Show me what heart.ldr looks like from the front and the top." |
| [`extend-lego-part-support`](skills/extend-lego-part-support.md) | "Add support for part 4070 so my headlight-brick model analyzes correctly." |
| [`eval-corpus`](skills/eval-corpus.md) | "Did my placement change make the whole corpus better or worse?" |

You do not need to name a skill. Say what you want; the descriptions above are what
your agent matches against.

!!! note "There is deliberately no routing skill"

    `legolize-model` owns making a **new** model from a mesh or voxel file.
    `optimize-lego-build` owns improving an **existing** brick assembly. That boundary
    is the routing, and it is drawn on purpose rather than delegated to a dispatcher
    that would guess.

---

## What every skill promises you

The same contract, in all ten:

- **It explains the likely outcome in plain language before running anything.**
- **It asks only when a choice materially changes the result** — and it always
  recommends one option rather than handing you a menu.
- **It presents the result as a path, a verdict, any warnings, and one useful next
  action.**

And three things it will not do:

- **It will not overwrite your source file.** Ever. Every operation writes into a new
  directory beside your input.
- **It will not present a rejected candidate as a fix.** If a repair failed, it says
  so, and shows you the best failed attempt labelled as such.
- **It will not launder an uncertain result into a confident one.** When the analysis
  says "unverified", the skill repeats that word.

---

## What you get back

Every operation produces a **bundle** — a self-describing directory beside your input:

```
dragon-legolization/
  model/model.mpd            the model you would build
  bom/bom.json               every part you need
  comparison/report.json     what was tried and why this one won
  instructions/              build steps, and a booklet when rendering is set up
  bundle.json                the record of what was run
```

You can move it, archive it, or hand it back to another skill. The skills accept a
bundle directory anywhere they accept a model.

More on reading these: [Reading the results](reading-results.md).

---

## Going further

When you want to drive the tool yourself, the [Guide](../guide/index.md) covers every
command and configuration key. When you want to know how the physics or the placement
algorithms work, that is the [Theory](../theory/index.md) track.
