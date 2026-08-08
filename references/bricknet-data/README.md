# Vendored BrickNet data tables

Copied verbatim from the `bricknet` PyPI package (`bricknet/_data/v1/`), MIT
licensed — see `LICENSE` (Copyright (c) 2026 Peter Kulits).

- Upstream: <https://github.com/kulits/BrickNet>
- Paper: Kulits & Schmid, *BrickNet: Graph-Backed Generative Brick Assembly*,
  CVPR 2026, pp. 39252-39261 (arXiv:2604.22984).
- Package version vendored: `bricknet` 0.1.0
- Vendored: 2026-08-08

## Why vendored rather than read from the installed package

`bricknet` is a dev-group dependency (`pyproject.toml`), so the analyses can use
its parser and collision kernel. These four tables are vendored **as well** so
the catalog and connector cross-checks run offline with no dependency at all,
and so a future upstream release cannot silently move the reference data a
committed measurement was taken against.

The datasets behind the project's [request form](https://forms.gle/dm4eYSa5gh4DqzRT6)
are **not** used — see `ROADMAP.md`, "External validation datasets". Everything
here ships inside the MIT package.

## The tables

| File | Contents | Measured |
|---|---|---|
| `part_names.json` | part vocabulary, `stem -> name`; `part_id` is the file-order index | 14,583 parts |
| `labels.json.xz` | per-part connector labels, `{stem.dat: {kind: {subtype: rows}}}` | 14,603 parts |
| `part_aliases.json.xz` | `{"rows": [{src, dst, final_matrix_3x4}]}` — obsolete/duplicate stem to canonical stem | 2,522 rows |
| `color_names.json` | colour name -> LDraw colour code, including extended codes absent from `LDConfig.ldr` | 258 colours |

### `labels.json.xz` row format

A row is `[x, y, z, pitch_deg, roll_deg, ...]` in **part-local LDU**. A sixth
column carries the frame yaw for `fixed` connectors and the span length for
`axle` connectors, and is absent otherwise. The mating axis of a connector frame
is its **-Y**.

Polarized kinds (`hinge`, `ball`, `fixed`) carry one more nesting level:
`{subtype: {"in" | "on": rows}}`.

Iterating `sorted(kind) -> sorted(subtype) -> sorted(polarity) -> row order`
defines the **canonical connector order** — the flat indices used by the
upstream graphs' `edge_idx`.

Spot check, `3001.dat` (brick 2x4): `stud/stud` has 8 rows at x in
{-30,-10,10,30}, z in {-10,10}, y = 0 — the 20 LDU stud pitch on a part-local
origin — plus `hole/hole` 8 and `hole/tube` 3.

## What uses this

`scripts/ldraw_coverage.py` — cross-checks our catalog's physical connectors and
`assembly_connections.py` against an independently derived connector set, and
feeds `part_aliases` into the part-id canonicalization in
`catalog_infer/sources.py` (whose own canonicalization is a single regex
stripping a trailing revision letter).

## Regenerating

```sh
uv run python -c "
import bricknet, pathlib, shutil
src = pathlib.Path(bricknet.__file__).parent / '_data' / 'v1'
dst = pathlib.Path('references/bricknet-data')
for name in ('part_names.json', 'labels.json.xz', 'part_aliases.json.xz', 'color_names.json'):
    shutil.copy2(src / name, dst / name)
"
```

Bump the vendored-version line above when you do.
