# PR #19 review

PR:
[hbmartin/legolization#19](https://github.com/hbmartin/legolization/pull/19)

Verdict: **Request changes**

This review found four P1, eight P2, and two P3 issues. It covered the full
48-file, +2,892/-486 diff with three independent review passes over:

- stability model assembly, batch and incremental solvers, graph contacts,
  repair localization, and maximin scoring;
- layered placement, bridge synthesis, connectivity repair, support-aware
  scoring, deadlines, and performance limits;
- CLI seed restarts, instruction and subassembly auditing, evaluation
  baselines, artifact provenance, documentation, and project integration.

No CodeRabbit comments, summaries, or findings were used as review evidence.

## Findings

### P1 — The PR still targets `roadmap-v4` instead of `main`

PR #18 has already merged `roadmap-v4` into `main` as merge commit
`cd43583b9890a7be104b1584feae2f9d29e9c965`. PR #19 still names
`roadmap-v4` as its base, and that branch is now one merge commit behind
`main`.

Merging the PR in its current state updates the staging branch rather than
the repository's default branch. The PR should be retargeted to `main` before
code changes are merged. GitHub's `main...roadmap-v5` comparison still
contains the same 48 changed files; the head is 19 commits ahead and one
merge commit behind.

### P1 — Maximin scoring uses different physics from the configured analysis

Locations:

- [`src/legolization/compare.py:143`](../src/legolization/compare.py#L143)
- [`src/legolization/placement/luo.py:141`](../src/legolization/placement/luo.py#L141)
- [`src/legolization/stability/model.py:296`](../src/legolization/stability/model.py#L296)
- [`src/legolization/stability/solver.py:104`](../src/legolization/stability/solver.py#L104)

`SolverConfig.rotate_contact_pattern` now defaults to `True`, while the
lower-level `build_model` function keeps `rotate_contact_pattern=False` as
its default. `candidate_metrics` and `LuoStrategy._capacity` call bare
`build_model(layout)`, so their maximin calculations use the old contact
geometry. Those calls also ignore `torque_z`, `paper_knob_rule`, and
`ground_pull`.

This makes the main stability verdict, objective evaluation, reported
capacity, and maximin tie-break disagree. It can select the wrong restart or
strategy winner, and Luo can accept or reject a refinement under physics
different from the `analyze(..., self.solver_config)` result used beside it.

A reproduction using physically equivalent one-stud 2x4 cantilevers found:

```text
default configured analysis:
  unrotated max_score = 0.0792
  rotated   max_score = 0.0792

current candidate maximin:
  unrotated capacity = 0.902384 N
  rotated   capacity = 0.874160 N

configured maximin:
  unrotated capacity = 0.902384 N
  rotated   capacity = 0.902384 N
```

A more heavily loaded rotated cantilever crossed the verdict boundary:

```text
analyze(layout):                  stable=True,  max_score=0.738533
solve_model(build_model(layout)): stable=False, max_score=1.0
default-model maximin:            capacity=-0.109107 N
configured-model maximin:         capacity= 0.256237 N
```

Model construction from a `SolverConfig` should be centralized and used by
every production caller. Tests should require both verdict and capacity
invariance across equivalent rotations.

### P1 — Direct removal rescue ignores every new physics option

Location:
[`src/legolization/stability/prefix.py:914`](../src/legolization/stability/prefix.py#L914)

The large-component direct rescue path runs:

```python
_solve_lp_highspy(build_model(sub_layout), self._config)
```

The model is therefore assembled with `torque_z=False`,
`paper_knob_rule=False`, `rotate_contact_pattern=False`, and
`ground_pull=True`, regardless of `self._config`. Scoring the resulting
solution with `self._config` cannot repair the wrong constraint matrix.

Forcing the documented direct path with
`rescue_direct_min_bricks=1` on the top-heavy table-mode fixture produced:

```text
analyze(layout, ground_pull=False):
  stable=False, max_score=1.0

RemovalSolver.probe_without(()):
  stable=True, max_score=0.0128125
```

With the default threshold the same defect applies automatically to contact
components of at least 200 bricks. The disassembly rescue can accept an
unsafe removal state and emit an ordering inconsistent with final
verification. The model should be assembled with the exact same switches as
`analyze`.

### P1 — A one-element `--seeds` override is silently ignored

Locations:

- [`src/legolization/main.py:448`](../src/legolization/main.py#L448)
- [`src/legolization/main.py:733`](../src/legolization/main.py#L733)

`_race_seeds` returns the original config whenever the effective seed list
has length one. For an explicit one-element list, the original config still
contains `args.seed`, not the requested override:

```text
command:             --seeds 7
parsed seed list:    (7,)
effective config:    PipelineConfig(seed=0)
```

This contradicts the CLI help, which says `--seeds` overrides `--restarts`
for a single strategy. A one-seed explicit override is also allowed with
profiling, but `_write_profile` records `args.seed`; both execution and
metadata need to use the effective seed.

The length-one path should return `replace(config, seed=seeds[0])`, and a CLI
regression test should verify both the generated model and profile metadata.

### P2 — Non-improving bridge callbacks can defeat `fail_max` forever

Locations:

- [`src/legolization/placement/merge.py:335`](../src/legolization/placement/merge.py#L335)
- [`src/legolization/placement/merge.py:367`](../src/legolization/placement/merge.py#L367)

`improve_connectivity` assigns every non-`None` bridge result to `best`
without verifying that its component count is lower than the current count.
The random-candidate path performs this check, but the bridge path does not.

The acceptance block then replaces the layout and resets `failures` to zero.
An equal-component bridge can consequently loop forever, while a
worse-component bridge can actively regress connectivity.

A reproduction with two adjacent, non-mergeable `tile_1x1_snot` parts and:

```python
bridge=lambda layout, region, grid: layout.copy()
```

was still running after 0.5 seconds despite `fail_max=1`. The caller should
compute `synthesized_components` and require it to be less than `components`
before considering the candidate.

### P2 — Bridge limits are checked only after full candidate enumeration

Locations:

- [`src/legolization/placement/layered/bridge.py:172`](../src/legolization/placement/layered/bridge.py#L172)
- [`src/legolization/placement/layered/engine.py:171`](../src/legolization/placement/layered/engine.py#L171)

`BridgeSynthesizer._solve_component` fully materializes
`enumerate_layer_rects(...)` before checking either `candidate_limit` or the
remaining deadline. The most expensive pre-solver work is therefore outside
both advertised limits.

Measurements with `total_time_s=slab_time_s=1e-9` found:

```text
40x40 region:  28,835 candidates, approximately 0.37 seconds
60x60 region:  67,235 candidates, approximately 0.89 seconds
100x100 enumeration: 192,035 candidates
```

The calls only returned `None` after materializing more than the default
20,000 candidates. Connectivity repair can repeat this work through
`fail_max` retries.

The layered engine also creates a fresh default 10-second synthesizer after
deadline-aware tiling, discarding the strategy's remaining deadline.
Candidate generation should stop incrementally at the first limit, and
bridge synthesis should consume the remaining placement budget rather than
starting a new one.

### P2 — `torque_z=True` silently cold-solves every stable prefix

Location:
[`src/legolization/stability/prefix.py:713`](../src/legolization/stability/prefix.py#L713)

With yaw torque enabled, `t_vals` has six entries, but the strict zip in
`PrefixSolver._extract` still provides five tolerances:

```python
(config.tol_force,) * 3 + (config.tol_torque,) * 2
```

`zip(..., strict=True)` raises `ValueError`. The broad exception handler
rebuilds the warm solver and cold-falls back, so correctness tests pass while
the incremental engine is effectively disabled.

Telemetry on one grounded probe showed:

```text
torque_z=False:
  stability.prefix.probe

torque_z=True:
  stability.prefix.probe
  stability.prefix.rebuild
  stability.prefix.warm_fail
  stability.analyze
  stability.lp
```

The torque tolerance count should be `self._rpb - 3`. The dual-engine test
should also assert that the warm path did not enter a failure fallback.

### P2 — Yaw side-contact corners use face centers, not physical edges

Locations:

- [`src/legolization/graph.py:251`](../src/legolization/graph.py#L251)
- [`src/legolization/stability/model.py:337`](../src/legolization/stability/model.py#L337)
- [`src/legolization/stability/prefix.py:639`](../src/legolization/stability/prefix.py#L639)

The vertical extent is converted from face centers to physical edges with
`z_lo` and `z_hi + 1`. The transverse extent is not:
`t_lo` and `t_hi` are the minimum and maximum face-center coordinates.

For two side-by-side 1x1 bricks, the physical face spans `[-0.5, 0.5]`
transversely, but the graph reports:

```text
t_lo = 0.0
t_hi = 0.0
```

Under `torque_z=True`, the intended four corner generators collapse into two
duplicate pairs at the center, and none has a yaw lever. Wider faces lose
half a stud of lever arm at each edge.

The transverse bounds should be `min(center) - 0.5` and
`max(center) + 0.5`, with equivalent batch and incremental solver tests.

### P2 — Insertion auditing skips subassembly attachment steps

Locations:

- [`scripts/check_instructions.py:73`](../scripts/check_instructions.py#L73)
- [`scripts/check_instructions.py:93`](../scripts/check_instructions.py#L93)
- [`src/legolization/instructions/sequencer.py:99`](../src/legolization/instructions/sequencer.py#L99)

Attach steps correctly add the subassembly's bricks to `audit_layout`, but
they intentionally have `step.brick_ids == ()`. The insertion condition
requires `step.brick_ids`, and its extra-mass map is built from that same
empty tuple. Every subassembly attachment is therefore excluded from the
new audit.

A focused reproduction built a 1x4 cantilever separately and attached it to
a one-stud tower:

```text
check_steps attach flags: []
analyze(attached layout, extra_masses={beam: 1.0}):
  stable=False, max_score=1.0
```

This false-negative is especially exposed now that subassemblies default to
on. For an attach step, the just-placed chunk should be the attached
subassembly's brick ids. The regression suite currently covers only an
ordinary brick-placement step.

### P2 — `--insertion-mass-kg` accepts invalid physical values

Location:
[`scripts/check_instructions.py:171`](../scripts/check_instructions.py#L171)

The option uses unrestricted `type=float`. Negative values become negative
gravitational mass and invert the intended press load. `nan` and `inf` reach
SciPy and terminate with an uncaught traceback:

```text
ValueError: b_ub must not contain values inf, nan, or None
```

The argument should use a finite, strictly positive parser like the main
CLI's `_positive_float`. Tests should cover negative, zero, `nan`, and
infinite values.

### P2 — Restart-race flags are silently ignored for LDraw imports

Locations:

- [`src/legolization/main.py:536`](../src/legolization/main.py#L536)
- [`src/legolization/main.py:622`](../src/legolization/main.py#L622)

This PR makes `--jobs`, `--timeout`, and `--seeds` valid for
single-strategy restart races and adds `--restarts`. `_validate_ldraw_args`
does not reject any of them, while the import path returns before
`_race_seeds` can run.

On a valid one-brick LDraw model, each of the following succeeded and wrote
the same output without warning:

```text
--seeds 9
--restarts 10
--jobs 4
--timeout 0.01
```

These controls should be rejected for `.ldr` and `.mpd` inputs, matching the
existing validation policy for placement-only flags.

### P2 — `bond_weight=0` also disables the independent grounding reward

Locations:

- [`src/legolization/placement/layered/kollsker.py:155`](../src/legolization/placement/layered/kollsker.py#L155)
- [`src/legolization/placement/layered/kollsker.py:170`](../src/legolization/placement/layered/kollsker.py#L170)

The new reward array combines:

```python
bond_reward + ground_weight * grounding_gain / area
```

The stage-two objective then multiplies the complete expression by
`bond_weight`. The valid pipeline setting `milp_bond_weight=0` therefore
silently disables `ground_weight`, even though grounding is a separately
validated support-aware tuning knob.

On a crafted 3x3 equal-count cover:

```text
bond_weight=0, ground_weight=0:   grounding score 2/3
bond_weight=0, ground_weight=100: grounding score 2/3
bond_weight=1, ground_weight=100: grounding score 5/6
```

The objective should use separate terms:

```text
-bond_weight * bond_reward
-ground_weight * grounding_gain / area
```

### P3 — Trajectory JSON omits the bridge ablation setting

Location:
[`scripts/count_trajectory.py:137`](../scripts/count_trajectory.py#L137)

`--no-milp-bridge` changes `PipelineConfig.milp_bridge` and adds a filename
suffix, but `payload["run"]` does not record the effective value.

Renaming, aggregating, or consuming the JSON independently of its filename
makes bridge-on and bridge-off measurements indistinguishable. The effective
boolean should be included in the run metadata and covered by
`tests/test_count_trajectory.py`.

### P3 — Evaluation artifacts and completion claims are inconsistent

Locations:

- [`ROADMAP.md:16`](../ROADMAP.md#L16)
- [`ROADMAP.md:111`](../ROADMAP.md#L111)
- [`ROADMAP.md:150`](../ROADMAP.md#L150)
- [`docs/v5-pending-measurements.md:8`](v5-pending-measurements.md#L8)
- [`eval/baselines/scorecard.json`](../eval/baselines/scorecard.json)
- [`scripts/eval_corpus.py:357`](../scripts/eval_corpus.py#L357)

The branch contains several conflicting completion signals:

- the roadmap says evaluation owns one committed baseline per corpus kind;
- `docs/v5-pending-measurements.md` correctly says
  `scorecard-mesh.json` does not exist;
- the committed synthetic baseline contains `unsupported_ratio` in zero of
  its 11 rows despite the newly documented scorecard schema;
- a mesh-only run currently loads the synthetic baseline when the mesh
  baseline is absent, producing no meaningful mesh regression comparison;
- the roadmap records 547 tests, while the branch currently collects 551.

The mesh baseline should either be committed or described consistently as
deferred. The synthetic baseline should be regenerated after the
`unsupported_ratio` schema change, and the completion/test-count wording
should be updated.

## Validation

Repository-wide validation at
`4c572ec414c7868fc1156750f76b95c3c884f27e`:

```text
git diff --check
  passed

uv run ruff format --check .
  90 files already formatted

uv run ruff check .
  passed

uv run pytest
  551 passed in 301.24 seconds

uv run ty check src tests
  passed

uv run pyrefly check src tests
  0 errors
```

An additional focused suite across bridge synthesis, comparison, insertion
auditing, prefix solving, stability, evaluation, and trajectory behavior
passed:

```text
131 passed in 17.18 seconds
```

An unrestricted `uv run ty check` also traverses the tracked
`papers PDFs/pdf-to-markdown` utility and reports its undeclared
`pdfplumber` import. The repository's documented and CI-scoped
`uv run ty check src tests` command passes, so that pre-existing utility
issue is not attributed to PR #19.

At the final GitHub snapshot:

- Python 3.12 and 3.13 lint/test jobs were still pending;
- Socket project and pull-request security checks passed;
- the Superagent security scan passed;
- the external contributor-trust check failed.

No repository files or GitHub state were changed during the review itself.

