"""Corpus manifest records and loading (``data/corpus/manifest.toml``)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_REPO = Path(__file__).resolve().parents[3]
MANIFEST = _REPO / "data" / "corpus" / "manifest.toml"


@dataclass(frozen=True, slots=True)
class CorpusModel:
    """One manifest entry: a mesh to download or a shape to generate."""

    name: str
    kind: Literal["mesh", "synthetic"]
    path: Path
    traits: tuple[str, ...] = ()
    expect_min_buildable: int = 1
    notes: str = ""
    source_url: str | None = None
    sha256: str | None = None
    license: str | None = None
    target_studs: int | None = None
    up: Literal["x", "y", "z"] | None = None
    generator: str | None = None
    plates_per_voxel: int = 3
    largest_component_only: bool = False
    extra_args: tuple[str, ...] = field(default=())

    @property
    def abs_path(self) -> Path:
        """Absolute on-disk location of this model's file."""
        return _REPO / self.path


def load_manifest(path: Path = MANIFEST) -> list[CorpusModel]:
    """Parse the corpus manifest into :class:`CorpusModel` records."""
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    models = [
        CorpusModel(
            name=entry["name"],
            kind=entry["kind"],
            path=Path(entry["path"]),
            traits=tuple(entry.get("traits", ())),
            expect_min_buildable=entry.get("expect_min_buildable", 1),
            notes=entry.get("notes", ""),
            source_url=entry.get("source_url"),
            sha256=entry.get("sha256"),
            license=entry.get("license"),
            target_studs=entry.get("target_studs"),
            up=entry.get("up"),
            generator=entry.get("generator"),
            plates_per_voxel=entry.get("plates_per_voxel", 3),
            largest_component_only=entry.get("largest_component_only", False),
            extra_args=tuple(entry.get("extra_args", ())),
        )
        for entry in data.get("model", ())
    ]
    names = [model.name for model in models]
    if len(names) != len(set(names)):
        msg = "duplicate model names in corpus manifest"
        raise ValueError(msg)
    return models


def select_models(models: list[CorpusModel], only: str | None) -> list[CorpusModel]:
    """Filter models by a comma-separated name list."""
    if only is None:
        return models
    wanted = {name.strip() for name in only.split(",") if name.strip()}
    unknown = wanted - {model.name for model in models}
    if unknown:
        listed = ", ".join(sorted(unknown))
        msg = f"unknown corpus model(s): {listed}"
        raise SystemExit(msg)
    return [model for model in models if model.name in wanted]
