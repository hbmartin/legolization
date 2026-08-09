# The analysis stack

**Source:** `analysis.py`, `assembly_*.py` · the geometry-first path for arbitrary
imported LDraw
{ .provenance }

Generation works on a voxel lattice with a 58-part catalog. Real LDraw models do not.
They contain gears, hinges, angled mechanisms, half-stud offsets, and parts nobody has
measured.

The analysis stack is a **parallel** implementation, backed by pyldraw3, built to say
useful things about those models rather than rejecting them.

---

## The design principle

> Preserve everything; report capability by capability; never silently snap.

An imported matrix and position are kept **exactly**. Catalog and assembly capabilities
then decide, independently, whether collision, connector, and physics operations
support that pose. An unsupported connector produces **partial evidence**, not an
aborted model.

This is why `analyze` handles models `build` cannot: it never needs the model to fit
the generation lattice.

---

## Two verdicts, deliberately separate

| Verdict | Values |
| --- | --- |
| Topology | `feasible` \| `infeasible` \| `indeterminate` |
| Physics | `feasible` \| `infeasible` \| `indeterminate` |

A model can be **definitely connected** while its physics stays **indeterminate**,
because some part's load-bearing capacity is not in the registry.

!!! important "Unknown capacity keeps physics indeterminate"

    Even when an *optimistic* equilibrium exists, an unknown capacity yields
    indeterminate — not feasible.

    `ScenarioResult` carries **both** an `optimistic_feasible` and a
    `known_capacity_feasible` verdict, plus the list of
    `unknown_capacity_variables`. Collapsing those into one number would be a lie in
    whichever direction you collapsed it.

    Topology-only recommendations on an indeterminate result are explicitly marked
    **unverified**, and that label is meant to be repeated verbatim rather than
    laundered into confident advice.

---

## Triple physics check

`_run_physics` runs three independent checks and requires **all** of them:

```python
official_failure = (
    component_count != 1
    or floating_ids
    or not parity.stable  # stablelego-parity profile
    or not strict.stable  # corrected 6-DOF profile
    or not maximin.feasible
    or maximin.capacity <= 0
)
```

The report declares `required_profiles = ["rbe-5dof", "rbe-6dof", "maximin-6dof"]`.

Running both physics profiles is not redundancy for its own sake. The parity profile
reproduces StableLego's published behaviour; the corrected profile models yaw torque
and rotation-invariant contacts. A model that passes one and fails the other is a
finding worth surfacing rather than a coin flip to resolve internally.

On failure, `localize_instability` supplies the seeds that the repair search works from.

---

## The assembly modules

| Module | Responsibility |
| --- | --- |
| `assembly_model` | Occurrence bodies from `ModelInspection` — bounds, centre of mass, convex hulls |
| `assembly_registry` | What pyldraw3 does not carry: mass, centre of mass, inertia, collision proxies, region tags, force capacities |
| `assembly_connections` | Typed connection edges with degrees of freedom and capacities |
| `assembly_physics` | Six-DOF equilibrium per occurrence body |
| `assembly_paths` | Region classification, selectors, load paths, connector minimum cuts |
| `assembly_grid` | Deterministic multi-frame lattice detection |
| `assembly_counterfactual` | Bounded BOM-preserving counterfactual diagnosis |
| `assembly_artifacts` | Graph, MPD, HTML, and callout artifacts |

### The registry

A builtin curated table merged with user schema-1 JSON by priority, validated with
JSON Schema Draft 2020-12.

The stud capacity mirrors the generation model's constant:

```python
STUD_CAPACITY = ConnectionCapacity(
    pull_n=0.98,
    compression_n=0.98,
    shear_n=0.98,
    torque_nm=0.0,
    friction_coefficient=1.0,
)
```

`torque_nm = 0.0` is deliberate and correct: **torque is transmitted by separated
studs, not by one stud**. A single stud is a point contact with no moment arm. Giving
it a torque capacity would let a one-stud connection resist twist that it physically
cannot.

`--connector-catalog` is the **only** source for these values. Studio `mass_g`/`tags`
fields are no longer read, because their provenance could not be established.

### Connections

Typed edges carry `DegreesOfFreedom(translations, rotations)` and
`ConnectionCapacity(pull_n, compression_n, shear_n, torque_nm, friction_coefficient)`.
`_combined_capacity` takes the **elementwise minimum** — a chain is as strong as its
weakest link on each axis independently.

Inferred surface contacts, connected components, per-occurrence coverage, and a
deterministic JSON projection round it out. Confirmed and potential connector graphs
stay separate throughout, so "we found a connection" and "there might be a connection
here" never get conflated.

### Six-DOF physics

Six rows per body — three force, three torque — rather than the generation model's
5-or-6 per brick. Moments are $(p - c) \times F$ about each body's own centre of mass.

Unit conversions: `_LDU_M = 0.0004` m per LDU, `_GRAVITY_M_S2 = 9.80665`,
`_DEFAULT_FRICTION = 0.5`.

Support resolution handles `auto | free | wheels | auto-ground | anchored-baseplate |
selected:<sel>` with retained evidence for each choice — so the report can say *why* it
seated the model the way it did.

### Load paths and cuts

`assembly_paths` answers "what carries load between these two regions?" via region
selectors like `pages:1-20` or `occurrences:20-30`, and computes connector **minimum
cuts** — the smallest set of connections whose removal separates two regions.

This is the yield-line reading from Kollsker's thesis: the minimum cut is where the
structure will fail, so finding it identifies the weak seam without needing to simulate
the failure.

### Counterfactuals

`assembly_counterfactual` performs bounded **BOM-preserving** diagnosis: which single
re-placement of an existing part would fix this? The BOM constraint is what makes the
answer actionable — it never suggests parts you do not have.

---

## Repair search

On a definite failure, the repair search tries **one BOM-preserving source edit at a
time**: orthonormal rotations and reflections, and nearby stud/plate translations.

A suggestion must improve **real connector topology**. Bounding-box overlap alone never
counts — that was the failure mode of naive approaches, which happily suggested moving
two parts so their boxes touched without any mating geometry meeting.

The budget splits deliberately: the counterfactual gets $\min(60\ \text{s},\ 25\%)$ of
the budget, and the redesign search gets the remainder. Diagnosis before search, capped
so a hard diagnosis cannot eat the whole budget.

A found candidate is marked `verification="physics-validated"` **only after passing
every official profile**. And repair never overwrites the source: the best validated
edit goes to a new file, and a failed repair explains why and retains its best rejected
candidate — which must never be presented as a fix.

---

## Step analysis

`_analyze_step_groups` walks prefixes with a `PrefixSolver`, reusing the whole-model
strict solve when a single step covers everything. Same warm-solving machinery as
generation — see [Warm solving](stability/warm-solving.md).

---

## Reports

The canonical output is the version-1 assembly manifest. Two legacy schemas remain
available as **explicitly requested derived views** — schema-2 analysis via `--report`,
schema-1 assembly via `--assembly-report` — rather than being written by default.

The assembly schema records exact occurrence transforms and provenance, connector
coverage, confirmed and optimistic component counts, detected grid frames, resolved
support, region-to-region cuts, load scenarios, and ranked counterfactual evidence.

Public occurrence IDs are **one-based**; pyldraw3's zero-based traversal index is
retained separately. Two numbering systems, both preserved, neither silently converted.

Exact stud contacts and AABB gaps are computed through 1 000 occurrences and **marked
as skipped** above that safety limit — an explicit gap in the evidence rather than a
silent truncation.

---

## Why two stacks exist

A reasonable question: why not use the assembly stack for everything?

| | Generation stack | Assembly stack |
| --- | --- | --- |
| Geometry | Voxel lattice, yaw-only | Exact transforms, any pose |
| Parts | 58 measured catalog parts | Anything pyldraw3 resolves |
| Physics | 5–6 rows per brick, knob-level contacts | 6 rows per body, typed connectors |
| Capacities | Known for every part | Often unknown — hence indeterminate |
| Speed | Fast enough for a per-step LP | Slower, richer |

Generation *needs* the lattice: exact cover, merging, and bonding all assume a regular
grid, and the physics model needs measured masses for every part. Analysis needs the
opposite — completeness over regularity.

They meet at the `--retile` path, which converts an imported assembly into a coloured
occupancy target and hands it to generation. That conversion is lossy by definition,
which is why it is opt-in and why the default for an imported assembly is to preserve
it exactly.
