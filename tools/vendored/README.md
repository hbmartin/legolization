# Vendored tools

## bricknet/

- Source: the `bricknet` PyPI package, release 0.1.0
  (<https://github.com/kulits/BrickNet>)
- Paper: Kulits & Schmid, *BrickNet: Graph-Backed Generative Brick Assembly*,
  CVPR 2026 (arXiv:2604.22984)
- Vendored: 2026-08-09
- License: MIT, Copyright (c) 2026 Peter Kulits (see `LICENSE-bricknet.txt`)
- Status: frozen — do not edit. Verbatim copy of the released package
  (bytecode caches excluded) so the external-dataset analyses
  (`scripts/ldraw_coverage.py`) run against a snapshot that a future upstream
  release cannot silently move. The same release is a pinned dev-group
  dependency (`bricknet==0.1.0` in `pyproject.toml`); the four reference data
  tables are additionally vendored at `references/bricknet-data/` and a test
  asserts the two copies stay byte-identical.
- The BrickNet datasets behind the project's request form are **not** used —
  see `ROADMAP.md`, "External validation datasets". Everything here ships
  inside the MIT package.

## quick_validate.py

- Source: Anthropic `skill-creator` skill, `scripts/quick_validate.py`
- Vendored: 2026-08-06
- License: Apache-2.0 (see `LICENSE-quick-validate.txt`)
- Status: frozen — do not edit. Byte-identical copy of the upstream file so
  skill validation in CI matches skill-creator's validator exactly.
