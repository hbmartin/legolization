# `extend-lego-part-support`

**Add support for a part the tool does not know yet.**

The catalog covers 58 parts. This researches, drafts, validates, and activates a new
one.

---

## Say something like

> Add support for part 4070 so my headlight-brick model analyzes correctly.
>
> This build failed on an unsupported part — can you fix that?
>
> Infer the geometry and mass for LDraw part 3001.

---

## What it does

1. **Researches the part** across authoritative sources, capturing where every value
   came from.
2. **Drafts** its geometry and physical estimates into a support bundle.
3. **Shows you the draft** — and flags anything with weak or missing provenance.
4. **Validates** it against five gates.
5. **Activates** it, only when you explicitly say so.

---

## What you get

```text
part_4070-legolization-support/
  catalog-extension.json     the drafted part
  draft-estimates.json       mass and geometry, with provenance
  sources.json               what was consulted, and what each said
  validation.json            gate results
  geometry/4070.dat
```

---

## The five gates

| Gate | Checks |
| --- | --- |
| **import** | The part's geometry loads and resolves |
| **round-trip** | Writing and re-reading it preserves it exactly |
| **collision** | Its collision volume is well-formed |
| **connector** | Its studs and anti-studs sit at valid mating points |
| **topology** | It can take part in a connection graph without ambiguity |

The skill will **never suggest activating a part while any gate is failing.**

---

## Two rules it will not bend

!!! warning "Estimates are estimates"

    Drafted values carry their recorded provenance and receive **no hidden safety
    adjustment**. They are presented with their sources, never as certified figures.

    A mass from a volumetric guess and a mass measured from a catalog dump are very
    different things, and the draft says which you have.

!!! danger "Nothing activates silently"

    A validated part only takes effect when it is explicitly named on a run. It does
    not quietly join your catalog because it passed.

    Promoting a part into the **built-in** catalog requires separate, explicit
    confirmation — that changes physics results for everything.

---

## Working offline

You can ask it to skip network lookups. It still consults any local catalog dump you
have, and it will tell you which sources it could not reach so you know what the draft
is missing.

---

## When it stops

| Result | Meaning |
| --- | --- |
| **complete** | Validated, measured, and confident |
| **partial** | The draft was written but not every gate passed — read `validation.json` |
| **error** | The part could not be found |

There is no "unbuildable" here. A draft is not a physics verdict.

---

## Where it hands off

The validated bundle can be activated on any of
[`legolize-model`](legolize-model.md), [`optimize-lego-build`](optimize-lego-build.md),
[`analyze-lego-assembly`](analyze-lego-assembly.md), or
[`repair-lego-assembly`](repair-lego-assembly.md) — by naming it explicitly.

---

## When you need this

Mostly you do not. Reach for it when a run fails on an unknown part, or when you want a
specific part available to the sideways-cladding and detail passes.

Everything here is opt-in by design, because catalog changes alter physics results and
should never be a surprise.
