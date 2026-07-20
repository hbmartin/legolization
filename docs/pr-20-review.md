# PR #20 review-feedback triage

Date: 2026-07-19

## Outcome

The PR #20 feedback triage evaluated 21 unique review records against the
current branch. The decisions are persisted in
[`triage_decisions.db`](../triage_decisions.db), using
[`reviews_triage/pr-20-feedback-20260719-182500.json`](../reviews_triage/pr-20-feedback-20260719-182500.json)
as the source of truth. Every stored record identifies `codex` as the
reviewing agent.

| Classification | Count | Summary |
| --- | ---: | --- |
| Should be fixed | 11 | One warm-solver failure, one insertion-audit gap, and grouped API, metadata, performance, documentation, and typing improvements |
| Already fixed | 2 | Duplicate reports of the vectorized `unsupported_ratio` implementation |
| High-level | 6 | Automated walkthroughs, status summaries, a service notice, a security-dashboard flag without repository details, and a validation reminder |
| Should not be fixed | 2 | Heart step-count synchronization and seam-conditioned connectivity-graph construction |

This triage did not implement the 11 suggested fixes.

## Should be fixed

### Severity 3: restore the `torque_z` warm path

`PrefixSolver._extract` reads `self._rpb` residual values, but its strict
near-boundary `zip` still supplies only three force tolerances and two torque
tolerances. With `torque_z=True`, the sixth residual causes `zip(...,
strict=True)` to raise. The broad warm-solver exception handler then rebuilds
the model and silently uses the cold solver.

A behavioral check on the heart example recorded three
`stability.prefix.warm_fail` spans and three
`stability.prefix.rebuild` spans, confirming the review finding.

Implementation plan:

1. Build the tolerance tuple from three force tolerances and
   `self._rpb - 3` torque tolerances.
2. Preserve the existing near-boundary behavior with and without
   `torque_z`.
3. Extend the dual-engine regression to assert that a normal
   `torque_z=True` warm run emits no warm-failure or rebuild spans, instead of
   checking verdict equivalence alone.

This is the highest-priority change because the current regression passes
while the feature under test has fallen back to the comparison engine.

### Severity 2: audit subassembly attachment presses

The insertion-fragility audit applies virtual press mass to
`step.brick_ids`. Attach steps intentionally have no direct brick IDs, so a
press-fragile subassembly attachment is never audited.

Implementation plan:

1. For attach steps, load the IDs from
   `subs[step.attaches].brick_ids`; otherwise retain `step.brick_ids`.
2. Use the resolved IDs for the stability analysis.
3. Add a regression with a statically stable but press-fragile attachment
   step.

### Severity 1: CLI, metadata, documentation, and performance

The remaining functional improvements can be implemented as a small,
independent batch:

- Raise an explicit error when a user-supplied `--baseline` path is missing,
  while preserving the optional committed-baseline behavior.
- Reject negative, infinite, and `NaN` insertion masses at the argument
  boundary.
- Persist `config.milp_bridge` in the trajectory payload's run metadata.
- Return or propagate the grid loaded by the restart race so the winning run
  does not load and voxelize the same input a second time.
- Match yaw `270` explicitly and reject non-orthogonal values instead of
  treating every unmatched yaw as a 270-degree rotation.
- Replace the periodic evaluation examples with copy-pasteable
  `uv run python scripts/...` commands and include the required flagship
  input for the insertion audit.

Each behavior change should carry a focused regression.

### Severity 0: maintenance

- Share a named constant between the `--restarts` default and its sweep
  validation.
- Correct the bridge-repair docstring: the MILP candidate competes with the
  random candidates on the same key and does not preempt them. The thread is
  marked addressed, but the contradictory text remains in the current file.
- Add `-> None` to
  `test_support_warnings_aggregate_consecutive_runs`.

## Already fixed

Two reviewers independently requested NumPy vectorization of
`unsupported_ratio`. The current implementation already constructs a filled
mask and computes unsupported voxels with array slicing, so both comments are
fixed by the same landed change.

## High-level feedback

Six records are informational rather than concrete fix requests:

- automated PR walkthrough and review rollups;
- the author's prior-remediation status report;
- the Gemini review summary and service-deprecation notice;
- the Superagent contributor flag, which links to a dashboard but supplies no
  repository-level finding;
- the reminder to run the required Python validation.

The concrete inline and outside-diff findings referenced by review summaries
were classified separately.

## Should not be fixed

### Do not force the heart step counts to match

The two reported counts represent different scopes:

- the CLI reports eight total instruction-plan steps;
- the plan contains six main-model steps and two subassembly steps;
- the root `heart.ldr` contains six `0 STEP` markers for the six main-model
  steps.

Changing the README output to six would misreport `PipelineResult.step_count`.
Adding two root-model markers would conflate subassembly construction with
main-model serialization. If additional clarity is useful, document the two
scopes rather than forcing equality.

### Do not skip connectivity graphs merely because a layer has no seams

`build_context` uses the graph both for seam priorities and for
`grounded_below`. The latter is consumed independently by the Fast, Beauty,
and Kollsker placement paths to distinguish grounded supports from floating
ones. Making graph construction conditional on `seams` would silently disable
support-aware placement on seam-free layers.

The performance concern is reasonable, but the safe direction is incremental
or cached connectivity state backed by benchmarks and grounding regressions,
not the proposed seam-conditioned shortcut.

## Recurrence prevention

Dependency-cruiser is designed for JavaScript/TypeScript dependency graphs and
is not applicable to this Python repository. Semgrep is the useful guard here:

- Add a path-scoped rule for
  `src/legolization/stability/prefix.py` that flags a strict `zip` pairing a
  dynamic solver slice with a tolerance sequence whose arity contains a
  hard-coded integer.
- Flag broad `except Exception` fallbacks in warm-solver paths unless the
  fallback is observable through telemetry and protected by a regression that
  proves the expected path remains warm.

These static checks complement, rather than replace, the telemetry assertion
recommended for the `torque_z` regression.

## Validation

Validation performed during triage:

| Command | Result |
| --- | --- |
| `uv run ruff check .` | Passed |
| `uv run ruff format --check .` | Passed; 90 files already formatted |
| `uv run pytest` | Passed; 562 tests in 401.81 seconds |
| `uv run ty check` | Passed |
| `uv run pyrefly check src scripts tests` | Passed; zero errors |

A full-root `uv run pyrefly check` also inspected an unrelated untracked helper
at `papers PDFs/pdf-to-markdown/convert_pdfs_to_markdown.py` and reported its
missing optional `pdfplumber` import. The tracked project paths are clean.

## Next step

Reply with `y` in the Codex task to implement the 11 suggested fixes
automatically.
