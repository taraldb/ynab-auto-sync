from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ynab_auto_sync.config import ApiResponseLoggingConfig

logger = logging.getLogger(__name__)

# Header/body keys that must never reach disk, matched case-insensitively.
# This client is shared with providers/sparebank1/auth.py's OAuth token
# exchange/refresh calls (client_secret/refresh_token in POST bodies) and
# every authenticated request (Authorization: Bearer <token> header) - an
# httpx event hook attached to the shared client sees those too, so
# redaction here is load-bearing, not decorative. Never remove it.
_REDACTED_HEADER_KEYS = {"authorization"}
_REDACTED_BODY_KEYS = {"client_secret", "refresh_token", "access_token", "password"}
_REDACTED_VALUE = "[REDACTED]"


def _redact_headers(headers: httpx.Headers) -> dict[str, str]:
    return {
        key: (_REDACTED_VALUE if key.lower() in _REDACTED_HEADER_KEYS else value)
        for key, value in headers.items()
    }


def _redact_body(body: Any) -> Any:
    if isinstance(body, dict):
        return {
            key: (_REDACTED_VALUE if key.lower() in _REDACTED_BODY_KEYS else _redact_body(value))
            for key, value in body.items()
        }
    if isinstance(body, list):
        return [_redact_body(item) for item in body]
    return body


def _parse_body(content: bytes) -> Any:
    if not content:
        return None
    try:
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return content.decode("utf-8", errors="replace")


class ApiResponseLogger:
    """Writes one redacted JSON file per HTTP response for debugging. Wired
    in as an httpx response event hook (see __main__.py) so it covers every
    provider/YNAB call site with no changes to either client module. Every
    write is best-effort - a logging failure must never break a real sync
    cycle, so all I/O here is caught and logged, never raised."""

    def __init__(self, log_dir: Path):
        self._log_dir = log_dir
        self._log_dir.mkdir(parents=True, exist_ok=True)

    async def on_response(self, response: httpx.Response) -> None:
        try:
            # Response event hooks fire before the body is read for a
            # non-streaming request - must read before .content/.text is
            # accessible.
            await response.aread()
            request = response.request
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "request": {
                    "method": request.method,
                    "url": str(request.url),
                    "headers": _redact_headers(request.headers),
                    "body": _redact_body(_parse_body(request.content)),
                },
                "response": {
                    "status_code": response.status_code,
                    "headers": _redact_headers(response.headers),
                    "body": _redact_body(_parse_body(response.content)),
                },
            }
            self._write(record, request)
        except Exception:
            logger.exception("Failed to log API response (non-fatal)")

    def _write(self, record: dict[str, Any], request: httpx.Request) -> None:
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        suffix = uuid.uuid4().hex[:8]
        host = request.url.host or "unknown"
        filename = f"{timestamp}_{suffix}_{host}_{request.method}.json"
        (self._log_dir / filename).write_text(json.dumps(record, indent=2, default=str))


def build_api_response_logger(
    config: ApiResponseLoggingConfig, base_dir: Path
) -> ApiResponseLogger | None:
    """Factory mirroring __main__.py's _build_sink()/_build_notifier()
    style: None when disabled (the common case - this is a debug feature,
    off by default), a real logger writing under base_dir/api_logs
    otherwise."""
    if not config.enabled:
        return None
    return ApiResponseLogger(base_dir / "api_logs")


def prune_old_logs(log_dir: Path, cutoff: datetime) -> int:
    """Delete API response log files older than cutoff (by mtime). Mirrors
    state_db.py's prune_booked_transactions/prune_audit_events shape - a
    plain count of files removed, no exceptions raised (the caller,
    scheduler.py, wraps this in the same non-fatal try/except it already
    uses for the DB pruning calls)."""
    if not log_dir.is_dir():
        return 0
    pruned = 0
    cutoff_ts = cutoff.timestamp()
    for path in log_dir.glob("*.json"):
        if path.stat().st_mtime < cutoff_ts:
            path.unlink()
            pruned += 1
    return pruned
