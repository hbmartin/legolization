# Rendering and parts

Two external pieces make the difference between numbers and pictures: the **LDraw
parts library** (geometry) and a **renderer** (images). Neither is required to
generate a model; both are required for booklets.

---

## The LDraw parts library

```sh
legolization parts sync
```

Installs or updates the official library into platform user-data storage — no admin
privileges, nothing in system directories. It validates the download before replacing
anything, checks weekly for updates afterwards, and continues silently offline while
a valid existing library remains usable. `--force` re-downloads early.

### Where it looks

In order:

1. an explicitly configured directory,
2. **`$LDRAWDIR`** — must contain a `parts/` subdirectory,
3. the managed library installed by `parts sync`,
4. `~/Library/Caches/pyldraw3/*/ldraw`, `~/.cache/pyldraw3/*/ldraw`,
5. `/usr/share/ldraw`, `/usr/local/share/ldraw`, `~/.ldraw`, `~/ldraw`,
   `~/Library/Application Support/LDraw`.

A missing library produces a **warning, not a failure**. Generation still works;
rendering and some catalog operations degrade.

---

## Renderers

Two are supported. LeoCAD is preferred for booklets because it exports all steps in
one batched invocation.

| Platform | Recommended | Install |
| --- | --- | --- |
| macOS | LDView | `brew install --cask ldview` |
| Windows | LeoCAD | `winget install LeoCAD.LeoCAD` |
| Ubuntu/Debian | LeoCAD + Xvfb | `apt install leocad xvfb` |

The [`render-ldraw` skill](../basics/skills/render-ldraw.md) ships paired
shell/PowerShell scripts that check for and install these, and it asks before
installing anything.

### Detection order

1. **`$LEGOLIZATION_RENDERER`** — an absolute path or a command name on `PATH`. The
   literal value `none` (case-insensitive) **disables rendering entirely**. The kind
   is taken as `ldview` when the basename contains "ldview", otherwise `leocad`.
2. `leocad` or `ldview` on `PATH`.
3. macOS application bundles:
   `/Applications/LeoCAD.app/Contents/MacOS/LeoCAD`,
   `/Applications/LDView.app/Contents/MacOS/LDView`.

```sh
LEGOLIZATION_RENDERER=/Applications/LeoCAD.app/Contents/MacOS/LeoCAD \
  legolization model render model.mpd

LEGOLIZATION_RENDERER=none legolization bundle model.obj   # skip all rendering
```

!!! warning "Judge renders by the files, not the exit code"

    Headless renderers can exit 0 while writing nothing — LDView's headless snapshot
    is a known case on some setups. `model render` reflects this in its exit codes
    (0 = all views, 3 = some, 1 = none) and the envelope names which views landed.

---

## Booklet rendering policy

`bundle --render` decides what happens when rendering is unavailable:

| Value | No renderer | Some steps fail |
| --- | --- | --- |
| `auto` *(default)* | Booklet **omitted entirely**, no placeholder pages; the omission is recorded. Exit 0. | Explicit missing-step markers; exit 3. |
| `required` | Partial result, exit 3. | Partial result, exit 3. |
| `off` | Never renders. | n/a |

Under every policy you still get the step-annotated `.mpd` and the instruction audit.
A booklet that silently substitutes blank pages for unrendered steps would be worse
than no booklet, so it never does that.

### Page size

Booklets pick Letter or A4 from your locale, reading `LC_PAPER`, `LC_ALL`,
`LC_CTYPE`, then `LANG`. Letter is used for `US`, `CA`, `MX`, `PH`, `CL`, `CO`, `CR`,
`GT`, `PA`, `DO`, `SV`, `VE`, `PR`; A4 otherwise; Letter when the locale is unknown
or unset. Booklets are English-only.

---

## What a booklet contains

- a cover page with model statistics and the full parts list;
- one rendered image per step, with the bricks added in that step highlighted;
- per-step part callouts;
- a separate section per subassembly, with an attach callout where it joins the model.

Subassemblies come from the sequencer lifting stretches that float in every build
order — mushroom caps, arch spans — into separately built units. See
[Subassemblies](../theory/subassemblies.md).

---

## Environment variables

Complete list of what the tool reads.

| Variable | Effect |
| --- | --- |
| `LEGOLIZATION_RENDERER` | Renderer path or command name; `none` disables rendering. |
| `LDRAWDIR` | LDraw parts library root; must contain `parts/`. |
| `LEGOLIZATION_DATA_HOME` | Overrides the user-data root for **both** the managed parts library and corpus inputs. |
| `LEGOLIZATION_BRICKLINK_DUMP` | Local BrickLink catalog export consulted by `catalog infer`. Works offline. |
| `REBRICKABLE_API_KEY` | Keyed source for `catalog infer`; skipped under `--offline`. |
| `BRICKOWL_API_KEY` | As above. |
| `LC_PAPER`, `LC_ALL`, `LC_CTYPE`, `LANG` | Booklet page size (Letter vs A4). |

---

## Verifying your setup

```sh
legolization parts sync
legolization bundle data/examples/heart.vox --quality fast
legolization model render heart-legolization
```

If the last command writes PNGs, everything is wired. If it exits 1, set
`LEGOLIZATION_RENDERER` explicitly to your renderer's binary and try again — auto
detection is best-effort, and an explicit path always wins.
