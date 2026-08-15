# legolization

Turn a 3D model into a **physically buildable LEGO-style model** in LDraw
format, with step-by-step build instructions and a bill of materials.

This is the classic "LEGO construction problem" from the research literature
(see `references/`): voxelize → hollow → place bricks → check structural
stability → repair → export. The stability check is a full
**Rigid-Block-Equilibrium (RBE)** model (StableLego formulation, cross-validated
against its released test fixtures): per-brick force *and* torque balance with
knob-friction capacities, solved as a provably exact linear program on an open
solver stack — no Gurobi required.

📖 **[Full documentation](https://hbmartin.github.io/legolization/)**

> **Legolization is an independent open-source project. It is not affiliated
> with, sponsored by, or endorsed by the LEGO Group. LEGO® is a trademark of
> the LEGO Group, which does not authorize or endorse this project.**
>
> The skill icons in this repository are original artwork and are licensed
> under the repository license; they do not use LEGO trademarks.

## Install the skills

The conversational way to use Legolization is the eleven-skill suite. Install
the complete collection into your coding agent with:

```sh
npx skills add hbmartin/legolization --all
```

The suite is Codex-first but works with every agent supported by
`npx skills`. Each skill requires `legolization>=0.6.0`; when the CLI is
missing or too old, the skill installs or upgrades the latest stable release
automatically — via `uv tool install` or `pip`, falling back to the `uvx.sh`
standalone installer — and, if that installation fails, reports the
failure and stops the requested operation.

## Install the CLI

The CLI is a normal Python package. All of the commands below install the
latest release from PyPI, so they deliver 0.6.0 only once its pending
publication lands:

```sh
uv tool install legolization   # persistent install via uv
uvx legolization --help        # ephemeral run via uv
pip install legolization       # classic pip
```

If no package manager is available, use the `uvx.sh` standalone
installer (the same fallback the skills use):

```sh
curl -LsSf https://uvx.sh/legolization/install.sh | sh
```

## Quickstart

```sh
legolization parts sync                       # once: managed LDraw parts library
legolization bundle data/examples/heart.vox   # the complete pipeline
```

That writes a portable `heart-legolization/` bundle beside the input:

```text
bundle: heart-legolization (complete)
  ingest: complete   candidates: complete   selection: complete   model: complete   bom: complete   instructions: complete
```

containing the winning model (`model/model.mpd`), its bill of materials, the
full strategy comparison, build instructions, and diagnostics. Open the model
in [LDView](https://tcobbs.github.io/ldview/) or
[BrickLink Studio](https://www.bricklink.com/v3/studio/download.page).

A result counts as **buildable** only when it is stable under the physics
model, forms one stud-connected component, and has nothing floating. Nothing
rescues a candidate that fails that gate.

## Skill catalog

| Skill | Purpose | Say something like |
| --- | --- | --- |
| `legolize-model` | Turn a mesh or voxel model into a stable LEGO-style model and complete bundle. | "Turn dragon.stl into a LEGO model I can actually build." |
| `prepare-lego-input` | Inspect, orient, color, and normalize a source model before generation. | "Is spot.obj oriented right, and how big should the brick version be?" |
| `optimize-lego-build` | Compare, retile, or improve an existing LDraw assembly. | "Can you rebuild castle.ldr with fewer bricks without making it fragile?" |
| `publish-lego-instructions` | Produce a step-annotated MPD and, when rendering is available, HTML/PDF instructions. | "Make a printable instruction booklet for my mushroom model." |
| `analyze-lego-assembly` | Diagnose stability, load paths, insertion concerns, and assembly risks. | "Why does my spaceship model feel wobbly at the wings?" |
| `repair-lego-assembly` | Propose and validate a repair or redesign without overwriting the source. | "This model falls apart at the arch — fix it without changing how it looks." |
| `extend-lego-part-support` | Research, estimate, validate, and activate parts-catalog extensions. | "Add support for part 4070 so my headlight-brick model analyzes correctly." |
| `render-ldraw` | Render a model or bundle from requested views. | "Show me what heart.ldr looks like from the front and the top." |
| `judge-aesthetics` | Render two variants consistently and record or escalate a visual preference. | "Which of these two builds looks better?" |
| `inspect-instructions` | Audit buildability, ordering, insertion pressure, and booklet readiness. | "Do these build steps make sense, or will something fall off halfway through?" |
| `eval-corpus` | Run repeatable strategy evaluation across the available corpus. | "Did my placement change make the whole corpus better or worse?" |

There is deliberately no routing skill: `legolize-model` owns complete
new-model creation and `optimize-lego-build` owns improvement of an existing
assembly.

## Documentation

The [documentation site](https://hbmartin.github.io/legolization/) has three
tracks. Pick one rather than reading all three.

| Track | For | Covers |
| --- | --- | --- |
| **[Basics](https://hbmartin.github.io/legolization/basics/)** | Talking to a coding agent | The eleven skills, what each asks before acting, reading the results, troubleshooting |
| **[Guide](https://hbmartin.github.io/legolization/guide/)** | Driving the CLI | Every command and flag, the full configuration schema, bundles and artifacts, exit codes, the Python API |
| **[Theory](https://hbmartin.github.io/legolization/theory/)** | Changing the algorithms | The RBE formulation, the LP exactness proof, eight placement strategies, sequencing and its duality argument, all thirteen papers mapped to code |

Contributor guides, frozen investigation reports, and the roadmap history live
under [`docs/`](docs/) and in the site's **Project notes** section.
[`ROADMAP.md`](ROADMAP.md) is the source of truth for current state, active
work, and the open engineering backlog.

## Development

```sh
uv sync
uv run ldraw download --yes   # once: fetch the LDraw parts library
uv run ldraw generate --yes   # once: generate ldraw.library.* part/colour modules
```

```sh
uv run pytest             # fast inner loop; slow integrations skip by default
uv run pytest --run-slow  # full suite, including benchmark/sweep/renderer tests
uv run ruff format --check . && uv run ruff check .
uv run ty check src tests
uv run pyrefly check src tests
uv run deptry .
uv run lizard --languages python --CCN 15 --length 120 --arguments 8 src/legolization
```

Build the documentation site locally:

```sh
uv run --group docs zensical serve   # live preview
uv run --group docs zensical build   # static output in ./site
```

## License

GPL-3.0-or-later (inherited from pyldraw3).

Third-party vendored components keep their own licences:

- **BrickNet** (`bricknet` 0.1.0) — MIT, Copyright (c) 2026 Peter Kulits.
  Vendored at `tools/vendored/bricknet/` (package source, see
  `tools/vendored/LICENSE-bricknet.txt`) and `references/bricknet-data/`
  (reference data tables, see the LICENSE there). Upstream:
  <https://github.com/kulits/BrickNet>. The BrickNet datasets distributed
  behind a request form are not used or redistributed here.
- **quick_validate.py** — Apache-2.0, from Anthropic's skill-creator skill
  (`tools/vendored/LICENSE-quick-validate.txt`).
