# Install the skills

```sh
npx skills add hbmartin/legolization --all
```

That installs all eleven skills into your coding agent. The suite is Codex-first but works
with every agent `npx skills` supports.

To install a subset, drop `--all` and pick from the list:

```sh
npx skills add hbmartin/legolization
```

---

## The CLI installs itself

The skills wrap a command-line tool, `legolization`, and they manage it for you.

Each skill checks for **version 0.6.0 or newer**. If it is missing or too old, the skill
installs or upgrades it automatically:

1. `uv tool install legolization@latest`, or
2. `pip install --upgrade legolization`, and
3. only if neither package manager exists, the standalone installer
   documented at [uvx.sh/legolization](https://uvx.sh/legolization).

!!! info "This step does not ask permission"

    Installing the tool a skill wraps is treated as part of running the skill. What it
    will **not** do is fail quietly: if installation fails, the skill reports the
    failure and stops rather than continuing with a broken or missing tool.

If you would rather install it yourself first:

```sh
uv tool install legolization
# or
pip install legolization
```

---

## Two optional pieces

Neither is needed to generate a model. Both improve what you get back.

### The LDraw parts library

Provides real part geometry, needed for rendering and for analyzing existing models.

The `legolize-model` skill refreshes it **silently on every run** — no consent needed,
because it is a cache in your user data directory, requires no admin privileges, and a
valid existing copy survives a failed check.

To do it yourself:

```sh
legolization parts sync
```

### A renderer

Turns models into images and step-by-step booklets into something with pictures.

The `render-ldraw` skill checks for one and, if none is found, explains the
platform-appropriate install and **asks before running it**:

| Platform | Renderer | Installed with |
| --- | --- | --- |
| macOS | LDView | Homebrew |
| Windows | LeoCAD | winget |
| Ubuntu/Debian | LeoCAD + Xvfb | apt |

Without a renderer everything still works — you get models, bills of materials, and
step-annotated instruction files. What you do not get is images or a printable booklet.

More detail: [Rendering and parts](../guide/rendering-and-parts.md).

---

## Checking it worked

Ask your agent:

> Show me what data/examples/heart.vox would look like as a LEGO model.

If the skills are installed, it will recognize the request, inspect the file, run the
pipeline, and come back with a bundle path and a verdict.

You can also check the CLI directly:

```console
$ legolization --version
```

---

## What gets installed where

| Thing | Location |
| --- | --- |
| Skills | Your agent's skills directory, managed by `npx skills` |
| The `legolization` CLI | Wherever `uv tool` or `pip` puts executables |
| LDraw parts library | Platform user-data storage — no admin rights, nothing in system directories |
| Your models and bundles | Beside your input files. Nothing is written anywhere you did not point at. |

Move the user-data root with `LEGOLIZATION_DATA_HOME` if you want the parts library
somewhere specific.

---

## Updating

```sh
npx skills add hbmartin/legolization --all   # re-run to update the skills
uv tool upgrade legolization                 # or let a skill do it
```

The skills upgrade the CLI on their own when their minimum version moves, so in practice
you only need the first line.
