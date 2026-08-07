"""Managed LDraw parts library: sync, validation, weekly policy."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta

import pytest

from legolization import parts_library
from legolization.errors import ConfigurationError
from legolization.parts_library import (
    CHECK_INTERVAL,
    FetchResponse,
    library_dir,
    load_metadata,
    managed_root,
    sync,
)

NOW = datetime(2026, 8, 6, tzinfo=UTC)


def _archive(*, corrupt: bool = False, missing_config: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ldraw/parts/3001.dat", "0 brick 2x4\n")
        archive.writestr("ldraw/p/stud.dat", "0 stud\n")
        if not missing_config:
            archive.writestr("ldraw/LDConfig.ldr", "0 colours\n")
    payload = bytearray(buffer.getvalue())
    if corrupt:
        payload[-10] ^= 0xFF
    return bytes(payload)


def _fetcher(
    payload: bytes,
    *,
    status: int = 200,
    calls: list | None = None,
) -> parts_library.Fetcher:
    def _fetch(url: str, headers: dict[str, str]) -> FetchResponse:
        if calls is not None:
            calls.append(headers)
        return FetchResponse(
            status=status,
            headers={"ETag": '"v1"', "Last-Modified": "yesterday"},
            payload=payload,
        )

    return _fetch


def test_managed_root_honours_data_home(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGOLIZATION_DATA_HOME", str(tmp_path / "custom"))
    assert managed_root() == tmp_path / "custom"


def test_first_sync_downloads_validates_and_records(tmp_path):
    result = sync(tmp_path, fetch=_fetcher(_archive()), now=lambda: NOW)
    assert result.status == "downloaded"
    assert result.ldraw_dir == tmp_path / "ldraw"
    assert library_dir(tmp_path) == tmp_path / "ldraw"
    metadata = load_metadata(tmp_path)
    assert metadata is not None
    assert metadata.source_url == parts_library.LIBRARY_URL
    assert metadata.etag == '"v1"'
    assert metadata.file_count == 3
    assert metadata.downloaded_at == NOW.isoformat()


def test_fresh_library_skips_the_network_within_a_week(tmp_path):
    sync(tmp_path, fetch=_fetcher(_archive()), now=lambda: NOW)

    def _explode(url: str, headers: dict[str, str]) -> FetchResponse:
        msg = "network must not be touched"
        raise AssertionError(msg)

    soon = NOW + CHECK_INTERVAL - timedelta(hours=1)
    result = sync(tmp_path, fetch=_explode, now=lambda: soon)
    assert result.status == "fresh"


def test_stale_check_uses_conditional_headers_and_not_modified(tmp_path):
    sync(tmp_path, fetch=_fetcher(_archive()), now=lambda: NOW)
    calls: list[dict[str, str]] = []
    later = NOW + CHECK_INTERVAL + timedelta(days=1)
    result = sync(
        tmp_path,
        fetch=_fetcher(b"", status=304, calls=calls),
        now=lambda: later,
    )
    assert result.status == "fresh"
    assert calls[0]["If-None-Match"] == '"v1"'
    metadata = load_metadata(tmp_path)
    assert metadata is not None
    assert metadata.last_check_at == later.isoformat()
    assert metadata.downloaded_at == NOW.isoformat()


def test_network_failure_with_valid_library_keeps_it(tmp_path):
    sync(tmp_path, fetch=_fetcher(_archive()), now=lambda: NOW)

    def _down(url: str, headers: dict[str, str]) -> FetchResponse:
        msg = "connection refused"
        raise OSError(msg)

    later = NOW + CHECK_INTERVAL + timedelta(days=1)
    result = sync(tmp_path, fetch=_down, now=lambda: later)
    assert result.status == "offline-kept"
    assert result.ldraw_dir == tmp_path / "ldraw"
    assert any("keeping the existing library" in warning for warning in result.warnings)


def test_network_failure_without_library_is_an_error(tmp_path):
    def _down(url: str, headers: dict[str, str]) -> FetchResponse:
        msg = "connection refused"
        raise OSError(msg)

    with pytest.raises(ConfigurationError, match="download failed"):
        sync(tmp_path, fetch=_down, now=lambda: NOW)


@pytest.mark.parametrize("kwargs", [{"corrupt": True}, {"missing_config": True}])
def test_invalid_archive_without_library_is_an_error(tmp_path, kwargs):
    with pytest.raises(ConfigurationError):
        sync(tmp_path, fetch=_fetcher(_archive(**kwargs)), now=lambda: NOW)


def test_invalid_archive_keeps_a_valid_library(tmp_path):
    sync(tmp_path, fetch=_fetcher(_archive()), now=lambda: NOW)
    later = NOW + CHECK_INTERVAL + timedelta(days=1)
    result = sync(
        tmp_path,
        fetch=_fetcher(_archive(missing_config=True)),
        now=lambda: later,
    )
    assert result.status == "offline-kept"
    assert library_dir(tmp_path) is not None


def test_force_redownloads_even_when_fresh(tmp_path):
    sync(tmp_path, fetch=_fetcher(_archive()), now=lambda: NOW)
    result = sync(
        tmp_path,
        fetch=_fetcher(_archive()),
        now=lambda: NOW + timedelta(hours=1),
        force=True,
    )
    assert result.status == "updated"


def test_detect_ldraw_dir_prefers_managed_library(tmp_path, monkeypatch):
    from legolization.instructions.render import detect_ldraw_dir

    monkeypatch.setenv("LEGOLIZATION_DATA_HOME", str(tmp_path))
    monkeypatch.delenv("LDRAWDIR", raising=False)
    sync(tmp_path, fetch=_fetcher(_archive()), now=lambda: NOW)
    assert detect_ldraw_dir(env={}) == tmp_path / "ldraw"


def test_parts_sync_cli_emits_single_envelope(tmp_path, monkeypatch, capsys):
    from legolization.cli import main

    monkeypatch.setenv("LEGOLIZATION_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(
        parts_library,
        "_urllib_fetch",
        _fetcher(_archive()),
    )
    import json

    assert main(["parts", "sync", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "parts sync"
    assert payload["status"] == "complete"
    assert payload["data"]["status"] == "downloaded"
    assert payload["data"]["ldraw_dir"].endswith("ldraw")
