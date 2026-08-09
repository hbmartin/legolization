# `eval-corpus`

**Measure whether a change made the whole project better or worse.**

A development tool, not a model-building one. If you are making LEGO models, you will
never need this.

---

## Say something like

> Did my placement change make the whole corpus better or worse?
>
> How is the project doing overall?
>
> Find me the worst current case to improve next.
>
> Run the regression sweep.

---

## What it does

1. **Prepares the corpus** — generates the synthetic test shapes and, if you want them,
   downloads the pinned meshes.
2. **Sweeps** every model through every placement strategy.
3. **Assembles a scorecard** and diffs it against the committed baseline.
4. **Points you at the worst row** so you know what to fix next.

Output lands in `./legolization-eval/` — the one skill that does not write beside your
input.

---

## It will warn you about cost up front

| Corpus | Time |
| --- | --- |
| **Synthetic** *(the default)* | Minutes |
| **Meshes** *(opt-in)* | Tens of minutes to hours |

Mesh sweeps should run in the background. The skill will say so before starting one.

---

## Reading the scorecard

| Column | Read it as |
| --- | --- |
| **buildable_count** | How many strategies produced a working result. Higher is better. |
| **winner objective** | Overall score of the best result. Lower is better — **only comparable within a model**. |
| **expectation** | What this model is currently expected to achieve. Some shapes expect zero *by design* — they are physically impossible. |
| **seed_spread** | How much the result varies with a different random seed. |

A **HARD regression** — the buildable count dropped, an expectation newly failed, or the
winner got measurably worse — must be explained or fixed before merging. Lines marked
`note:` are context, not failures.

Timings are noise. Never compare them across runs.

---

## The one rule that matters

!!! danger "It will never rewrite the baseline without asking"

    The baseline is the committed record of how good the project currently is.
    Replacing it to make a regression disappear defeats the entire harness.

    The skill will **never** do this unless you explicitly confirm it in the
    conversation — and it will not do it to silence a regression you cannot explain.

    A legitimate refresh happens after you have read the comparison and accepted every
    changed row, and the new baseline is committed alongside the code change that
    justified it.

---

## When it stops

| Result | Meaning |
| --- | --- |
| **complete** | Clean — no failures, no regressions |
| **unbuildable** | An expectation failed, or a HARD regression appeared |
| **error** | The sweep did not complete |

Meshes that are not available locally produce `note:` warnings and still exit clean.

---

## Where it hands off

Once you know *which* model got worse:

- **See what it looks like** → [`render-ldraw`](render-ldraw.md)
- **Check whether it is a sequencing problem** →
  [`inspect-instructions`](inspect-instructions.md)

---

## The full methodology

This page covers the skill. The discipline behind it — what to measure, which
regressions matter, and the rules around the baseline — is in the
[self-evaluation playbook](../../guides/self-evaluation-playbook.md).
