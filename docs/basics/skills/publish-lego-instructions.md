# `publish-lego-instructions`

**Produce step-by-step building instructions.**

---

## Say something like

> Make a printable instruction booklet for my mushroom model.
>
> I need build instructions for this.
>
> Export the steps as a PDF.
>
> Give me an A4 building guide.

**Accepts:** any model or bundle directory.

---

## What you get

Always:

- a **step-annotated model file** you can open in a viewer and step through,
- an **instruction audit** — the machine check that the steps actually work.

And when a renderer is installed:

- **`instructions.html`** and **`instructions.pdf`** — a cover page with model
  statistics, the full parts list, one rendered picture per step with the new bricks
  highlighted, and per-step part callouts.

---

## What is in the booklet

| | |
| --- | --- |
| **Adaptive step size** | Steps are sized to the model — no fixed number of bricks per step |
| **Symmetric halves together** | Left and right wings appear in the same step, not eleven steps apart |
| **Subassemblies** | Parts that would float during the build — mushroom caps, arch spans — are lifted out as separately built units, then attached as one piece |
| **View rotations** | The model turns when the next step is on the other side, and not otherwise |
| **Insertion checks** | Steps where pressing a piece home could disturb the build are flagged |

---

## If you have no renderer

The booklet is **omitted entirely** — cleanly, with the omission recorded. You still get
the step-annotated model and the audit.

!!! success "This is deliberate"

    A booklet full of blank placeholder pages is worse than no booklet. If you want the
    pictures, ask for a renderer to be set up — the skill borrows
    [`render-ldraw`](render-ldraw.md)'s consent-gated installer.

If you say the booklet is **required**, a missing renderer becomes a warning rather
than a silent omission, so it cannot be missed.

**If only some steps render**, the booklet is still produced and the missing steps are
**explicitly marked** — never silently blank. Ask which ones.

---

## Page size

Letter or A4, chosen from your system locale, falling back to Letter. Booklets are
English-only.

---

## When it stops

| Result | Meaning |
| --- | --- |
| **complete** | Instructions published |
| **partial** | Published, with something to read — a missing booklet, missing steps, or audit findings |
| **unbuildable** | The model fails physics, so instructions cannot be certified |
| **error** | Invalid input or a runtime problem |

---

## Where it hands off

- **Set up pictures** → [`render-ldraw`](render-ldraw.md)
- **Review the steps properly** → [`inspect-instructions`](inspect-instructions.md)

!!! note "Publish makes the booklet; inspect audits the plan"

    This skill produces the thing a human follows.
    [`inspect-instructions`](inspect-instructions.md) asks whether that thing makes
    sense. Different questions.
