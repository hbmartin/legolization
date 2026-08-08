"""Adapters for external validation datasets.

Each module here is a pure external-format reader: it turns someone else's
release format into our types (``Layout``, ``VoxelGrid``) and exposes that
release's own ground-truth labels alongside, without deciding policy about
sampling, reporting, or I/O locations. That split follows
:mod:`legolization.stablelego`, whose loaders are shared by a test and a sweep
script and belong to neither.

The payloads themselves are never vendored; they are fetched by
``scripts/fetch_datasets.py`` from the registry in ``scripts/datasets.toml``.
Provenance and licences: ``docs/reports/dataset-survey.md``.

**Score conventions differ between releases and are not interchangeable.**
StableLego's ``stability_score.npy`` is a *stress* (higher is worse, >= 1.0
collapses; its README legend reads "Black: more stable. Red: higher internal
stress. White: collapsing bricks"), which is also our ``max_score`` convention.
StableText2Brick's ``stability_scores`` is the *inverse*: a margin in (0, 1]
where higher is better and unoccupied voxels are padded with 1.0. Each adapter
states the convention it reads and converts nothing implicitly.
"""

from __future__ import annotations

__all__ = ["stabletext2brick"]
