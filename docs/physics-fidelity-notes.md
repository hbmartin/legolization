# Physics fidelity notes

State of the RBE's physical assumptions after the v5 fidelity
workstream: what each new switch models, what was measured, and where
the capacity constants could come from next. Written 2026-07-19 on
branch `roadmap-v5`; companion to `ROADMAP.md` "Physics fidelity
backlog" and the v5 progress notes.

## 1. The v5 switches (`SolverConfig`)

- **`torque_z`** — a sixth residual row (yaw torque) per brick.
  StableLego (paper and release) models fx/fy/fz/τx/τy only; the yaw
  row makes horizontal knob presses meaningful for twist loads.
  Gravity never loads it (its yaw lever is identically zero), so
  gravity-only verdicts are unchanged by construction — the row starts
  to matter once lateral force chains (arches, side-supported spans)
  or external horizontal loads exist. Side contacts switch from two
  vertical-extreme press generators to four (transverse, vertical)
  corner generators: without the yaw row a horizontal press's torque
  coefficient is linear in z alone, so two z-extremes span everything;
  with it the coefficient is also linear in the transverse coordinate,
  and a nonnegative distribution over the shared-face rectangle
  reaches exactly the cone of its four corners. `SideContact.t_lo/t_hi`
  carry the transverse extent for this.
- **`paper_knob_rule`** — per-knob contact points per the StableLego
  *paper*: 1xX cavities pinch at four points, 2xX at three, and on
  min-dimension-3+ bodies edge knobs take three while interior knobs
  take four. The release (and our default) uses the uniform per-brick
  rule. **Provably inert for the shipped catalog** — no part has min
  footprint dimension >= 3 (pinned by test); it exists so future wide
  parts get the exact rule.
- **`rotate_contact_pattern`** — rotates the contact pattern with the
  gripping brick's yaw. The release keeps the asymmetric three-point
  triangle axis-aligned regardless of orientation, so **the same
  physical structure scores differently built rotated 90°** — measured
  on a single-knob 2x4 cantilever: max_score 0.0792 along x vs 0.1080
  rotated, a 36% verdict distortion. The flag restores exact rotation
  invariance (the four-point diamond is rotation-invariant as a set,
  so only three-point engagements change).
- **`ground_pull`** — `True` (default) keeps StableLego's
  baseplate-style ground whose studs can pull down; `False` models
  bricks resting loose on a table (ground pushes, never pulls): the
  tipping-column pin flips to unstable exactly as physics says. Ground
  drag variables keep their indices and simply carry no force entries,
  so scoring and capacity machinery are untouched.
- **`analyze(..., extra_masses={brick_id: kg})`** — per-brick external
  load applied at the brick centroid, torque-free like gravity. This is
  the hook for Liu et al.'s virtual-brick insertion check (a ~1 kg
  press on the just-placed chunk; arXiv:2408.10162 §IV-D) and for
  payload analyses mirroring StableLego's `external_weight` fixtures.

Both engines (batch `model.py` and warm `prefix.py`) implement every
switch; dual-engine plan equality is pinned with `torque_z` on.
`links.py` reads the row layout from the model it builds, so a future
default flip cannot desynchronize the artificial-link QP.

### v7 consistency corrections

`build_model_from_config(layout, config)` is now the production
construction boundary for every cold RBE consumer. `analyze`,
candidate maximin scoring, Luo's capacity gate, link localization, and
direct removal rescue can no longer silently fall back to
`build_model` defaults. The low-level builder remains available for
tests and deliberate release-physics experiments; production callers
must provide their effective `SolverConfig`. Rotation-invariance and
table-mode rescue regressions enforce the boundary.

The side-contact transverse interval now describes physical face
edges, not occupied-cell centers: a face spanning transverse columns
`lo..hi` carries `t_lo = lo - 0.5` and `t_hi = hi + 0.5`. This matters
only when `torque_z` asks for four face-corner generators, but it is
load-bearing there: a one-cell-wide face now has two distinct yaw
levers instead of duplicate zero-width generators. The recalibrated
`torsion-bridge` fixture (18-cell dog-leg arm) pins a nontrivial
seed-0 kollsker score move, 0.202533 → 0.227176, when the yaw row is
enabled.

## 2. BrickFEM assessment (Pletz & Drvoderic 2023, engrXiv 10.31224/2898)

Read in full for this workstream. What it is: an MIT-licensed Python
package that auto-generates, meshes, runs, and evaluates **Abaqus**
models of small brick sets (regular bricks, plates, tiles, base-plates
of any size; 1961-patent geometry with inner-tube ribs). Its key trick
is establishing the clamp without simulating assembly: a *widen* step
displaces cavity faces out of overlap, a *contact* step engages
surface-to-surface contact, and a *free* step releases the stud faces
— then a static (implicit) or dynamic (explicit) load step runs, with
loads from brick-set reference points or rigid spheres/cylinders.
Penalty contact with a configurable friction coefficient (default
mu = 0.2); linear-elastic material (tower example peaks near 230 MPa
locally).

What it is **not**: it publishes no calibrated clutch-force table — no
per-stud pull-off newtons, no shear capacity, no moment capacity — and
its own conclusion positions it as a tool to "improve the analytical
models for the stability of Lego designs", i.e. the calibration is
left to whoever runs it. It needs commercial Abaqus (we have none),
handles only a few bricks per model (full solid mesh), and implements
no brick rotation.

**Consequence for us:** `T_CAPACITY_N = 0.98` (StableLego's
experimentally sourced single friction capacity) remains the
best-sourced constant available, and a per-contact-type capacity flag
is **not warranted yet** — there is no data to fill it with. Recorded
revisit trigger: access to an Abaqus seat (or a port of BrickFEM's
clamping-step trick to an open FE code such as CalculiX).

## 3. The BrickFEM calibration path (when runnable)

Each item is one small `assembly` dictionary in BrickFEM's input
format; together they would replace the single T constant with a
per-contact-type capacity table:

1. **Single-stud pull-off** — 1x1 on 1x1, rigid pull on the top brick;
   peak reaction vs. displacement calibrates T for the FOUR_POINT
   pattern (compare 0.98 N).
2. **Wide-cavity pull-off** — 1x1 under a 2x2 (one stud engaged, three
   contact points): does the 3-point pattern deserve its own T?
3. **Single-stud shear** — lateral rigid-cylinder push at the joint:
   calibrates the knob-press capacity our K_DIRECTIONS variables
   currently leave unbounded.
4. **Single-stud moment** — offset vertical pull: per-knob moment
   capacity, validating the lever geometry of the contact patterns.
5. **Friction sensitivity** — repeat (1) across mu in [0.1, 0.3] to
   bound how much of T is friction vs. interference fit.

A SNOT lateral variant of (1) would need brick rotation, which
BrickFEM lacks — noted as a limitation, not a blocker for the vertical
table.

## 4. Pointers

- `src/legolization/stability/constants.py` — every constant the
  calibration would touch, with StableLego provenance comments.
- `tests/test_stability.py` — the analytic pins guarding each switch
  (rotation variance, table-mode tipping, extra-mass overload,
  torque-z row census).
- `docs/performance-testing.md` §5 — engine/drift policy the switches
  must respect.
- BrickFEM: github.com/mpletz/BrickFEM (MIT).
