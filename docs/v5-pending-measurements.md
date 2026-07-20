# v5 pending measurements — circle back

Status as of the v6 program end (2026-07-20). Runs 2 and 3 closed;
run 1 remains open with new evidence about WHY it keeps failing.

## 1. Mesh-kind baseline cut — STILL PENDING

`eval/baselines/scorecard-mesh.json` does not exist yet. The v6
attempt ran in an isolated worktree but the machine was carrying four
concurrent jobs: five of six meshes timed out at the 300 s per-job cap
across ALL strategies ("error: all failed"); only suzanne completed
(greedy, 365 bricks, PASS). The write-guard correctly refused the
baseline. The command is unchanged — what it needs is an OTHERWISE
IDLE machine:

    uv run python scripts/eval_corpus.py --kind mesh --write-baseline

Run it after any change that moves placement or physics, on the state
you want as the reference. Do not share the machine with other sweeps:
the 300 s job timeout is calibrated for an uncontended core.

## 2. U1 subassemblies-at-scale: the spot pair — CLOSED (v6)

spot@24: 80/155 unstable (v4 record) → **72/176** with
`--subassemblies`. Completes the U1 evidence table at six of six
models, five improved; recorded in ROADMAP's v6 WS-M entry.

## 3. spot@24 program-end profile — CLOSED (v6)

Result block 996 bricks / 176 steps (155 → 176 is the U1 subassembly
trade, not a regression). Wall was measured under four concurrent jobs
and is excluded from regression claims per docs/performance-testing.md.
