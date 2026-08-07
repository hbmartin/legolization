"""Shared option groups and configuration resolution for CLI commands.

Module-level imports stay light so building the parser (for ``--help``)
never pulls the numeric pipeline stack; configuration machinery loads
lazily inside :func:`resolve_config`.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from legolization.errors import ConfigurationError

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable

    from legolization.cli.envelope import ResultEnvelope
    from legolization.configuration import ProjectConfig


def add_json_option(parser: argparse.ArgumentParser) -> None:
    """Register the machine-readable output flag."""
    parser.add_argument(
        "--json",
        action="store_true",
        help=(
            "write exactly one result envelope to stdout; progress and "
            "warnings go to stderr"
        ),
    )


def add_config_options(parser: argparse.ArgumentParser) -> None:
    """Register project configuration loading and generic overrides."""
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        metavar="PATH",
        help="project TOML configuration file",
    )
    parser.add_argument(
        "--set",
        action="append",
        default=[],
        dest="set_overrides",
        metavar="KEY=VALUE",
        help=(
            "override one dotted configuration key (repeatable), "
            "e.g. --set geometry.hollow=false"
        ),
    )


def add_catalog_options(parser: argparse.ArgumentParser) -> None:
    """Register repeatable catalog overlay and estimate-sidecar paths."""
    parser.add_argument(
        "--catalog",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="parts-catalog extension; may be repeated",
    )
    parser.add_argument(
        "--catalog-estimates",
        type=Path,
        action="append",
        default=[],
        dest="catalog_estimates",
        metavar="PATH",
        help="catalog estimate sidecar with labeled provenance; may be repeated",
    )


def parse_set_override(raw: str) -> tuple[str, object]:
    """Parse ``KEY=VALUE`` with a TOML-typed value; bare words stay strings."""
    key, separator, value = raw.partition("=")
    key = key.strip()
    if not separator or not key:
        msg = f"--set expects KEY=VALUE, got {raw!r}"
        raise ConfigurationError(msg)
    text = value.strip()
    try:
        parsed = tomllib.loads(f"value = {text}")
    except tomllib.TOMLDecodeError:
        return (key, text)
    return (key, parsed["value"])


def resolve_config(
    args: argparse.Namespace,
    *,
    dedicated: dict[str, object] | None = None,
) -> ProjectConfig:
    """Load the project TOML and apply CLI overrides on top.

    Precedence: defaults < TOML < ``--set`` < dedicated flags. The
    repeatable ``--catalog`` / ``--catalog-estimates`` paths append
    after any TOML-declared entries.
    """
    from legolization.configuration import (  # noqa: PLC0415 - heavy import stays lazy
        load_project_config,
        merge_overrides,
    )

    config_path = getattr(args, "config", None)
    if config_path is not None:
        require_file(config_path, label="configuration")
    config = load_project_config(config_path)
    overrides: dict[str, object] = dict(
        parse_set_override(raw) for raw in getattr(args, "set_overrides", None) or []
    )
    overrides |= {
        key: value for key, value in (dedicated or {}).items() if value is not None
    }
    extensions = _catalog_paths(
        config.catalog.extensions,
        getattr(args, "catalog", None),
    )
    if extensions is not None:
        overrides["catalog.extensions"] = extensions
    sidecars = _catalog_paths(
        config.catalog.estimate_sidecars,
        getattr(args, "catalog_estimates", None),
    )
    if sidecars is not None:
        overrides["catalog.estimate_sidecars"] = sidecars
    return merge_overrides(config, overrides) if overrides else config


def _catalog_paths(
    existing: tuple[Path, ...],
    extra: list[Path] | None,
) -> list[str] | None:
    """Merge TOML-declared and CLI-passed catalog paths, keeping order."""
    if not extra:
        return None
    values = [str(path) for path in existing]
    values.extend(str(path) for path in extra)
    return list(dict.fromkeys(values))


def require_file(path: Path, *, label: str) -> None:
    """Reject a missing or non-file input before any expensive work."""
    if not path.is_file():
        msg = f"{label} must be an existing file: {path}"
        raise ConfigurationError(msg)


def require_output_path(path: Path) -> None:
    """Reject unusable single-model output destinations."""
    if path.exists() and not path.is_file():
        msg = f"output must be a file path: {path}"
        raise ConfigurationError(msg)
    if path.suffix.lower() not in {".ldr", ".mpd"}:
        msg = "output must end in .ldr or .mpd"
        raise ConfigurationError(msg)


def error_line(error: object) -> None:
    """Print one diagnostic line to stderr."""
    print(f"error: {error}", file=sys.stderr)


def stub_handler(
    command: str,
    phase: str,
) -> Callable[[argparse.Namespace], ResultEnvelope]:
    """Build a placeholder handler for a command landing in a later phase."""

    def _run(args: argparse.Namespace) -> ResultEnvelope:
        from legolization.cli.envelope import envelope_for_error  # noqa: PLC0415

        del args
        msg = f"'{command}' is not implemented yet; it lands with {phase}"
        return envelope_for_error(command, NotImplementedError(msg))

    return _run
