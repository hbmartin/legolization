"""Bundle identity: the four hashes that define resumable work.

Identity intentionally ignores timestamps and output paths. The
configuration hash covers only result-affecting configuration — the
``cache`` and ``output`` sections are projected away so resume works
across machines and checkouts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from legolization.configuration import ProjectConfig

_NON_RESULT_SECTIONS = ("cache", "output")


@dataclass(frozen=True, slots=True, kw_only=True)
class BundleIdentity:
    """Input, configuration, software, and catalog identity of a bundle."""

    input_sha256: str
    config_sha256: str
    legolization_version: str
    catalog_sha256: str

    def key(self) -> str:
        """Return the combined identity key."""
        encoded = json.dumps(
            asdict(self),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()

    def short(self) -> str:
        """Return a filename-friendly identity prefix."""
        return self.key()[:16]

    def to_dict(self) -> dict[str, str]:
        """Return the JSON payload for this identity."""
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        """Rehydrate an identity from its JSON payload."""
        return cls(
            input_sha256=str(payload["input_sha256"]),
            config_sha256=str(payload["config_sha256"]),
            legolization_version=str(payload["legolization_version"]),
            catalog_sha256=str(payload["catalog_sha256"]),
        )


def result_affecting_config(
    config: ProjectConfig,
    *,
    invocation: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Project the configuration to the values that affect results.

    ``invocation`` folds result-affecting request options that live
    outside the configuration model (``--retile``, the retry ladder
    marker) into the hashed payload.
    """
    payload = config.to_dict()
    for section in _NON_RESULT_SECTIONS:
        payload.pop(section, None)
    if invocation:
        payload["invocation"] = dict(invocation)
    return payload


def bundle_identity(
    input_path: Path,
    config: ProjectConfig,
    *,
    invocation: Mapping[str, object] | None = None,
) -> BundleIdentity:
    """Compute the four-part identity for one bundle invocation."""
    from legolization.catalog import catalog_hash  # noqa: PLC0415
    from legolization.configuration import mapping_hash  # noqa: PLC0415
    from legolization.eval_artifacts import input_sha256  # noqa: PLC0415
    from legolization.inspection import resolve_prepared_input  # noqa: PLC0415
    from legolization.version import package_version  # noqa: PLC0415

    prepared = resolve_prepared_input(input_path)
    return BundleIdentity(
        input_sha256=(
            prepared.npy_sha256 if prepared is not None else input_sha256(input_path)
        ),
        config_sha256=mapping_hash(
            result_affecting_config(config, invocation=invocation)
        ),
        legolization_version=package_version(),
        catalog_sha256=catalog_hash(),
    )
