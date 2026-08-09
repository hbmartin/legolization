# Troubleshooting

Symptom, cause, fix.

---

## "Not buildable"

The most common real outcome. The physics rejected every attempt.

### It is usually not enough material

A one-brick-thick shell cannot always carry the load its own shape implies. The fix is
to add material, and there is a built-in ladder for it:

> That failed — try again with more material. Give it half an hour.

The skill will confirm and then walk three rungs: a four-plate shell, a six-plate
shell, and finally solid. It stops at the first one that works.

It needs a **total time budget from you**, which is why it asks rather than guessing.

### It might be the shape

Some shapes cannot stand as brick models, and no algorithm fixes that:

| Symptom | Cause |
| --- | --- |
| Every attempt scores exactly **1.0** stress | **Toppling.** The centre of mass is outside the base. The shape needs a wider footprint. |
| Reported as **several pieces** even though each part sits on the ground | Your source is several separate islands. Resting on the same ground is not being connected. |
| Long unsupported spans fail | An arch or bridge with nothing under the middle. Try a thicker shell, or accept a support structure. |

Ask to see the failed attempt — it is kept in `diagnostics/` — and the problem is
usually obvious at a glance:

> Show me the best rejected model.

### Other things to try

> Try a different placement strategy.

`luo` optimizes structural soundness directly and often succeeds on spanning shapes
where others fail. See [Choosing a strategy](../guide/choosing-a-strategy.md).

---

## The model looks wrong

| Symptom | Fix |
| --- | --- |
| **Limbs or thin parts missing** | Voxelization dropped them. Ask for a larger size — more studs across means finer detail. |
| **Blocky where it should be smooth** | Same cause. Larger size, or accept the resolution. |
| **Wrong colours** | If it came from a mesh with no colour data, a single colour was chosen. Ask for a specific one, or for colours to be sampled from the mesh. |
| **Colours fragment the brickwork into tiny pieces** | Colour boundaries force small parts. Ask for *soft* colour mode — fewer, larger bricks at the cost of some slightly-wrong colour at the edges. |
| **It is upside down or on its side** | The up-axis was misclassified. Most `.obj` files are **y-up**. Say so and re-run. |
| **Visible vertical seams stacked on top of each other** | Ask for the `bond` strategy, which prioritizes running-bond staggering. |

---

## No images, no booklet

**Cause:** no renderer installed.

Everything else still works — you get the model, the parts list, and the step-annotated
instruction file. What you do not get is pictures.

> Set up a renderer for me.

The skill checks first (free), explains the platform-appropriate install, and asks
before doing anything:

| Platform | What gets installed |
| --- | --- |
| macOS | LDView, via Homebrew |
| Windows | LeoCAD, via winget |
| Ubuntu/Debian | LeoCAD and Xvfb, via apt |

!!! note "A missing booklet is deliberate, not a bug"

    Rather than producing a booklet full of blank placeholder pages, the tool omits it
    entirely and records that it did. A booklet you cannot build from is worse than no
    booklet.

**If some images render and others do not:** the booklet is still produced, and the
missing steps are **explicitly marked**. Ask which ones.

---

## It is taking forever

Runtime scales with model size, not with your patience.

| Input | Expect |
| --- | --- |
| Small voxel model | Seconds |
| Mesh at 16–24 studs | Minutes |
| Mesh at 28–36 studs | **Tens of minutes per strategy** |

Three things help:

1. **Ask for a quick preview first** — the `fast` tier gives you an answer in about two
   minutes so you can check the shape and size before committing.
2. **Ask for a smaller size.** Half the studs is far less than half the work.
3. **Ask for it to run in the background.** Long runs print nothing until they finish,
   so silence is expected, not a hang.

!!! warning "Stopping a long run"

    Interrupting saves its place — re-running continues from there. But background
    workers may keep using CPU after the command returns. Ask your agent to cancel the
    pending workers if you need the machine back.

---

## "Unsupported part"

The tool knows 58 parts by default. A model using something else will say so.

> Add support for part 4070.

That runs [`extend-lego-part-support`](skills/extend-lego-part-support.md), which
researches the part, drafts its geometry and mass, and runs five validation gates.

Two things to know:

- **Estimates are estimates.** Drafted values carry their sources and receive **no
  hidden safety margin**. Read where a mass came from before trusting it — a
  volumetric guess is not a measurement.
- **Nothing activates silently.** A validated draft only takes effect when you
  explicitly ask for it to be used.

---

## The result changed between runs

It should not have. Everything is seeded and deterministic — the same input with the
same settings produces the same output, byte for byte.

If it changed, something changed with it:

| Likely cause | Check |
| --- | --- |
| Different settings | A different quality tier, size, or strategy makes a different bundle |
| A different software version | Bundle identity includes it |
| An updated parts catalog | So does that |
| A time-limited run | If it reported `partial`, it published the best result *available at the deadline* — a later run may find better |

---

## Reading a warning correctly

| It says | It means |
| --- | --- |
| `partial` | It worked; something is worth reading |
| `findings` in the instruction audit | Warnings about specific steps; the model is still buildable |
| `unverified` | **Take this literally.** The tool could not confirm the recommendation — usually because a part's strength is unknown. Do not treat it as confirmed. |
| `indeterminate` | Physics could not reach a verdict, usually for the same reason |
| `infeasible` in an instruction audit | A genuine bug. Please report it. |

---

## Still stuck

Ask your agent to run a diagnosis:

> Analyze this model and tell me what is actually wrong with it.

That produces a full structural report: the connection graph, each disconnected piece
as its own file, every floating brick as its own file, load paths, and an HTML report
with pictures if a renderer is available. Opening the floating-bricks file usually
answers the question in seconds.

If you think you have found a bug — particularly an `infeasible` instruction audit or
a crash — the `bundle.json` in your output directory records exactly what ran, and is
the most useful thing to attach to an
[issue](https://github.com/hbmartin/legolization/issues).
