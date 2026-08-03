# Roadmap

This document is the current source of truth for legolization's pre-release
engineering program. The former dated progress journal is preserved in
[`docs/roadmap-history.md`](docs/roadmap-history.md); historical measurements
there are evidence, not descriptions of current defaults.

## Verified current state

- The primary interface is a local, deterministic command-line application:
  `build`, `analyze`, `validate`, and `cache inspect|clear`. Domain functions
  are called in-process; there is no network service or daemon.
- New command paths use strict nested TOML configuration. Precedence is
  built-in defaults, then TOML, then explicit command-line overrides. Unknown
  keys, invalid path kinds, incompatible modes, and non-finite values fail
  validation.
- Successful builds and analyses emit a canonical
  `legolization.assembly-manifest` version 1 sidecar by default. Canonical JSON
  contains no wall-clock timestamp and includes hashes, algorithms, exact LDU
  poses, normalized contacts, capability results, stability evidence, action
  relations, instructions, BOM data, artifacts, and cache provenance.
- Generated placement is yaw-only. Imported LDraw matrices are preserved
  exactly and analyzed capability by capability; unsupported operations are
  reported as partial, never silently snapped or described as exact.
- Geometry uses integer LDraw units for transforms, connector locations, and
  collision volumes. Stud/plate cells remain a separate coarse target-coverage
  representation. Exact collision checks follow coarse spatial bucketing.
- Placement defaults to `auto`: bounded global exact placement is selected
  when preflight is within the cell and candidate-row caps; deterministic
  `bond` placement is selected otherwise. Explicit exact placement defaults to
  failing at a limit, with explicit `fallback` and deadline-bound `continue`
  policies available.
- Heuristic restarts are deterministic and opt-in (`restarts = 1` by default).
  Exact placement is not raced across seeds.
- The corrected six-degree-of-freedom rigid-body-equilibrium profile is the
  default. It enables yaw torque, yaw-rotated contacts, the paper contact-point
  rule, and virtual-baseplate support. `stablelego-parity` remains an explicit
  reproduction profile. Loose-table analysis and optional emitted physical
  support plates are supported.
- Production physics uses the linear RBE solver. The former complementarity
  placement/configuration mode is obsolete; only a private small-instance
  reference oracle remains for equivalence tests.
- Incremental rejection screening uses exact changed bounds, contact-graph
  rings, and frozen interface forces. Every accepted modified layout and every
  emitted instruction sequence is certified by a full cold solve.
- Contact evidence is normalized into physical connectors while preserving all
  raw parser evidence. Assembly planning uses typed connector, support,
  six-direction blocker, order, subassembly, and template relations and records
  the earliest failing action when certification fails.
- Shape-preserving slope and tile finishing is on by default. It preserves
  target occupancy, includes inverted slopes, and uses mesh normals when
  choosing orientation. Plate caps are opt-in because they change layering and
  part count. Headlight bricks and inverted brackets provide exact compound
  side-detail templates with half-stud/half-plate geometry.
- MagicaVoxel input uses the format's default palette when RGBA is absent.
  Multi-model scenes are composed through PACK and scene transforms; ambiguous
  unplaced models and conflicting overlaps are errors.

## Completed foundation work

- Canonical nested configuration, typed errors/results, typed progress events,
  and a shared monotonic deadline.
- Canonical deterministic manifests and manifest-derived BOM/action/stability
  views, including build, analysis, and validation command paths.
- Exact LDU catalog schema, collision geometry, physical connectors, half-unit
  offsets, arbitrary imported-matrix preservation, and exact yaw invariance.
- Bounded whole-model exact cover with hard target coverage, color, collision,
  rooted connectivity, brick-count or mass objectives, deterministic tie
  breaking, physics certification, and weakest-region no-good cuts.
- Deterministic grid-phase selection, opt-in automatic scale selection, mesh
  feature annotations, normal-aware finishing, inverted slopes, tile finishing,
  and opt-in plate caps without source deformation.
- Repeated-component signatures under translation and yaw with color remapping,
  reusable instruction submodels, and a content-addressed platform cache with
  locking, atomic writes, corruption quarantine, deterministic inspection, and
  scoped clearing.
- Corrected physical-contact normalization, full directional blocker analysis,
  hierarchical assembly verification, causal prefix-failure traces, frozen-
  boundary screening, and final cold certification.
- Strict dependency declarations for the editable LDraw sibling projects.

## Active work

The remaining pre-release work is maintenance rather than a new product area:

- Keep the manifest schema and exact catalog migrations explicit and versioned
  as supported parts and imported poses expand.
- Continue decomposing algorithm implementations when they approach the
  enforced complexity limits; preserve typed state/context boundaries rather
  than growing orchestration functions.
- Expand small deterministic property and structural golden fixtures when a
  geometry, instruction, or cache regression is found. Optional LDView visual
  snapshots remain developer infrastructure, not a required renderer feature.
- Finish removing legacy report duplication once all internal evaluation
  consumers read manifest-derived views. Existing evaluation and BOM behavior
  remains available during that mechanical migration.

## Research traceability

The implementation deliberately borrows mechanisms rather than claiming paper
parity:

| Source | Applied foundation | Deliberate boundary |
| --- | --- | --- |
| *Legolization* and silhouette-fitted voxelization | Deterministic voxel phases, surface/silhouette error, exact coverage, and scale selection | The source mesh is not deformed to improve brick fit. |
| *Automatic Generation of Vivid LEGO Architectural Sculptures* | Normal-aware architectural slopes, exposed detail regions, and repeated structural motifs | Only catalog-declared, occupancy-preserving templates are generated. |
| *StableLego* | Rigid-body equilibrium, contact forces, weakest contacts, and a named reproduction profile | The corrected production profile intentionally includes additional torque/contact corrections. |
| Force-based reinforcement work | Weakest-region localization and repair feedback | Every repair is rechecked by a cold solve rather than accepted from a proxy score. |
| Assembly-sequence graph work | Typed contact/blocker action graph, hierarchical subassemblies, and prefix checks | Planning remains deterministic and rule/optimization based; no learned planner is introduced. |
| Kollsker, Luo, split-and-merge, and streamlining work | Existing deterministic placement/refinement strategies and exact subproblems | Whole-model exact placement is separately bounded and explicitly reports limits/fallbacks. |
| Segmentation and component methods | Independent component analysis/search and reusable component templates | The paper's weak force heuristic is not copied; certification uses the RBE solver. |

Converted paper text is retained under [`references/`](references/) and the
source PDFs under `papers PDFs/`. When a paper and the implementation differ,
tests and the explicit configuration/profile name define package behavior.

## Deferred by product decision

These items are intentionally outside the current implementation program:

- standalone release/package-boundary work;
- expansion of benchmark corpora, metrics, baselines, or performance gates;
- inventory-constrained placement, availability, sourcing, or pricing;
- a product viewer;
- a TUI;
- vision or image-to-assembly workflows;
- learned or creative-generation modes; and
- general-orientation generation beyond the current yaw-only generator.

Existing benchmark/evaluation utilities and BOM output are preserved, but no
new work is planned in the deferred benchmark or inventory areas.
