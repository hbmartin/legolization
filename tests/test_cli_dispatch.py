"""Unified dispatcher contract: inventory, guidance, one-envelope JSON."""

import argparse
import json
from pathlib import Path

import pytest

from legolization.cli import main
from legolization.cli.common import parse_set_override, resolve_config
from legolization.cli.parser import COMMAND_NAMES, build_parser
from legolization.errors import ConfigurationError

EXAMPLES = Path(__file__).parent.parent / "data" / "examples"

TOP_LEVEL_COMMANDS = {
    "build",
    "validate",
    "cache",
    "analyze",
    "bundle",
    "input",
    "model",
    "instructions",
    "catalog",
    "corpus",
    "parts",
}

LEAF_COMMANDS = (
    "build",
    "validate",
    "cache inspect",
    "cache clear",
    "analyze",
    "bundle",
    "input inspect",
    "model render",
    "instructions audit",
    "catalog infer",
    "catalog validate",
    "corpus list",
    "corpus generate",
    "corpus download",
    "corpus verify",
    "corpus collect",
    "corpus assemble",
    "corpus evaluate",
    "parts sync",
)


def test_top_level_command_inventory_is_pinned():
    assert frozenset(TOP_LEVEL_COMMANDS) == COMMAND_NAMES


@pytest.mark.parametrize("command", ["", *LEAF_COMMANDS])
def test_every_command_is_discoverable_via_help(command):
    argv = [*command.split(), "--help"]
    with pytest.raises(SystemExit) as excinfo:
        main(argv)
    assert excinfo.value.code == 0


def test_bare_invocation_prints_migration_guidance(capsys):
    heart = EXAMPLES / "heart.vox"
    assert main([str(heart), "-o", "out.ldr"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "removed in 0.6" in captured.err
    assert "legolization bundle" in captured.err
    assert "legolization build" in captured.err


def test_bare_invocation_by_suffix_without_existing_file(capsys):
    assert main(["missing-model.obj"]) == 1
    assert "removed in 0.6" in capsys.readouterr().err


def test_unknown_command_is_a_usage_error_not_guidance(capsys):
    assert main(["frobnicate"]) == 1
    captured = capsys.readouterr()
    assert "removed in 0.6" not in captured.err
    assert "invalid choice" in captured.err


def test_missing_command_is_a_usage_error(capsys):
    assert main([]) == 1
    assert "error:" in capsys.readouterr().err


def test_parse_usage_error_with_json_emits_envelope(capsys):
    assert main(["build", "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema"] == "legolization.result/v1"
    assert payload["command"] == "build"
    assert payload["status"] == "error"
    assert payload["exit_code"] == 1


def test_handler_failure_with_json_emits_single_envelope(capsys, tmp_path):
    missing = tmp_path / "missing.npy"
    code = main(["build", str(missing), "-o", str(tmp_path / "o.ldr"), "--json"])
    assert code == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["error"]["type"] == "ConfigurationError"
    assert payload["exit_code"] == 1


@pytest.mark.parametrize(
    "argv",
    [
        ["catalog", "infer", "3001"],
        ["catalog", "validate", "ext.json"],
    ],
)
def test_stub_commands_return_valid_error_envelopes(argv, capsys):
    assert main([*argv, "--json"]) == 1
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["schema"] == "legolization.result/v1"
    assert payload["error"]["type"] == "NotImplementedError"
    assert "not implemented yet" in payload["error"]["message"]
    assert "not implemented yet" in captured.err


def test_cache_inspect_json_envelope_carries_entries(capsys, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(f'[cache]\npath = "{tmp_path / "cache"}"\n')
    assert main(["cache", "--config", str(config), "inspect", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "complete"
    assert payload["data"]["entries"] == []


def test_parser_builds_without_heavy_imports():
    parser = build_parser()
    assert parser.prog == "legolization"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("geometry.hollow=false", ("geometry.hollow", False)),
        ("placement.seed=3", ("placement.seed", 3)),
        ("placement.strategy=greedy", ("placement.strategy", "greedy")),
        ("placement.colour_weight=1.5", ("placement.colour_weight", 1.5)),
        ("input.mesh.auto_scale=[8, 16]", ("input.mesh.auto_scale", [8, 16])),
        ("input.mesh.up='y'", ("input.mesh.up", "y")),
    ],
)
def test_parse_set_override_types(raw, expected):
    assert parse_set_override(raw) == expected


def test_parse_set_override_rejects_missing_separator():
    with pytest.raises(ConfigurationError):
        parse_set_override("geometry.hollow")


def _namespace(**kwargs) -> argparse.Namespace:
    defaults = {
        "config": None,
        "set_overrides": [],
        "catalog": [],
        "catalog_estimates": [],
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def test_resolve_config_applies_set_overrides():
    config = resolve_config(
        _namespace(set_overrides=["geometry.hollow=false", "placement.seed=3"])
    )
    assert config.geometry.hollow is False
    assert config.placement.seed == 3


def test_resolve_config_dedicated_flags_beat_set():
    config = resolve_config(
        _namespace(set_overrides=["placement.seed=3"]),
        dedicated={"placement.seed": 9},
    )
    assert config.placement.seed == 9


def test_resolve_config_rejects_unknown_keys():
    with pytest.raises(ConfigurationError):
        resolve_config(_namespace(set_overrides=["nope.nothing=1"]))


def test_resolve_config_appends_catalog_paths(tmp_path):
    extension = tmp_path / "ext.json"
    config = resolve_config(_namespace(catalog=[extension]))
    assert config.catalog.extensions == (extension.resolve(),)


def test_config_toml_catalog_section_round_trips(tmp_path):
    extension = tmp_path / "ext.json"
    config_file = tmp_path / "legolization.toml"
    config_file.write_text('[catalog]\nextensions = ["ext.json"]\n')
    config = resolve_config(_namespace(config=config_file))
    assert config.catalog.extensions == (extension.resolve(),)
    assert config.to_dict()["catalog"]["extensions"] == [str(extension.resolve())]
