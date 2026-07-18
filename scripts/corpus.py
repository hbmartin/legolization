"""Manage the self-evaluation corpus described by ``data/corpus/manifest.toml``.

The corpus has two halves: curated meshes downloaded from pinned URLs
(gitignored; ``download`` fetches and sha256-verifies them) and synthetic
stress-test shapes (gitignored; ``generate`` rebuilds them from the pure,
deterministic generators in this file — the generators are the committed
source of truth, never the ``.npy`` files).

Usage::

    uv run python scripts/corpus.py generate [--only NAME,...]
    uv run python scripts/corpus.py download [--only NAME,...]
    uv run python scripts/corpus.py verify
    uv run python scripts/corpus.py list
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Literal

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Callable

_REPO = Path(__file__).resolve().parent.parent
MANIFEST = _REPO / "data" / "corpus" / "manifest.toml"
_EMPTY = -1  # legolization.grid.EMPTY, inlined so generators stay numpy-pure
_DOWNLOAD_TIMEOUT_S = 30.0


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
    up: str | None = None
    generator: str | None = None
    plates_per_voxel: int = 3
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
            extra_args=tuple(entry.get("extra_args", ())),
        )
        for entry in data.get("model", ())
    ]
    names = [model.name for model in models]
    if len(names) != len(set(names)):
        msg = "duplicate model names in corpus manifest"
        raise ValueError(msg)
    return models


# --- synthetic generators -------------------------------------------------
# Pure deterministic geometry (no RNG): regeneration is byte-identical.
# Each returns an int16 LDraw-code array indexed (x, y, layer-voxel).


def cantilever(length: int = 8, thickness: int = 2) -> np.ndarray:
    """Arm cantilevered over a base slab: stresses joint capacity + repair.

    The slab keeps the centre of mass over the support polygon so the model
    cannot topple; what remains is the knob-joint stress along the arm and
    (from ``length >= 8``) an unstable mid-build prefix for the sequencer.
    """
    base_depth = 4
    nx = 2 + length
    height = 7
    codes = np.full((nx, base_depth, height), _EMPTY, dtype=np.int16)
    codes[:, :, 0] = 7
    codes[:2, 1:3, 1 : height - thickness] = 4
    codes[:, 1:3, height - thickness :] = 14
    return codes


def topple_arm(length: int = 6, thickness: int = 2) -> np.ndarray:
    """One-sided arm on a narrow column: the whole model must topple.

    No brick joint fails here — the RBE verdict is global tipping (centre
    of mass beyond the 2x2 base). Expected unbuildable by physics; pins the
    solver's torque handling.
    """
    height = 6
    codes = np.full((2 + length, 2, height), _EMPTY, dtype=np.int16)
    codes[:2, :, :] = 4
    codes[2:, :, height - thickness :] = 14
    return codes


def mushroom(cap_radius: int = 6, stem_radius: int = 2) -> np.ndarray:
    """Wide cap on a thin stem: stresses overhang sequencing."""
    size = 2 * cap_radius + 1
    stem_height, cap_height = 5, 3
    codes = np.full((size, size, stem_height + cap_height), _EMPTY, dtype=np.int16)
    xs, ys = np.mgrid[0:size, 0:size]
    dist2 = (xs - cap_radius) ** 2 + (ys - cap_radius) ** 2
    codes[dist2 <= stem_radius**2, :stem_height] = 15
    codes[dist2 <= cap_radius**2, stem_height:] = 4
    return codes


def two_towers_bridge(gap: int = 6) -> np.ndarray:
    """Deck spanning two towers: stresses connectivity + mid-build spans."""
    tower, height, deck = 3, 8, 2
    nx = tower * 2 + gap
    codes = np.full((nx, tower, height + deck), _EMPTY, dtype=np.int16)
    codes[:tower, :, :height] = 1
    codes[-tower:, :, :height] = 1
    codes[:, :, height:] = 14
    return codes


def thin_shell(radius: int = 8) -> np.ndarray:
    """One-voxel-thick open dome: stresses fragmentation + seed variance."""
    size = 2 * radius + 1
    codes = np.full((size, size, radius + 1), _EMPTY, dtype=np.int16)
    xs, ys, zs = np.mgrid[0:size, 0:size, 0 : radius + 1]
    dist2 = (xs - radius) ** 2 + (ys - radius) ** 2 + zs**2
    shell = (dist2 <= radius**2) & (dist2 >= (radius - 1) ** 2)
    codes[shell] = 2
    return codes


def letter_t() -> np.ndarray:
    """Top-heavy T: bar ends dangle until the bar row completes."""
    depth, stem_width, stem_height = 2, 3, 7
    bar_width, bar_height = 11, 3
    codes = np.full(
        (bar_width, depth, stem_height + bar_height), _EMPTY, dtype=np.int16
    )
    x0 = (bar_width - stem_width) // 2
    codes[x0 : x0 + stem_width, :, :stem_height] = 1
    codes[:, :, stem_height:] = 4
    return codes


def letter_h() -> np.ndarray:
    """H crossbar suspended mid-air between posts: dangling-step risk.

    Single colour on purpose: under the hard colour constraint a
    two-colour H is *impossible* (no brick may span the colour boundary,
    so the bar never gets stud support). Monochrome, merged bricks anchor
    the bar into both posts and the challenge is purely sequencing.
    """
    depth, post_width, height, gap, bar_height = 2, 2, 10, 4, 2
    nx = post_width * 2 + gap
    codes = np.full((nx, depth, height), _EMPTY, dtype=np.int16)
    codes[:post_width, :, :] = 14
    codes[-post_width:, :, :] = 14
    mid = (height - bar_height) // 2
    codes[post_width : post_width + gap, :, mid : mid + bar_height] = 14
    return codes


def letter_h_bicolour() -> np.ndarray:
    """Two-colour H: hard colour constraint severs the bar's stud support.

    No brick may span the post/bar colour boundary, so the bar can never
    be anchored - expected unbuildable; pins the colour-constraint gate.
    """
    codes = letter_h()
    bar = codes[2:6, :, :]
    bar[bar != _EMPTY] = 4
    return codes


def staircase_overhang(offset: int = 1, steps: int = 8) -> np.ndarray:
    """Each tread shifts sideways: progressive overhang limit."""
    depth, tread = 4, 3
    nx = tread + offset * (steps - 1)
    codes = np.full((nx, depth, steps), _EMPTY, dtype=np.int16)
    for i in range(steps):
        x0 = i * offset
        codes[x0 : x0 + tread, :, i] = 2
    return codes


def wide_arch(span: int = 10) -> np.ndarray:
    """Flat lintel over a wide gap: keystone instability during build."""
    pier, depth, height, lintel = 2, 3, 6, 2
    nx = pier * 2 + span
    codes = np.full((nx, depth, height + lintel), _EMPTY, dtype=np.int16)
    codes[:pier, :, :height] = 15
    codes[-pier:, :, :height] = 15
    codes[:, :, height:] = 1
    return codes


def sparse_pillars() -> np.ndarray:
    """Four disconnected pillars: exercises the least-bad selection path."""
    codes = np.full((10, 10, 5), _EMPTY, dtype=np.int16)
    for x0, y0 in ((0, 0), (8, 0), (0, 8), (8, 8)):
        codes[x0 : x0 + 2, y0 : y0 + 2, :] = 7
    return codes


GENERATORS: dict[str, Callable[[], np.ndarray]] = {
    "cantilever": cantilever,
    "topple_arm": topple_arm,
    "mushroom": mushroom,
    "two_towers_bridge": two_towers_bridge,
    "thin_shell": thin_shell,
    "letter_t": letter_t,
    "letter_h": letter_h,
    "letter_h_bicolour": letter_h_bicolour,
    "staircase_overhang": staircase_overhang,
    "wide_arch": wide_arch,
    "sparse_pillars": sparse_pillars,
}


# --- commands -------------------------------------------------------------


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


def generate(models: list[CorpusModel], only: str | None = None) -> int:
    """Regenerate synthetic models from their registered generators."""
    for model in select_models(models, only):
        if model.kind != "synthetic":
            continue
        if model.generator not in GENERATORS:
            print(f"error: {model.name}: unknown generator {model.generator!r}")
            return 1
        codes = GENERATORS[model.generator]()
        model.abs_path.parent.mkdir(parents=True, exist_ok=True)
        np.save(model.abs_path, codes)
        print(f"generated {model.path} {codes.shape}")
    return 0


def _sha256_of(path: Path) -> str:
    """Hex sha256 of a file."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(models: list[CorpusModel], only: str | None = None) -> int:
    """Fetch mesh models from their pinned URLs, verifying sha256."""
    status = 0
    for model in select_models(models, only):
        if model.kind != "mesh":
            continue
        if model.source_url is None or model.sha256 is None:
            print(f"error: {model.name}: manifest is missing source_url/sha256")
            status = 1
            continue
        if model.abs_path.exists() and _sha256_of(model.abs_path) == model.sha256:
            print(f"ok {model.path} (already present)")
            continue
        if not model.source_url.startswith("https://"):
            print(f"error: {model.name}: refusing non-https URL")
            status = 1
            continue
        model.abs_path.parent.mkdir(parents=True, exist_ok=True)
        print(f"fetching {model.source_url}")
        try:
            with TemporaryDirectory(
                dir=model.abs_path.parent,
                prefix=f".{model.abs_path.name}.",
            ) as temp_dir:
                temp_path = Path(temp_dir) / model.abs_path.name
                with (
                    urllib.request.urlopen(  # noqa: S310 - https enforced above
                        model.source_url,
                        timeout=_DOWNLOAD_TIMEOUT_S,
                    ) as response,
                    temp_path.open("wb") as target,
                ):
                    shutil.copyfileobj(response, target)
                if (actual := _sha256_of(temp_path)) != model.sha256:
                    print(
                        f"error: {model.name}: sha256 mismatch "
                        f"(expected {model.sha256}, got {actual}); file discarded"
                    )
                    status = 1
                    continue
                temp_path.replace(model.abs_path)
        except (OSError, urllib.error.URLError) as error:
            print(f"error: {model.name}: download failed: {error}")
            status = 1
            continue
        print(f"ok {model.path}")
    return status


def verify(models: list[CorpusModel]) -> int:
    """Check every model is present and matches its source of truth."""
    status = 0
    for model in models:
        if not model.abs_path.exists():
            print(f"MISSING {model.name} ({model.path}) - run generate/download")
            status = 1
            continue
        match model.kind:
            case "mesh":
                ok = model.sha256 is not None and (
                    _sha256_of(model.abs_path) == model.sha256
                )
                print(f"{'ok' if ok else 'HASH MISMATCH'} {model.name}")
            case "synthetic":
                expected = GENERATORS[model.generator or ""]()
                try:
                    actual = np.load(model.abs_path)
                except (EOFError, OSError, ValueError) as error:
                    print(f"CORRUPT {model.name}: {error}")
                    status = 1
                    continue
                ok = bool(np.array_equal(actual, expected))
                print(f"{'ok' if ok else 'STALE (regenerate)'} {model.name}")
            case _:
                ok = False
                print(f"UNKNOWN KIND {model.name}: {model.kind}")
        if not ok:
            status = 1
    return status


def list_models(models: list[CorpusModel]) -> int:
    """Print a status table of the manifest."""
    print(f"{'name':<20} {'kind':<10} {'present':<8} traits")
    for model in models:
        present = "yes" if model.abs_path.exists() else "no"
        print(
            f"{model.name:<20} {model.kind:<10} {present:<8} {', '.join(model.traits)}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("generate", "download"):
        command = sub.add_parser(name)
        command.add_argument("--only", default=None, metavar="NAME,...")
    sub.add_parser("verify")
    sub.add_parser("list")
    args = parser.parse_args(argv)
    models = load_manifest()
    match args.command:
        case "generate":
            return generate(models, only=args.only)
        case "download":
            return download(models, only=args.only)
        case "verify":
            return verify(models)
        case _:
            return list_models(models)


if __name__ == "__main__":
    sys.exit(main())
