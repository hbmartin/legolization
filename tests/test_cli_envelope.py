"""Result envelope, exit-code mapping, and version reporting contract."""

import importlib.metadata
import json
from io import StringIO
from typing import Never

import pytest

from legolization.cli.envelope import (
    RESULT_SCHEMA,
    ArtifactRecord,
    ErrorRecord,
    ResultEnvelope,
    emit,
    envelope_for_error,
)
from legolization.cli.exit_codes import (
    COMPLETE,
    EXACT_LIMIT,
    INTERRUPTED,
    OPERATIONAL_ERROR,
    PARTIAL,
    UNBUILDABLE,
    CliUsageError,
    exit_code_for_exception,
    status_for_exit_code,
)
from legolization.cli.runner import run_command
from legolization.errors import (
    ConfigurationError,
    ExactPlacementLimitError,
    LegolizationError,
    ManifestError,
    PlacementInfeasibleError,
    UnsupportedCapabilityError,
)
from legolization.main import main
from legolization.version import __version__, package_version


def test_package_version_matches_distribution():
    assert package_version() == importlib.metadata.version("legolization")
    assert package_version() == "0.6.0"
    assert __version__ == package_version()


def test_version_flag_prints_version(capsys):
    assert main(["--version"]) == 0
    captured = capsys.readouterr()
    assert captured.out == f"legolization {__version__}\n"
    assert captured.err == ""


def test_envelope_golden_shape():
    envelope = ResultEnvelope(command="bundle", exit_code=COMPLETE)
    payload = envelope.to_dict()
    assert sorted(payload) == [
        "artifacts",
        "command",
        "exit_code",
        "schema",
        "status",
        "version",
        "warnings",
    ]
    assert payload["schema"] == RESULT_SCHEMA
    assert payload["version"] == "0.6.0"
    assert payload["command"] == "bundle"
    assert payload["status"] == "complete"
    assert payload["exit_code"] == 0
    assert payload["artifacts"] == []
    assert payload["warnings"] == []


def test_envelope_includes_error_data_and_artifacts():
    envelope = ResultEnvelope(
        command="bundle",
        exit_code=OPERATIONAL_ERROR,
        artifacts=(
            ArtifactRecord(path="model/model.mpd", kind="model", sha256="ab" * 32),
            ArtifactRecord(path="bom/bom.json", kind="bom"),
        ),
        warnings=("renderer unavailable",),
        error=ErrorRecord(type="OSError", message="boom", detail={"errno": 2}),
        data={"answer": 42},
    )
    payload = envelope.to_dict()
    assert payload["status"] == "error"
    assert payload["error"] == {
        "type": "OSError",
        "message": "boom",
        "detail": {"errno": 2},
    }
    assert payload["data"] == {"answer": 42}
    assert payload["artifacts"] == [
        {"path": "model/model.mpd", "kind": "model", "sha256": "ab" * 32},
        {"path": "bom/bom.json", "kind": "bom"},
    ]
    assert payload["warnings"] == ["renderer unavailable"]


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (ExactPlacementLimitError("limit"), EXACT_LIMIT),
        (PlacementInfeasibleError("infeasible"), UNBUILDABLE),
        (ConfigurationError("bad config"), OPERATIONAL_ERROR),
        (ManifestError("bad manifest"), OPERATIONAL_ERROR),
        (UnsupportedCapabilityError("unsupported"), OPERATIONAL_ERROR),
        (LegolizationError("generic"), OPERATIONAL_ERROR),
        (CliUsageError("usage"), OPERATIONAL_ERROR),
        (OSError("os"), OPERATIONAL_ERROR),
        (ValueError("value"), OPERATIONAL_ERROR),
        (RuntimeError("unexpected"), OPERATIONAL_ERROR),
        (KeyboardInterrupt(), INTERRUPTED),
    ],
)
def test_exit_code_for_exception(error, expected):
    assert exit_code_for_exception(error) == expected


@pytest.mark.parametrize(
    ("code", "status"),
    [
        (COMPLETE, "complete"),
        (OPERATIONAL_ERROR, "error"),
        (UNBUILDABLE, "unbuildable"),
        (PARTIAL, "partial"),
        (EXACT_LIMIT, "error"),
        (INTERRUPTED, "interrupted"),
        (99, "error"),
    ],
)
def test_status_for_exit_code(code, status):
    assert status_for_exit_code(code) == status


def test_envelope_for_error_populates_error_record():
    envelope = envelope_for_error("bundle", ValueError("boom"))
    assert envelope.exit_code == OPERATIONAL_ERROR
    assert envelope.status == "error"
    assert envelope.error is not None
    assert envelope.error.type == "ValueError"
    assert envelope.error.message == "boom"


def test_envelope_for_error_names_messageless_exceptions():
    envelope = envelope_for_error("bundle", KeyboardInterrupt())
    assert envelope.exit_code == INTERRUPTED
    assert envelope.status == "interrupted"
    assert envelope.error is not None
    assert envelope.error.message == "KeyboardInterrupt"


def test_emit_writes_exactly_one_json_object():
    stream = StringIO()
    emit(ResultEnvelope(command="bundle", exit_code=COMPLETE), stream=stream)
    payload = json.loads(stream.getvalue())
    assert payload["schema"] == RESULT_SCHEMA


def test_run_command_json_failure_emits_single_envelope(capsys):
    def handler() -> Never:
        msg = "bad configuration"
        raise ConfigurationError(msg)

    assert run_command("bundle", handler, json_output=True) == OPERATIONAL_ERROR
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "error"
    assert payload["error"]["type"] == "ConfigurationError"
    assert payload["error"]["message"] == "bad configuration"


def test_run_command_json_success_emits_single_envelope(capsys):
    envelope = ResultEnvelope(command="bundle", exit_code=COMPLETE)
    assert run_command("bundle", lambda: envelope, json_output=True) == COMPLETE
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "complete"


def test_run_command_human_mode_emits_nothing(capsys):
    envelope = ResultEnvelope(command="bundle", exit_code=PARTIAL)
    assert run_command("bundle", lambda: envelope, json_output=False) == PARTIAL
    assert capsys.readouterr().out == ""


def test_run_command_maps_keyboard_interrupt(capsys):
    def handler() -> Never:
        raise KeyboardInterrupt

    assert run_command("bundle", handler, json_output=True) == INTERRUPTED
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "interrupted"
