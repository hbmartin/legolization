# Documentation index

[`ROADMAP.md`](../ROADMAP.md) is the source of truth for current state, active
work, and the open engineering backlog. Everything in this directory is either
a **guide** you should follow, a **frozen report** recording a past
investigation, or **history**. Nothing here defines current defaults.

## Guides — live, maintained, follow these

| Doc | What it is |
| --- | --- |
| [`guides/self-evaluation-playbook.md`](guides/self-evaluation-playbook.md) | How to judge output quality — renders, strategy comparison, instruction sanity, corpus sweeps — and run the self-improvement loop. The entry point cited by `CLAUDE.md` / `AGENTS.md`. |
| [`guides/performance-testing.md`](guides/performance-testing.md) | How to measure speed, prove a perf change, and what counts as a regression. Holds the pre-registered measurement thresholds. |

## Reports — evidence from a specific investigation, at a specific date

Each states the code state it was written against. Later addenda are appended
rather than rewritten, so read the dates. Recommendations in a report rank a
fix space; they are not commitments — open work lives in `ROADMAP.md`.

| Doc | What it is |
| --- | --- |
| [`reports/kollsker-drift-report.md`](reports/kollsker-drift-report.md) | Why the per-layer-optimal strategy finished worse end-to-end, and the best-of-k fix. Closed v8. |
| [`reports/unstable-prefix-report.md`](reports/unstable-prefix-report.md) | Why "prefix unstable" steps survive the sequencer's rescue paths. Class resolved v8. |
| [`reports/physics-fidelity-notes.md`](reports/physics-fidelity-notes.md) | What each RBE solver switch models, what was measured, and where capacity constants could come from. |
| [`reports/mesh-baseline-pending.md`](reports/mesh-baseline-pending.md) | **Has one open item** — the mesh-kind baseline cut, with its measured history and the exact commands. Cross-linked from `ROADMAP.md` "Active work". |
| [`reports/dataset-survey.md`](reports/dataset-survey.md) | Web survey of external datasets that could widen the test/verification surface — access paths, licences, and what verification gap each closes. Ranks a space; commits to nothing. |

## History — append-only

| Doc | What it is |
| --- | --- |
| [`history/roadmap-history.md`](history/roadmap-history.md) | Dated log of the v3–v8 programs, plus the superseded design notes those programs worked from. Historical evidence only; `ROADMAP.md` wins any disagreement. |

## Before moving or renaming a doc

Several files are cited from source comments and scripts, where a stale path
is invisible until someone follows it. Grep first:

```sh
grep -rn "docs/" --exclude-dir=.venv --exclude-dir=.git .
```

Current inbound references live in `CLAUDE.md`, `AGENTS.md`, `README.md`,
`ROADMAP.md`, `scripts/benchmark_screen.py`, `scripts/count_trajectory.py`,
`src/legolization/instructions/subassembly.py`, and
`src/legolization/placement/layered/{bridge,engine}.py`.
