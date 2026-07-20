# v5 pending measurements — circle back

Status as of the v7 program (2026-07-20). Runs 2 and 3 closed;
run 1 remains an explicit offline/release measurement, not part of
the default development loop.

## 1. Mesh-kind baseline cut — STILL PENDING

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
mesh cut only as an explicit offline/release run on an idle machine,
with a cap above Armadillo's measured floor:

    uv run python scripts/eval_corpus.py --kind mesh --timeout SECONDS --write-baseline

Run it after any change that moves placement or physics, on the state
you want as the reference. Do not share the machine with other sweeps,
and do not put this command in the fast inner loop.

## 2. U1 subassemblies-at-scale: the spot pair — CLOSED (v6)

spot@24: 80/155 unstable (v4 record) → **72/176** with
`--subassemblies`. Completes the U1 evidence table at six of six
models, five improved; recorded in ROADMAP's v6 WS-M entry.

## 3. spot@24 program-end profile — CLOSED (v6)

Result block 996 bricks / 176 steps (155 → 176 is the U1 subassembly
trade, not a regression). Wall was measured under four concurrent jobs
and is excluded from regression claims per docs/performance-testing.md.
