# PR #21 review

Date: 2026-07-20

PR:
[hbmartin/legolization#21](https://github.com/hbmartin/legolization/pull/21)

Verdict: **Changes required**

PR #21 was already merged when this review was performed. The review covered
the exact `523b671d278a9b4d32185ce260ef3f5939d93432..6e90d0695127b5be3ef71a165e8f61f9f08dec68`
comparison: 8 commits, 29 changed files, 2,966 additions, and 323 deletions.

Four actionable findings were confirmed: one P1, two P2s, and one P3. The
bridge MILP and warm press-probe implementations otherwise looked
well-guarded, and their focused tests passed.

## Findings

### P1 — The new corpus regression cannot run from a clean checkout

Location:
[`tests/test_check_instructions.py:223`](../tests/test_check_instructions.py#L223)

The new `test_press_tower_pins_the_insertion_audit` regression reads:

```python
_PRESS_TOWER = (
    Path(__file__).parent.parent
    / "data"
    / "corpus"
    / "synthetic"
    / "press-tower.npy"
)
```

However, `.gitignore` excludes the entire `data/corpus/synthetic/` directory,
and the repository tracks none of the generated `.npy` corpus artifacts. The
committed source of truth is the pure `press_tower()` generator in
`scripts/corpus.py`.

Consequently, the exact PR commit failed its clean Python 3.12 CI checkout
with:

```text
FileNotFoundError: data/corpus/synthetic/press-tower.npy
```

The local suite passes only when the ignored generated artifact already
exists in the developer's working copy. This makes the regression
environment-dependent and leaves a release-version PR with red CI.

Recommended fix:

1. Load the committed `press_tower()` generator in the test.
2. Save its output under `tmp_path`.
3. Pass that temporary path to `check_instructions.main`.
4. Keep a clean-checkout regression in CI so ignored local artifacts cannot
   mask this class of failure.

The PR's Python 3.12 CI job reported four failures in total. Three were
pre-existing platform or dependency-sensitive failures in the colour and
render tests; the missing `press-tower.npy` failure is directly introduced by
this PR.

### P2 — `verify_plan` skips insertion-fragile validation for subassembly steps

Locations:

- [`src/legolization/instructions/sequencer.py:705`](../src/legolization/instructions/sequencer.py#L705)
- [`src/legolization/instructions/sequencer.py:732`](../src/legolization/instructions/sequencer.py#L732)
- [`src/legolization/instructions/sequencer.py:810`](../src/legolization/instructions/sequencer.py#L810)

`_PlanVerifier.main_step` invokes `_check_fragile_mark`, but `sub_step` and
`attach_step` never do. Even if attachment verification called the existing
helper, its early guard requires `step.brick_ids`; attachment steps
intentionally have an empty tuple because they seat a completed subassembly
as a unit.

The result is that `verify_plan` does not enforce the claimed invariant that
a marked insertion-fragile step must reproduce as press-unstable for either
non-main step kind.

A focused reproduction used one grounded brick whose 1 kg press verdict was
stable. Both its sub-build step and attachment step were deliberately marked
`insertion_fragile=True`, yet verification returned:

```text
ground_brick_press_stable True
verify_false_fragile_marks []
```

The existing Gemini review comment about attachment steps is therefore valid
and slightly narrower than the full defect: sub-build marks are skipped too.

Recommended fix:

1. Resolve attachment press IDs from
   `self.subs[step.attaches].brick_ids`.
2. Re-derive attachment presses against `self.placed | unit` before the unit
   is committed.
3. Re-derive sub-build presses in the subassembly's translated,
   table-grounded frame.
4. Add false-positive regressions for both sub-build and attachment marks.

### P2 — Fragile sub-build steps lose their builder-facing warning

Locations:

- [`src/legolization/instructions/subassembly.py:364`](../src/legolization/instructions/subassembly.py#L364)
- [`src/legolization/instructions/subassembly.py:424`](../src/legolization/instructions/subassembly.py#L424)

`_emit_subassembly` recursively generates a sub-plan with insertion checking
enabled, and the resulting `BuildStep` objects preserve their
`insertion_fragile` marks. Warning propagation then retains only warnings
containing `_UNSTABLE_WARNING_MARK`:

```python
warnings.extend(
    f"subassembly {cluster.name}: {warning}"
    for warning in sub_plan.warnings
    if _UNSTABLE_WARNING_MARK in warning
)
```

The later `_regenerate_warnings` pass emits insertion warnings only when
`step.submodel is None`. Therefore neither path creates a warning for a
fragile sub-build step.

A focused reproduction produced:

```text
sub_fragile_steps [(2, 'sub-1', None)]
sub_fragile_warnings []
```

This is user-visible. The booklet derives its press-gently callouts from
`plan.warnings` and does not render a separate insertion-fragile badge from
the `BuildStep` field, so a builder receives no handling guidance for the
marked sub-build step.

Recommended fix:

1. Preserve and renumber insertion-fragile warnings from `sub_plan`, or
   regenerate warnings for every marked step, including submodel steps.
2. Include the subassembly name in the warning so the build context remains
   clear.
3. Add booklet coverage proving a fragile sub-build produces a visible
   press-gently callout.

### P3 — The new copy-pasteable evaluation command is invalid

Locations:

- [`docs/self-evaluation-playbook.md:146`](self-evaluation-playbook.md#L146)
- [`docs/self-evaluation-playbook.md:183`](self-evaluation-playbook.md#L183)

The playbook advertises this command as copy-pasteable:

```bash
uv run python scripts/eval_corpus.py --seeds 0,1,2 --only thin-shell
```

`scripts/eval_corpus.py` accepts `--models`, not `--only`. Running the
documented command exits during argument parsing:

```text
eval_corpus.py: error: unrecognized arguments: --only thin-shell
```

The same section still describes the committed synthetic baseline as having
11 shapes, while this PR expands it to 13. Its direct
`data/corpus/synthetic/*.npy` audit commands also require an explicit corpus
generation step on a clean checkout.

Recommended fix:

1. Replace `--only thin-shell` with `--models thin-shell`.
2. Update the baseline count from 11 to 13.
3. Add `uv run python scripts/corpus.py generate` before commands that consume
   generated `.npy` paths.

## Validation

The following validation was performed against the PR head:

| Command | Result |
| --- | --- |
| Focused changed-area pytest selection | 144 passed |
| `uv run pytest -q` | 584 passed locally in 298.53 seconds |
| `uv run ruff format --check .` | Passed; 90 files already formatted |
| `uv run ruff check .` | Passed |
| `uv run ty check src tests` | Passed |
| `uv run pyrefly check src scripts tests` | Passed; zero errors |

The full local pytest result includes ignored corpus artifacts already present
in the working copy and therefore does not contradict the clean-checkout CI
failure. No tracked source files were modified while gathering the review
evidence.
