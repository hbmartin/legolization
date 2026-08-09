# Documentation

This directory is the source for the
[documentation site](https://hbmartin.github.io/legolization/).

**Start at [`index.md`](index.md)**, or read the rendered site — it is the same
content with navigation and search.

| Directory | Contents |
| --- | --- |
| [`basics/`](basics/) | Track 1 — using Legolization through a coding agent |
| [`guide/`](guide/) | Track 2 — the CLI, configuration, bundles, and artifacts |
| [`theory/`](theory/) | Track 3 — algorithms, physics, sequencing, and the papers |
| [`project/`](project/) | Index of the contributor archive below |
| [`guides/`](guides/) | Live contributor guides: self-evaluation, performance testing |
| [`reports/`](reports/) | Frozen investigation reports, each dated |
| [`history/`](history/) | Append-only roadmap and progress log |

Nothing under `guides/`, `reports/`, or `history/` defines current defaults.
[`ROADMAP.md`](https://github.com/hbmartin/legolization/blob/main/ROADMAP.md) is
the source of truth for current state, active work, and the open engineering
backlog.

## Building the site

```sh
uv run --group docs zensical serve   # live preview
uv run --group docs zensical build   # static output in ../site
```

Configuration is [`../zensical.toml`](../zensical.toml).
`.github/workflows/docs.yml` builds every pull request and publishes `main` to
GitHub Pages.

## Before moving or renaming a page

`tests/test_docs.py` fails if a navigation entry stops resolving, if a page
becomes unreachable from the navigation, or if a relative link between pages
breaks. Run `uv run pytest tests/test_docs.py` after any reorganization.

Several files under `guides/` and `reports/` are additionally cited **by path**
from source comments and scripts, where a stale path is invisible until someone
follows it. Grep first:

```sh
grep -rn "docs/" --exclude-dir=.venv --exclude-dir=.git --exclude-dir=site .
```

Current inbound references live in `CLAUDE.md`, `AGENTS.md`, `README.md`,
`ROADMAP.md`, `scripts/benchmark_screen.py`, `scripts/count_trajectory.py`,
`src/legolization/instructions/subassembly.py`, and
`src/legolization/placement/layered/{bridge,engine}.py`.
