# Theory

This track explains why the system works and where its guarantees come from. It is
written for someone who wants to change the algorithms, verify a claim, or read the
papers alongside the code.

Sections name their **source** — the paper and the implementing file or symbol — and
label claims as *proved*, *measured*, or *heuristic*. Those three words are not
interchangeable, and the difference is the point of this track.

---

## The three epistemic labels

**Proved.** A mathematical argument holds unconditionally, given the model. The LP
relaxation's exactness is proved: any optimum with a contact both pressing and
pulling can be strictly improved, so no optimum has one. Proofs are about the model,
not about reality — they say the solver computes what the model says, not that the
model is a real LEGO brick.

**Measured.** A claim backed by an experiment recorded in this repository, with the
numbers. That the single weighted screening QP converges in hundreds of ADMM
iterations where the paper's three-stage relaxation needs tens of thousands is
measured. Measured claims can go stale — re-run the experiment before leaning on one.

**Heuristic.** Something that works well enough, chosen by judgement or tuning, with
no argument that it is right. Most placement scoring terms are heuristic. So are most
of the constants in the objective weights. This is not a criticism — the underlying
problem is NP-hard and heuristics are the only tractable answer — but it should never
be dressed up as more than it is.

---

## Reading order

<div class="grid cards" markdown>

-   **[The pipeline](pipeline.md)**

    ---

    Phase order, the guard pattern that makes quality passes safe, and where each
    phase can fail. Start here for the shape of the whole thing.

-   **[Representations](representations.md)**

    ---

    `VoxelGrid`, `Part`, `Layout`, `ConnectionGraph`. The three-tier geometry story
    and the colour lattice. Everything else assumes these.

-   **[Placement](placement/index.md)**

    ---

    The shared objective, then eight strategies: constructive heuristics, the layered
    engine, metaheuristics, exact methods, the merge engine and ALNS repair, and the
    finishing passes.

-   **[Stability](stability/index.md)**

    ---

    The RBE formulation, the exactness proof, the maximin and artificial-link
    certificates, the screening hierarchy, and warm incremental solving.

-   **[Sequencing](sequencing.md)**

    ---

    Bands, the readiness predicate, the removability–insertability duality that makes
    assembly-by-disassembly correct, and the maximal-stability beam search.

-   **[Subassemblies](subassemblies.md)**

    ---

    The one class of unstable step no reordering can fix, and the rewrite that
    addresses it — including an honest account of what it does *not* fix.

-   **[The analysis stack](analysis-stack.md)**

    ---

    The parallel geometry-first path for arbitrary imported assemblies: typed
    connectors, capacities, six-DOF equilibrium, load paths, and counterfactuals.

-   **[Papers and provenance](papers.md)**

    ---

    Thirteen references mapped to implementing code, including every deliberate
    deviation from the papers.

</div>

---

## The problem, stated

Given a target shape as a set of filled cells on a lattice, each with a colour,
choose a set of parts from a catalog such that:

1. every target cell is covered exactly once (exact cover),
2. no two parts occupy overlapping physical volume,
3. every part's colour is compatible with the cells it covers,
4. the resulting assembly is a single stud-connected component,
5. every part has a stud path to the ground,
6. the assembly is in static equilibrium with friction demands below capacity,
7. there exists an ordering in which it can be physically built, where every
   partial assembly also satisfies (6),

while minimizing part count (or mass) and maximizing structural and visual quality.

Conditions 1–3 alone make this a set-partitioning problem, which is NP-hard.
Condition 4 adds a connectivity certificate. Condition 6 couples the combinatorial
choice to a continuous optimization. Condition 7 makes the *output* a sequence rather
than a set, and its feasibility is not implied by any of the others.

The system's structure follows from that decomposition: heuristics or exact methods
handle 1–4, stud-graph reachability on the `ConnectionGraph` enforces 5 within the
same connectivity check as 4 (the ground-merged `floating_ids` half of the
buildability verdict), an LP handles 6, a search handles 7, and a repair loop
mediates when 6 fails given a 1–4 solution.

---

## What is genuinely guaranteed

Being precise about this matters, because "provably exact" is used in this project in
a specific and narrow sense.

| Claim | Status |
| --- | --- |
| The LP relaxation of the RBE gives the same answer as the complementarity MILP | **Proved.** [Exactness](stability/exactness.md) |
| A reduced-QP screen can only *overestimate* the drag the exact LP needs | **Proved** for the restriction; relaxed to a strong tendency by the soft equilibrium term. [Screens](stability/screens.md) |
| Reversing a valid disassembly order yields a collision-free build order | **Proved.** [Sequencing](sequencing.md) |
| The disassembly search never deadlocks | **Proved**, given band-pure chunks. [Sequencing](sequencing.md) |
| `global-exact` finds the minimum-part-count layout satisfying cover, collision, colour, and connectivity | **Proved**, within its enumerated candidate set and its caps. [Exact methods](placement/exact.md) |
| `kollsker` finds the minimum part count *per layer* | **Proved** per layer; says nothing about the whole model. [Exact methods](placement/exact.md) |
| A published model is stable | **Certified** — every published layout and every emitted instruction sequence passes a full cold solve. Guaranteed relative to the physics model. |
| The physics model predicts what a real brick does | **Not claimed.** Constants come from StableLego's measurements; see [physics fidelity notes](../reports/physics-fidelity-notes.md). |

The last row is the important one. Everything upstream is exact *with respect to a
model of brick physics*, and that model is a linear approximation with measured
constants and known simplifications.

---

## The certification interlock

One design decision holds the performance work together, and it is worth stating
before you read any of the fast paths.

The system uses several approximations to avoid solving the full LP: a reduced QP
screen, a frozen-boundary incremental screen, warm-started prefix solves, and a
graph-only floating shortcut. Each could in principle produce a wrong verdict.

None of them can produce a wrong *published* verdict, because:

> **Every accepted modified layout and every emitted instruction sequence is certified
> by a full cold solve.** The only thing a screen may do without a cold solve is
> **reject** a candidate — and a rejected candidate is simply not used.

A screen that wrongly rejects costs quality. A screen that wrongly accepts costs
nothing, because acceptance is never final. That asymmetry is why the screens are
allowed to be approximate at all, and it is why `ScreenReport` is a deliberately
different type from `StabilityResult` and never enters a verdict-bearing artifact.

---

## Conventions in this track

- **Units.** Geometry is in integer LDU (20 per stud horizontally, 8 per plate
  vertically). Physics is in SI: metres, newtons, kilograms. The two conversions are
  `KNOB_PITCH_M = 0.0078` and `PLATE_HEIGHT_M = 0.0032`.
- **Indices.** `i` ranges over bricks, `j` over force variables, `r` over equilibrium
  rows, `f` over contact points, `b` over placement candidates.
- **Code references** are `path/file.py` with a symbol name. Line numbers drift; names
  do not.
- **Papers** are cited by short name and mapped in full on the
  [papers page](papers.md).
