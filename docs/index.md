# Legolization

Turn a 3D model into a **physically buildable LEGO-style model** in LDraw format,
with step-by-step build instructions and a bill of materials.

This is the classic *LEGO construction problem* from the research literature:
voxelize → hollow → place bricks → check structural stability → repair → export.
What separates this implementation from a brick-shaped voxel dump is the physics.
Every candidate layout is scored by a full **Rigid-Block-Equilibrium** model —
per-brick force *and* torque balance with knob-friction capacities — solved as a
provably exact linear program on an open solver stack.

!!! info "Independent project"

    Legolization is an independent open-source project. It is not affiliated with,
    sponsored by, or endorsed by the LEGO Group. LEGO® is a trademark of the LEGO
    Group, which does not authorize or endorse this project.

---

## Pick a track

The documentation is split into three tracks. They cover the same system at three
depths, and you are meant to pick one rather than read all three.

<div class="grid cards" markdown>

-   :material-robot-happy: **[Basics](basics/index.md)**

    ---

    **You talk to a coding agent and never type a command.**

    Install the eleven-skill suite, then say *"turn dragon.stl into a LEGO model I can
    actually build"*. This track covers what each skill does, which one owns your
    job, what it will ask you before acting, and how to read what comes back.

    [:octicons-arrow-right-24: Start here](basics/index.md)

-   :material-console: **[Guide](guide/index.md)**

    ---

    **You want the command line and the concepts behind it.**

    Every command, every flag, every configuration key with its real default.
    Plus the ideas you need to use them well: the buildable gate, bundle identity
    and resume, quality tiers, and how to pick a placement strategy.

    [:octicons-arrow-right-24: Read the guide](guide/index.md)

-   :material-function-variant: **[Theory](theory/index.md)**

    ---

    **You want to know why it works, or to change it.**

    The RBE formulation written out in full, the proof that the LP relaxation is
    exact, the restriction argument behind the fast screen, the duality that makes
    assembly-by-disassembly correct — each anchored to the code and the paper.

    [:octicons-arrow-right-24: Go deep](theory/index.md)

</div>

---

## Sixty-second version

=== "With an agent"

    Install the skills into your coding agent:

    ```sh
    npx skills add hbmartin/legolization --all
    ```

    Then ask for what you want:

    > Turn `dragon.stl` into a LEGO model I can actually build.

    The agent installs or upgrades the CLI if it needs to, inspects your input,
    runs the pipeline, and reports back with a bundle directory, the winning
    strategy, and one suggested next step.

=== "With the CLI"

    ```sh
    uv tool install legolization
    legolization bundle model.obj
    ```

    That writes a portable `model-legolization/` directory next to your input
    containing the winning model, its bill of materials, a strategy comparison
    report, build instructions, and diagnostics.

    ```text
    bundle: model-legolization (complete)
      ingest: complete
      candidates: complete
      selection: complete
      model: complete
      bom: complete
      instructions: complete
    ```

---

## What it produces

Every operation writes a **portable bundle directory** — a self-describing folder
you can move, archive, or hand to another tool. Its `bundle.json` records the
source identity, the effective configuration, software and catalog versions, every
artifact with its hash, per-stage status, and the verdicts.

| You asked for | You get |
| --- | --- |
| A brick model from a mesh or voxel file | `.ldr` and `.mpd` models, BOM, comparison report, instructions |
| Building instructions | Step-annotated `.mpd`, an instruction audit, and an HTML/PDF booklet when a renderer is installed |
| A structural diagnosis | Connection graph, per-component and floating-brick models, load-path evidence, an HTML report |
| A repair | The repaired model in its own bundle, with before/after analysis — the source is never overwritten |

The **buildable gate** is hard and it is the same everywhere: a result counts as
buildable only when it is stable under the physics model, forms one stud-connected
component, and has nothing floating. Nothing rescues a candidate that fails it.

---

## Where things live

| | |
| --- | --- |
| [Basics](basics/index.md) | The eleven skills, conversationally |
| [Guide](guide/index.md) | CLI, configuration, bundles, artifacts |
| [Theory](theory/index.md) | Algorithms, physics, sequencing, papers |
| [Project notes](project/index.md) | Contributor guides, frozen investigation reports, roadmap history |
| [`ROADMAP.md`](https://github.com/hbmartin/legolization/blob/main/ROADMAP.md) | Current state, active work, open backlog — the source of truth for *what is being worked on* |

Documentation describes what the software does today. When this site and
`ROADMAP.md` disagree about a default, the code wins; please
[open an issue](https://github.com/hbmartin/legolization/issues).
