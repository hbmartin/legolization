# `inspect-instructions`

**Audit whether a model's build steps actually make sense.**

---

## Say something like

> Do these build steps make sense, or will something fall off halfway through?

> Review the step ordering.

> Why is it warning about unstable prefixes?

> Are any steps too big?

**Accepts:** a step-annotated `.ldr`/`.mpd`, or a bundle directory.

---

## What it checks

For every step, the state of the build *after* that step:

| | |
| --- | --- |
| **Size** | How many bricks this step adds |
| **Does the partial build stand?** | Full physics on everything built so far |
| **Is anything floating?** | Bricks with no route to the ground yet |
| **How many pieces?** | Whether the build is still in one connected part |
| **Can it be pressed home?** | Whether pushing the new bricks in would disturb what is there |

---

## The flags

| Flag | Meaning | Worry? |
| --- | --- | --- |
| `floating` | Something in this step has nothing under it yet | Sometimes legitimate — islands that join up later. It must be warned about. |
| `unstable-prefix` | The partial build does not stand on its own | One, warned, on a hard shape is acceptable. Several mean the sequencer struggled. |
| `insertion-fragile` | Stable, but pressing the piece home could disturb it | Worth a look — usually a sequencing choice, not a design flaw. |
| `oversized` | More bricks than the target step size | Cosmetic. |
| **violations** | A rule of the plan is broken | **Always a bug. Please report it.** |

---

## It looks at the pictures too

Ask it to render the steps and it will actually open the images and describe them
against a checklist:

- do the new bricks rest on something, or hover?
- is the step one coherent region, or bricks scattered across the model?
- could you actually push the piece straight down into place?
- does the view rotation between steps disorient you?

On a long build it samples rather than opening all of them: the first few, the last few,
**every flagged step**, and an even spread in between.

---

## When it stops

| Verdict | Meaning |
| --- | --- |
| **certified** | Every rule holds and every partial build stands |
| **findings** | Warnings worth reading; still buildable |
| **infeasible** | The final step leaves the model in pieces or with something floating, or a rule is violated. **This is a bug.** |
| **error** | Invalid input or a runtime problem |

---

## Where it fits

[`publish-lego-instructions`](publish-lego-instructions.md) produces the booklet a
human follows. This skill audits the plan behind it.

Reach for it when a booklet looks wrong, after a change to how steps are ordered, or
when you want to know whether a warning is serious.

Also useful from [`eval-corpus`](eval-corpus.md) when a model's weakness turns out to
be sequencing rather than placement.
