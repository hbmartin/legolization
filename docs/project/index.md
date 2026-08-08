# Project notes

This section is the contributor archive. Unlike the three reader tracks, **nothing
here defines current defaults** — it is either a guide you should follow when doing
a particular kind of work, a frozen report recording a past investigation, or
append-only history.

[`ROADMAP.md`](https://github.com/hbmartin/legolization/blob/main/ROADMAP.md) is the
source of truth for current state, active work, and the open engineering backlog.
For how the software behaves today, read the [Guide](../guide/index.md) and
[Theory](../theory/index.md) tracks.

## Guides — live, maintained, follow these

| Doc | What it is |
| --- | --- |
| [Self-evaluation playbook](../guides/self-evaluation-playbook.md) | How to judge output quality — renders, strategy comparison, instruction sanity, corpus sweeps — and run the self-improvement loop. The entry point cited by `CLAUDE.md` / `AGENTS.md`. |
| [Performance testing](../guides/performance-testing.md) | How to measure speed, prove a perf change, and what counts as a regression. Holds the pre-registered measurement thresholds. |

## Reports — evidence from a specific investigation, at a specific date

Each states the code state it was written against. Later addenda are appended rather
than rewritten, so read the dates. Recommendations in a report rank a fix space; they
are not commitments — open work lives in `ROADMAP.md`.

| Doc | What it is |
| --- | --- |
| [Kollsker drift](../reports/kollsker-drift-report.md) | Why the per-layer-optimal strategy finished worse end-to-end, and the best-of-k fix. Closed v8. Cited from [Merge and repair](../theory/placement/merge-and-repair.md). |
| [Unstable prefixes](../reports/unstable-prefix-report.md) | Why "prefix unstable" steps survive the sequencer's rescue paths. Class resolved v8. The empirical basis for [Subassemblies](../theory/subassemblies.md). |
| [Physics fidelity notes](../reports/physics-fidelity-notes.md) | What each RBE solver switch models, what was measured, and where capacity constants could come from. Companion to [The RBE model](../theory/stability/rbe.md). |
| [Mesh baseline](../reports/mesh-baseline-pending.md) | **Has one open item** — the mesh-kind baseline cut, with its measured history and the exact commands. Cross-linked from `ROADMAP.md` "Active work". |

## History — append-only

| Doc | What it is |
| --- | --- |
| [Roadmap history](../history/roadmap-history.md) | Dated log of the v3–v8 programs, plus the superseded design notes those programs worked from. Historical evidence only; `ROADMAP.md` wins any disagreement. |

## Before moving or renaming a doc

Several files under `docs/guides/` and `docs/reports/` are cited by path from source
comments and scripts, where a stale path is invisible until someone follows it. Grep
first:

```sh
grep -rn "docs/" --exclude-dir=.venv --exclude-dir=.git --exclude-dir=site .
```

Current inbound references live in `CLAUDE.md`, `AGENTS.md`, `README.md`,
`ROADMAP.md`, `scripts/benchmark_screen.py`, `scripts/count_trajectory.py`,
`src/legolization/instructions/subassembly.py`, and
`src/legolization/placement/layered/{bridge,engine}.py`.

`tests/test_docs.py` guards the rest: it fails if a navigation entry stops
resolving, if a page under `docs/` becomes unreachable from the navigation, or if a
relative link between pages breaks.

## Building this site

```sh
uv run --group docs zensical serve   # live preview
uv run --group docs zensical build   # static output in ./site
```

Configuration is [`zensical.toml`](https://github.com/hbmartin/legolization/blob/main/zensical.toml)
at the repository root. `.github/workflows/docs.yml` builds every pull request and
publishes `main` to GitHub Pages.
