# Guide

This track is for driving Legolization from the command line and understanding
what it is doing. It assumes you are comfortable in a terminal but makes no
assumptions about brick geometry, structural analysis, or the research literature.

If you would rather ask a coding agent to do this for you, read [Basics](../basics/index.md)
instead — the skills wrap exactly these commands. If you want the algorithms and the
math, read [Theory](../theory/index.md).

## Install

```sh
uv tool install legolization    # persistent install via uv
uvx legolization --help         # ephemeral run via uv
pip install legolization        # classic pip
```

If no package manager is available:

```sh
curl -LsSf https://uvx.sh/legolization/install.sh | sh
```

Two optional pieces unlock rendering and part geometry — see
[Rendering and parts](rendering-and-parts.md):

```sh
legolization parts sync         # managed official LDraw parts library
```

## The one command you need

`legolization bundle` is the complete pipeline. It accepts every supported input and
writes a portable bundle directory next to it.

```sh
legolization bundle model.obj
```

Everything else in this section is either a narrower entry point into the same
machinery (`build`, `analyze`) or a tool that operates on what `bundle` produced
(`model render`, `instructions audit`).

## Read in this order

<div class="grid cards" markdown>

-   **[Core concepts](concepts.md)**

    ---

    Studs, plates, and LDU; the three geometry representations; the buildable gate;
    bundles, identity, and resume; the detached worker model. Read this first — the
    command pages assume it.

-   **[Quality tiers and budgets](quality-and-budgets.md)**

    ---

    What `--quality fast|balanced|exhaustive|direct` actually runs, how `--duration`
    interacts with it, the material retry ladder, and how the winner is chosen.

-   **[Choosing a strategy](choosing-a-strategy.md)**

    ---

    Eight placement strategies, what each is good at, what it costs, and when to
    reach past the default.

-   **[Configuration reference](configuration.md)**

    ---

    Every TOML section and key with its type, real default, and effect. Precedence
    rules and the validation that fails before work starts.

-   **[Bundles and artifacts](bundles-and-artifacts.md)**

    ---

    Directory layouts for all seven bundle flavours, plus the JSON schemas you will
    actually open: `bundle.json`, the comparison report, the BOM, the audit.

-   **[Exit codes and JSON](exit-codes-and-json.md)**

    ---

    What each exit code means, which commands emit it, and the single-envelope
    `--json` contract for scripting.

-   **[Rendering and parts](rendering-and-parts.md)**

    ---

    Renderer detection and install, the managed LDraw library, and every environment
    variable the tool reads.

-   **[Python API](python-api.md)**

    ---

    Calling the pipeline, LDraw analysis, and assembly analysis in-process.

-   **[Command reference](cli/index.md)**

    ---

    Every command and flag, verified against `--help`.

</div>

## Conventions used here

Flag tables give the **real default**, taken from the dataclass or parser that owns
it — not from prose. Where a flag's default is "None", that means the flag defers to
a configuration key, and the table names it.

Commands are shown as `legolization ...`. In a development checkout, prefix them with
`uv run`.

## Evaluation and regression work

Measuring whether a change made the project better across many models is a separate
discipline with its own tooling (`legolization corpus ...`). The commands live in
[`corpus`](cli/corpus.md); the methodology — what to measure, how to read a scorecard,
and when a baseline may be rewritten — lives in the
[self-evaluation playbook](../guides/self-evaluation-playbook.md).
