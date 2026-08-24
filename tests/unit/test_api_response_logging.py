import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ynab_auto_sync.api_response_logging import (
    ApiResponseLogger,
    _redact_body,
    _redact_headers,
    build_api_response_logger,
    prune_old_logs,
)
from ynab_auto_sync.config import ApiResponseLoggingConfig


def test_redact_headers_strips_authorization_case_insensitively():
    headers = httpx.Headers({"Authorization": "Bearer secret-token", "Accept": "application/json"})
    result = _redact_headers(headers)
    assert result["authorization"] == "[REDACTED]"
    assert result["accept"] == "application/json"


def test_redact_body_strips_known_secret_fields():
    body = {
        "client_secret": "shh",
        "refresh_token": "shh",
        "access_token": "shh",
        "password": "shh",
        "grant_type": "refresh_token",
        "nested": {"access_token": "shh-nested", "keep": "me"},
    }
    result = _redact_body(body)
    assert result["client_secret"] == "[REDACTED]"
    assert result["refresh_token"] == "[REDACTED]"
    assert result["access_token"] == "[REDACTED]"
    assert result["password"] == "[REDACTED]"
    assert result["grant_type"] == "refresh_token"
    assert result["nested"]["access_token"] == "[REDACTED]"
    assert result["nested"]["keep"] == "me"


def test_redact_body_passes_through_non_dict():
    assert _redact_body(None) is None
    assert _redact_body("plain text") == "plain text"
    assert _redact_body([{"password": "shh"}]) == [{"password": "[REDACTED]"}]


async def test_on_response_writes_redacted_file_for_get(tmp_path: Path):
    api_logger = ApiResponseLogger(tmp_path)
    request = httpx.Request(
        "GET",
        "https://example.test/resource",
        headers={"Authorization": "Bearer secret-token"},
    )
    response = httpx.Response(200, request=request, json={"ok": True})

    await api_logger.on_response(response)

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record["request"]["method"] == "GET"
    assert record["request"]["headers"]["authorization"] == "[REDACTED]"
    assert record["response"]["status_code"] == 200
    assert record["response"]["body"] == {"ok": True}


async def test_on_response_redacts_post_body(tmp_path: Path):
    api_logger = ApiResponseLogger(tmp_path)
    request = httpx.Request(
        "POST",
        "https://example.test/oauth/token",
        json={"client_secret": "shh", "grant_type": "refresh_token"},
    )
    response = httpx.Response(200, request=request, json={"access_token": "shh-response"})

    await api_logger.on_response(response)

    files = list(tmp_path.glob("*.json"))
    record = json.loads(files[0].read_text())
    assert record["request"]["body"]["client_secret"] == "[REDACTED]"
    assert record["request"]["body"]["grant_type"] == "refresh_token"
    assert record["response"]["body"]["access_token"] == "[REDACTED]"


async def test_on_response_never_raises_on_write_failure(tmp_path: Path, monkeypatch):
    api_logger = ApiResponseLogger(tmp_path)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ApiResponseLogger, "_write", _boom)
    request = httpx.Request("GET", "https://example.test/resource")
    response = httpx.Response(200, request=request, json={"ok": True})

    await api_logger.on_response(response)  # must not raise


def test_build_api_response_logger_disabled_returns_none(tmp_path: Path):
    config = ApiResponseLoggingConfig(enabled=False)
    assert build_api_response_logger(config, tmp_path) is None


def test_build_api_response_logger_enabled_creates_dir(tmp_path: Path):
    config = ApiResponseLoggingConfig(enabled=True)
    result = build_api_response_logger(config, tmp_path)
    assert result is not None
    assert (tmp_path / "api_logs").is_dir()


def test_prune_old_logs_deletes_only_stale_files(tmp_path: Path):
    old_file = tmp_path / "old.json"
    new_file = tmp_path / "new.json"
    old_file.write_text("{}")
    new_file.write_text("{}")

    old_time = time.time() - 10 * 86400
    os.utime(old_file, (old_time, old_time))

    cutoff = datetime.now(UTC) - timedelta(days=7)
    pruned = prune_old_logs(tmp_path, cutoff)

    assert pruned == 1
    assert not old_file.exists()
    assert new_file.exists()


def test_prune_old_logs_missing_dir_returns_zero(tmp_path: Path):
    assert prune_old_logs(tmp_path / "does-not-exist", datetime.now(UTC)) == 0
