# PR #17 Review

- Pull request: [hbmartin/legolization#17](https://github.com/hbmartin/legolization/pull/17)
- Reviewed head: `5b4c6cb0fa2b3a7c717907ba6de48e05ad1679c6`
- Base branch: `main`
- Review status: **Request changes**
- Findings: **3 P1, 10 P2, 2 P3**

This was a careful, independent review of the 58-file PR (`+7,082 / -400`). Three
subagents reviewed separate subsystem groups: stability and physics;
instructions, LDraw I/O, rendering, and booklet generation; and pipeline, CLI,
evaluation, and placement. CodeRabbit was not used or relied upon.

## Findings

### P1 — Preserve positional API compatibility

New defaulted fields were inserted in the middle of exported dataclasses:

- [`Candidate.seed`](../src/legolization/compare.py#L75) was inserted before the
  previous `result` field.
- [`PipelineConfig`](../src/legolization/pipeline.py#L58) received SNOT and MILP
  fields among its existing fields.
- [`PipelineResult.snot_added`](../src/legolization/pipeline.py#L101) was inserted
  before the previous `plan` field.
- [`MeshOptions.colour_mode`](../src/legolization/mesh.py#L58) was inserted before
  the previous `fill` field.

Existing positional calls are therefore silently reinterpreted or fail. For
example, the former call
`Candidate("greedy", 1.0, None, None, "boom")` now produces `seed=None`,
`metrics="boom"`, `error=None`, and `ok=True`. A former call such as
`MeshOptions(32, None, "z", 7, False, True)` now treats `False` as
`colour_mode` and raises. A positional `PipelineResult` plan can similarly
become `snot_added`, leaving `plan` as `None`.

Append new defaulted fields after the previous field order, or provide explicit
compatibility initializers.

### P1 — Disambiguate upright and SNOT `3070b`

[`read_ldraw`](../src/legolization/ldraw_in.py#L71) builds a one-to-one reverse
part dictionary. Both `tile_1x1` and `tile_1x1_snot` use LDraw part `3070b`, so
the later SNOT entry wins. A horizontal tile written by this project then fails
to re-import with `sideways part in an unsupported orientation`.

This breaks round-tripping for a common catalog part. Map part codes to
candidate definitions and select the correct candidate from orientation and
category.

### P1 — Enforce the sequential sweep deadline

The [`jobs == 1` sweep path](../src/legolization/compare.py#L245) launches every
strategy/seed candidate without checking one outer deadline. The per-candidate
call at [`compare.py:270`](../src/legolization/compare.py#L270) also grants each
candidate a fresh full timeout, while greedy and Luo do not consume the budget
cooperatively.

In a mocked two-seed reproduction where each candidate took 50 ms,
`timeout_s=0.01` still took about 110 ms and returned both candidates normally.
That contradicts the seed-sweep timeout behavior advertised by
[`--seeds`](../src/legolization/main.py#L152).

Create one monotonic sweep deadline, stop launching candidates once it expires,
and pass only the remaining budget to each candidate.

### P2 — Distinguish the exterior from hollow cavities for SNOT

[`placement/snot.py:61`](../src/legolization/placement/snot.py#L61) accepts every
empty neighboring cell as a mounting site, including cells in enclosed hollow
interiors. Tuple sorting also favors negative faces. In a closed hollow
5-by-5-by-12 shell reproduction, the algorithm mounted ten tiles inside the
cavity while exterior faces remained unclad.

Flood-fill empty space from the model boundary to classify exterior space, then
emit or prioritize one exterior-facing site for each wall window.

### P2 — Do not create phantom SNOT side contacts

The SNOT tile's catalog definition uses a conservative three-cell collision
prism in [`catalog.py:253`](../src/legolization/catalog.py#L253), while
[`graph.py:214`](../src/legolization/graph.py#L214) treats all occupied-cell
faces as physical side contact. A correctly mounted bracket/tile pair therefore
gets both the dedicated lateral-knob contact and a generic three-layer side
contact.

In the reproduction, the tile's drag changed from `0.000784 N` with only the
knob contact to `0.0003185 N` with the phantom face contact; minimum capacity
changed from `0.979216 N` to `0.9796815 N`.

Separate collision geometry from contact geometry, or exclude mounted SNOT
pairs from generic side-contact generation.

### P2 — Remove the 63-stud blocker limit

[`instructions/blocking.py:67`](../src/legolization/instructions/blocking.py#L67)
uses `max_reach=64` and `range(1, max_reach)`, so it scans only distances 1
through 63 even though supported grids can be much larger. In a reproduction,
a wall 64 studs beyond a tile was omitted and an impossible insertion was
approved.

Derive the ray extent from layout bounds, or inspect all occupied cells aligned
with the insertion ray.

### P2 — Apply strictness after the subassembly rewrite

[`instructions/sequencer.py:170`](../src/legolization/instructions/sequencer.py#L170)
runs the initial sequence using the original strict policy before extracting
subassemblies. The sequence can raise for a persistently floating prefix before
the subassembly rewrite has a chance to make the plan stable.

The mini-mushroom reproduction becomes fully stable with warning mode plus
subassemblies, but strict mode plus subassemblies raises before extraction.

Construct the candidate plan using warning semantics, rewrite it into
sub-build-and-attach steps, and enforce strictness on that final plan.

### P2 — Require every subassembly to attach exactly once

The coverage check at
[`instructions/sequencer.py:519`](../src/legolization/instructions/sequencer.py#L519)
counts brick IDs in sub-build steps, but there is no assertion before return that
every subassembly is attached exactly once or that the final world contains the
whole layout.

Removing the attach step from a valid mini-mushroom plan leaves a complete
`plan.order`; `verify_plan()` returns no errors, while flattened LDraw output
contains only three of six bricks.

Track attachment counts, require each declared subassembly to attach exactly
once, and assert that final world placement equals the source layout.

### P2 — Derive MPD stems consistently

[`ldraw_out.py:131`](../src/legolization/ldraw_out.py#L131) uses
`name.removesuffix(".ldr").removesuffix(".mpd")`, which is case-sensitive.
`write_model` accepts `.MPD` case-insensitively, while `FILE` sections use
`Path.stem`. For `MODEL.MPD`, the root references `MODEL.MPD-sub-1.ldr` but
defines `0 FILE MODEL-sub-1.ldr`, causing viewers and re-import to lose the
subassembly.

Use `Path(name).stem`, or apply identical case-insensitive suffix handling
throughout the writer.

### P2 — Reject or implement ignored profile modes

Profile handling in [`main.py:388`](../src/legolization/main.py#L388) is not
honored by every accepted command path. Both `--strategy all --profile p.json`
and LDraw input with `--profile p.json` return success and write a model, but do
not create the requested profile.

Reject unsupported combinations during argument validation, as the help text
implies, or implement profile recording for those paths.

### P2 — Catch MILP failures before Kollsker fallback

The SciPy `milp` calls in
[`placement/layered/kollsker.py:94`](../src/legolization/placement/layered/kollsker.py#L94)
and [`kollsker.py:106`](../src/legolization/placement/layered/kollsker.py#L106)
are unguarded. A mocked `milp` raising `RuntimeError("HiGHS crashed")`
propagates and fails the whole Kollsker candidate instead of falling back to
component Bond placement. Non-finite `milp_bond_weight` values can also reach
the solver because they are not validated.

Validate configuration values as finite and non-negative, and convert expected
SciPy/HiGHS failures into the existing `None` fallback path.

### P2 — Honor the Kollsker layer deadline

Deadline handling at
[`placement/layered/kollsker.py:63`](../src/legolization/placement/layered/kollsker.py#L63),
[`kollsker.py:89`](../src/legolization/placement/layered/kollsker.py#L89), and
[`kollsker.py:114`](../src/legolization/placement/layered/kollsker.py#L114)
floors expired deadlines to 100 ms. Every component receives the same deadline,
and stage two reuses an allowance calculated before stage one. Without a global
deadline, each component can spend twice `layer_time_s`.

In a mocked 50 ms deadline reproduction, both stages received 100 ms and
completed after the deadline.

Use one deadline per layer, fall back immediately when it is expired, and
recompute remaining time before stage two.

### P2 — Do not flag successful attaches as warnings

[`scripts/check_instructions.py:86`](../scripts/check_instructions.py#L86) adds
an `"attach"` flag for every attach operation. The script later treats any flag
as a reason to exit with status 2. Consequently, `--subassemblies` cannot
produce a clean result even when all attachments are stable and valid.

Record attachments as neutral metadata, or exclude the `"attach"` marker from
warning and exit-status classification.

### P3 — Cross-check floating shortcuts

The floating-prefix shortcut in
[`stability/prefix.py:302`](../src/legolization/stability/prefix.py#L302) returns
before `engine_cross_check` runs, although that mode promises a cold solve and
comparison for every warm probe.

For a grounded 1-by-1, cantilevered 1-by-6, and disconnected floater, cross-check
mode returned synthetic zero scores for the grounded component. Cold analysis
returned scores around `0.05` and objective `0.00431379`.

Disable the shortcut in cross-check mode, or route its result through
`_cross_check`.

### P3 — Correct finishing telemetry attribution

The [`phase.slopes` span](../src/legolization/pipeline.py#L179) wraps slope,
SNOT, and tile finishing. An SNOT-only profile therefore reports all finishing
time as slope time.

Rename the outer span to something such as `phase.finish_surfaces`, and use
nested spans if individual finishing phases need separate attribution.

## CI blocker

The pull request cannot currently pass its required lint-and-test workflow.
[`lint-test.yml:39`](../.github/workflows/lint-test.yml#L39) runs:

```text
uv run lizard --languages python --CCN 18 src tests
```

GitHub Actions run `29670455775`, Python 3.12 job `88148352070`, passed Ruff
formatting and linting, ty, and pyrefly, then failed this complexity gate:

- `_validate_args`: CCN 25 at
  [`src/legolization/main.py:388`](../src/legolization/main.py#L388)
- `_mount`: CCN 22 at
  [`src/legolization/placement/snot.py:107`](../src/legolization/placement/snot.py#L107)
- `verify_plan`: CCN 33 at
  [`src/legolization/instructions/sequencer.py:500`](../src/legolization/instructions/sequencer.py#L500)

The test step never ran, and the Python 3.13 matrix job was canceled by
fail-fast. The same lizard failure reproduces locally. Refactor these functions
below the existing CCN 18 threshold; relaxing the gate should require an
explicit project decision.

The external `Contributor trust` check also reports `ACTION_REQUIRED`, but it
does not expose an Actions log or a code-level root cause.

## Verification performed

Independent local verification passed on Python 3.12 and 3.13:

```text
uv run ruff format --check .
uv run ruff check .
uv run ty check src tests
uv run pyrefly check src tests
uv run pytest -q
uv run deptry .
git diff --check
```

All 471 tests passed on both versions (255.68 seconds on Python 3.12 and 231.52
seconds on Python 3.13). The local lizard command failed only on the same three
functions reported by CI.

Focused subsystem verification included:

- 88 instruction, LDraw, rendering, and booklet tests, plus 32 SNOT/search
  tests; real LeoCAD rendering succeeded for ordinary and subassembly models.
- 120 pipeline, CLI, and evaluation tests.
- 4,674 randomized stability warm-versus-cold probes; all results matched.

No repository files were changed while performing the original review.
