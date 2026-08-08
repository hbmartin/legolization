# The pending mesh-kind baseline cut

The one open measurement left from the v5 program's "circle back" list.
It is an explicit offline/release run, deliberately not part of the
default development loop. Opened v5; latest status 2026-08-07.

Cross-linked from `ROADMAP.md` "Active work". The other two v5 items
(the U1 subassembly spot pair, and the spot@24 program-end profile)
both closed in v6 — see the v6 WS-M entry in
`docs/history/roadmap-history.md`.

## The measurement — STILL PENDING

`eval/baselines/scorecard-mesh.json` does not exist yet. The v6
attempt under contention showed that the 300 s cap was insufficient.
The v7 idle-machine rerun raised it to 900 s and measured:

- spot: five strategies succeeded (605.9–900.1 s), two timed out;
- stanford-bunny: SM-GA and bond succeeded (350.2/871.9 s);
- teapot: all seven succeeded (189.8–300.2 s);
- armadillo: all seven still timed out at 900 s.

No baseline was written; Armadillo's failed row guarantees the guarded
write would have declined it. The user stopped Homer after another ten
minutes and made these runs opt-in by policy: default pytest skips
`slow` tests, and bare `eval_corpus.py` selects synthetics. Finish the
mesh cut only as an explicit offline/release run on an idle machine.

Armadillo stage triage completed sequentially on an idle machine on
2026-07-20, with layer-only instructions and a fresh 600-second watchdog
per top-level stage:

- greedy and Luo timed out in generic placement, whose internal
  stability-scoring calls no longer reset the parent watchdog;
- bond, fast, SM-GA, beauty, and Kollsker completed voxelization,
  layered tiling, compaction, connectivity, and initial stability
  analysis, then timed out in stability repair;
- layered tiling ranged from 0.9 to 36.6 seconds, connectivity from 4.1
  to 9.2 seconds, and voxelization from 0.17 to 0.19 seconds.

Bond completed one 596.9-second repair pass before a second repair timed
out. The detailed non-additive stability-span totals are recorded in
`docs/guides/performance-testing.md`. This closes the stage-identification task:
the remaining mesh baseline is still pending until the placement and
stability-repair release failures are resolved or explicitly accepted.

v8 gives the release run a working lever: `--time-budget SECONDS` now
bounds the repair, hollow-restore, and Luo stabilize loops at round
boundaries (one absolute pipeline deadline; see
`docs/guides/performance-testing.md`), so an Armadillo run can complete with a
partially repaired verdict instead of a watchdog kill. Whether such a
budget-truncated row is acceptable as the mesh baseline reference — or
whether Armadillo waits for incremental re-analysis to land — is the
explicit release decision to make at the next attempt.

*2026-08-07 status:* the release decision is made — **a documented
budget-truncated Armadillo row is acceptable** if the row still cannot
finish untruncated, and the attempt is sequenced after the reduced-QP
screen (the incremental-re-analysis mechanism, see
`docs/guides/performance-testing.md`, "The reduced-QP screen") merges. The
cut was deliberately NOT taken from the uncommitted screen worktree:
the baseline must reference a committed state. Next attempt, on an
otherwise idle machine after the screen lands:

    uv run legolization corpus collect --kind mesh --timeout 900
    uv run legolization corpus assemble
    uv run legolization corpus assemble --write-baseline

(the 0.6 CLI replaced the old `scripts/eval_corpus.py` /
`scripts/assemble_eval.py` commands). Note the corpus runner pins its
own per-candidate `PipelineConfig` — a screen-assisted or
budget-truncated Armadillo row needs the runner to grow config
plumbing (`corpus/collect.py::_effective_config`) first; whichever
levers are used, record them next to the baseline.

Run it after any change that moves placement or physics, on the state
you want as the reference. Do not share the machine with other sweeps,
and do not put this command in the fast inner loop.

## Closed alongside it (v6)

- **U1 subassemblies-at-scale, the spot pair.** spot@24: 80/155 unstable
  (v4 record) → **72/176** with `--subassemblies`, completing the U1
  evidence table at six of six models, five improved.
- **spot@24 program-end profile.** 996 bricks / 176 steps (155 → 176 is
  the U1 subassembly trade, not a regression). Wall was measured under
  four concurrent jobs and is excluded from regression claims per
  `docs/guides/performance-testing.md`.

Both are recorded in the v6 WS-M entry of
`docs/history/roadmap-history.md`.
