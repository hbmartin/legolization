# PR #18 review

PR: [hbmartin/legolization#18](https://github.com/hbmartin/legolization/pull/18)

Verdict: **Request changes**

This review found one P1, seven P2, and three P3 issues. It covered the full
44-file, +3,625/-703 diff with three independent review passes over:

- geometry, SNOT, catalog, graph, and LDraw behavior;
- stability solvers, instruction sequencing, deadlines, and connectivity;
- APIs, CLI validation, profiling, telemetry, compatibility, documentation,
  and tooling.

No CodeRabbit comments, summaries, or findings were used as review evidence.

## Findings

### P1 — Sparse LDraw coordinates can cause unbounded CPU consumption

Location:
[`src/legolization/instructions/blocking.py:115`](../src/legolization/instructions/blocking.py#L115)

`_outward_ray_blockers` walks one grid coordinate at a time until it reaches
the model's global bounds. LDraw imports permit large sparse coordinates, so
a two-piece file can force billions of empty occupancy lookups during default
smart sequencing.

A lateral tile at `x=0` and an unrelated brick at `x=N` scales linearly with
`N`. Measured timings for `N=1_000`, `10_000`, and `100_000` were
approximately 0.0003, 0.0029, and 0.0292 seconds. A valid
`N=1_000_000_000` input can therefore hang for minutes.

The ray should iterate indexed occupied cells on the relevant outward
half-ray rather than every intervening coordinate. A regression test should
use a billion-stud gap and assert a bounded number of occupancy lookups.

### P2 — Best-of-five connectivity repair ignores the strategy deadline

Locations:

- [`src/legolization/placement/layered/engine.py:163`](../src/legolization/placement/layered/engine.py#L163)
- [`src/legolization/placement/merge.py:313`](../src/legolization/placement/merge.py#L313)

`LayeredStrategy.place` invokes
`improve_connectivity(..., bridge_draws=5)` after deadline-aware tiling but
passes no remaining deadline. The connectivity loop can consequently perform
five full copy/split/remerge/graph passes per retry after the cooperative
budget has expired.

Connectivity repair was already outside the budget, but this PR multiplies
that unbudgeted tail by five for every layered strategy. The absolute
deadline should be passed through and checked before every draw. An
expired-deadline regression test should produce zero connectivity attempts.

### P2 — Count trajectories are not ordered by execution time

Locations:

- [`scripts/count_trajectory.py:80`](../scripts/count_trajectory.py#L80)
- [`src/legolization/pipeline.py:157`](../src/legolization/pipeline.py#L157)
- [`src/legolization/pipeline.py:476`](../src/legolization/pipeline.py#L476)

`_rows` groups values by phase name rather than emission sequence. Even the
repair phase is reversed: `pipeline.repaired` is emitted inside
`_place_and_repair` before `pipeline.placed`, but the report prints placed
before repaired.

A forced hollow-restore run produced:

```text
tiled#1, tiled#2, compacted#1, compacted#2,
connected#1, connected#2, placed#1, restored#1, remerged#1
```

The actual order is pass-one tiling/compaction/connectivity/placement,
followed by pass two and restoration. Printed deltas can therefore be
attributed to the wrong phase.

Telemetry should record atomic events carrying a global sequence number
instead of independently ordered lists for each gauge name. Tests should
cover both a forced repair and a forced hollow-restore pass.

### P2 — The trajectory omits promised component and stability gauges

Locations:

- [`src/legolization/placement/layered/engine.py:156`](../src/legolization/placement/layered/engine.py#L156)
- [`ROADMAP.md:75`](../ROADMAP.md#L75)
- [`scripts/count_trajectory.py:3`](../scripts/count_trajectory.py#L3)

The trajectory script claims to report brick, component, and stability
readings after every phase, and the roadmap says the layered engine emits
them. In reality, the engine records:

- only brick counts after tiling;
- only brick counts after compaction;
- brick count and component count after connectivity;
- no stability value for any of these phases.

A real layered hollow-restore run showed `components=None, stable=None` for
tiling and compaction, and `stable=None` after connectivity. The committed
drift report's pre-connectivity fragmentation claims therefore cannot be
reproduced from the advertised tool alone.

The engine should emit the documented gauges, and the smoke test should
assert their values rather than only checking that phase labels exist.

### P2 — SHA stamping fails in Git worktrees

Location:
[`src/legolization/telemetry.py:143`](../src/legolization/telemetry.py#L143)

`git_sha` assumes `.git` is a directory containing `HEAD`. In a linked Git
worktree, `.git` is a file containing a `gitdir:` indirection.

A real `git worktree add` reproduction returned:

```text
dot_git_is_file=True
git_sha=None
```

The new schema-2 CLI profiles and trajectory artifacts therefore lose their
code-state identity in common worktree-based development and CI
environments.

The implementation should parse `gitdir:` files, including relative paths
and common refs, or use `git rev-parse`. Tests should cover linked worktrees
and packed refs.

### P2 — Kollsker performs expensive enumeration before checking an expired deadline

Location:
[`src/legolization/placement/layered/kollsker.py:125`](../src/legolization/placement/layered/kollsker.py#L125)

`_solve_component` materializes every rectangle candidate before calling
`_time_limit`. An already-expired sweep can therefore still spend
significant CPU and memory enumerating candidates for every remaining
component.

A monkeypatched expired-deadline reproduction confirmed that candidate
enumeration runs before the MILP is skipped.

The deadline should be checked before enumeration and again after it. Where
possible, `candidate_limit` should be enforced incrementally instead of only
after materializing the full candidate list.

### P2 — Profiling artifacts do not identify their effective mesh input

Locations:

- [`scripts/profile_pipeline.py:56`](../scripts/profile_pipeline.py#L56)
- [`scripts/profile_pipeline.py:153`](../scripts/profile_pipeline.py#L153)
- [`scripts/count_trajectory.py:135`](../scripts/count_trajectory.py#L135)
- [`scripts/eval_corpus.py:89`](../scripts/eval_corpus.py#L89)

The profile payload records `target_studs` from the command line but omits
the up-axis. For corpus names, `_resolve_grid` ignores those command-line
mesh options and `eval_corpus.model_grid` uses manifest settings instead.

For example, profiling `spot --target-studs 16` can actually voxelize the
manifest's `spot@24` configuration while recording `target_studs: 16`.
Conversely, two file-mesh runs using different `--up` values can carry
identical recorded identities despite different voxelizations. The new
trajectory payload omits target resolution entirely.

`_resolve_grid` should return the effective resolution metadata. Artifacts
should record the effective target size, up-axis, component filtering, and
preferably an input hash.

### P2 — The public Python telemetry bucket representation breaks compatibility

Location:
[`src/legolization/telemetry.py:50`](../src/legolization/telemetry.py#L50)

`SpanStats.buckets` changes from `dict[int, list[float]]` to
`dict[int, _Bucket]`. The JSON representation remains compatible, but the
Python telemetry API does not. Existing access such as:

```python
session.spans["x"].buckets[8][0]
```

now raises:

```text
TypeError: '_Bucket' object is not subscriptable
```

The base test explicitly pinned the former list representation. The public
container shape should be preserved, or the new bucket type should provide
a backward-compatible sequence interface and an explicit migration path.

### P3 — `bridge_draws <= 0` silently disables connectivity repair

Location:
[`src/legolization/placement/merge.py:320`](../src/legolization/placement/merge.py#L320)

Zero or negative values execute no candidate draws, consume the retry loop,
and return a disconnected layout without an error. `improve_connectivity`
should validate that `bridge_draws` is positive.

### P3 — Negative connectivity retry overrides are accepted

Locations:

- [`src/legolization/pipeline.py:90`](../src/legolization/pipeline.py#L90)
- [`src/legolization/placement/registry.py:58`](../src/legolization/placement/registry.py#L58)

The documentation reserves zero for disabling connectivity repair, but
negative `connectivity_fail_max` values are forwarded unchanged and also
silently disable it because the loop's initial `0 < fail_max` check fails.

The public configuration should reject values below zero.

### P3 — The trajectory CLI lacks basic argument validation

Location:
[`scripts/count_trajectory.py:101`](../scripts/count_trajectory.py#L101)

The new diagnostic accepts unknown strategies, nonpositive target sizes,
and negative `--fail-max` values. Unknown strategies and invalid target
sizes can end in tracebacks, while a negative fail-max silently behaves like
the documented zero ablation.

The parser should use registered strategy choices, a positive target-size
type, and a nonnegative fail-max type.

## Validation

The following checks passed on PR head
`a745b6d46250eb56de5b6f60b8642ce890b875b8` against base
`8e27b59c576417bb3fba64426f4b2d596aa70c5b`:

```text
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv run ty check src tests
uv run pyrefly check src tests
uv run lizard --languages python --CCN 18 src tests
uv lock --check
git diff --check origin/main...HEAD
```

Full test result:

```text
520 passed in 236.10s
```

Targeted reproductions covered:

- sparse-coordinate blocker scaling;
- linked-worktree SHA resolution;
- legacy telemetry bucket indexing;
- repair and hollow-restore trajectory ordering;
- missing layered phase gauges;
- expired-deadline candidate enumeration;
- negative connectivity and best-of-k inputs;
- effective versus recorded mesh profiling options.

No repository files were modified during the review itself, and the
pre-existing untracked files were left untouched.

## Pre-existing observations not attributed to PR #18

Two additional concerns found during the independent passes reproduce on
`origin/main`, so they are not counted as PR #18 regressions:

1. Imported LDraw models silently accept several placement-only CLI flags
   whose values are ignored.
2. Same-layer SNOT carrier/cladding chunks force instruction planning into
   the disassembly rescue path, producing unnecessary cold solver work.

These should be tracked separately from this PR's merge decision.
