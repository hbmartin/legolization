# `analyze-lego-assembly`

**Diagnose an assembly's stability, load paths, and assembly risks.**

Diagnosis only. It changes nothing and repairs nothing.

---

## Say something like

> Why does my spaceship model feel wobbly at the wings?
>
> Is this model stable?
>
> What's floating in this build?
>
> Give me a structural report before I build this.
>
> Which bricks are weakest?

**Accepts:** `.ldr`, `.mpd`.

---

## What you get

```text
spaceship-analysis/
  analysis.html                     a readable report, with pictures if you have a renderer
  diagnostics/connections.json      what is attached to what
  diagnostics/components.mpd        each disconnected piece, as its own model
  diagnostics/floating.mpd          every brick with no route to the ground
  renders/
  report.json
```

The two `.mpd` files are the fastest way to *see* a connectivity problem — open them in
a viewer rather than reading JSON.

---

## What it can tell you

| Question | Answer |
| --- | --- |
| Is it one piece? | Component count, and each piece as a separate file |
| Is anything floating? | Every unsupported brick, as a separate file |
| Will it stand? | A physics verdict under three independent checks |
| Where is it weakest? | The weakest joint, and each brick's stress |
| What carries the load between here and there? | Load paths, and the minimum set of connections whose loss would separate them |
| What happens if I pick it up by the body? | Load scenarios — resting, lifting, torsion, side load |

---

## How it decides the model is sitting

Support handling adapts to what you have:

- a strict grid-aligned model gets an anchored baseplate,
- a detected vehicle rests on its wheels,
- anything else uses loose lowest-surface contacts.

You can override it — "analyze this as if it were resting free" — and that mode
deliberately reports **no** static verdict, because seating is meaningless there.

---

## The honesty rules

!!! warning "\"Unverified\" means unverified"

    When a part's strength is not in the reference data, physics stays
    **indeterminate** — even when an optimistic reading would say it works.
    Recommendations under that condition are labelled **unverified**, and the skill
    repeats that label verbatim rather than smoothing it into confident advice.

!!! abstract "It will not fix anything"

    Analysis is diagnosis. When it finds problems it hands you to
    [`repair-lego-assembly`](repair-lego-assembly.md) rather than quietly attempting
    something you did not ask for.

---

## When it stops

| Result | Meaning |
| --- | --- |
| **complete** | Connected and feasible |
| **unbuildable** | Definitely disconnected, or definitely fails physics as built |
| **partial** | Indeterminate — usually an unknown part strength. Recommendations are marked unverified. |
| **error** | Invalid input, or a runtime problem |

---

## Where it hands off

→ [`repair-lego-assembly`](repair-lego-assembly.md) when there is something to fix.

---

??? info "Advanced controls (only if you ask)"

    - **Support mode** — `auto`, `free`, `wheels`, `auto-ground`, `anchored-baseplate`,
      or a specific selection
    - **Load scenarios** — `rest`, `lift-body`, `lift-chassis`, `front-torsion`,
      `rear-torsion`, `side-load`, repeatable, with adjustable gravity, side-load, and
      torsion multipliers
    - **Topology only** — skip physics entirely
    - **Load paths** — between two named regions
    - **External metadata** — connector catalogs, LDCad shadow libraries, Studio
      connectivity exports

    All covered in [`analyze`](../../guide/cli/analyze.md).
