from __future__ import annotations

import logging

import httpx

from ynab_auto_sync.alerts.base import CycleStats, EventNotifier
from ynab_auto_sync.config import NtfyConfig

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT_SECONDS = 10.0


def _stats_body(stats: CycleStats) -> str:
    return (
        f"Fetched {stats.fetched}, created {stats.created}, updated {stats.updated}, "
        f"{stats.duplicates} duplicate(s) skipped, {stats.resolved_deleted} resolved as deleted."
    )


class NtfySink(EventNotifier):
    """EventNotifier backed by ntfy.sh (or a self-hosted ntfy server) -
    https://ntfy.sh/{topic}, a plain HTTP POST per push, no persistent
    connection to manage. A short-lived httpx.AsyncClient per call is
    deliberate, not an oversight: this fires at most a few times per cron
    cycle, far too infrequent to justify holding a pooled client across the
    scheduler's whole lifetime the way MqttSink holds its broker
    connection.

    Each method checks its own config.notify_on_* flag first (no-op if
    that event type is disabled) and never raises - an HTTP failure (bad
    network, ntfy.sh down, wrong topic) is logged and swallowed, matching
    EventNotifier's best-effort contract.
    """

    def __init__(self, config: NtfyConfig):
        self._config = config

    async def notify_success(self, stats: CycleStats) -> None:
        if not self._config.notify_on_success:
            return
        await self._send(
            title="Sync OK",
            body=_stats_body(stats),
            tags="white_check_mark",
            priority="3",
        )

    async def notify_success_with_changes(self, stats: CycleStats) -> None:
        if not self._config.notify_on_success_with_changes:
            return
        await self._send(
            title="Sync OK - changes",
            body=_stats_body(stats),
            tags="white_check_mark",
            priority="3",
        )

    async def notify_error(self, message: str, *, auth_required: bool) -> None:
        if not self._config.notify_on_error:
            return
        title = "Sync failed - re-authentication required" if auth_required else "Sync failed"
        await self._send(
            title=title,
            body=message,
            tags="rotating_light",
            priority="4",
        )

    async def _send(self, *, title: str, body: str, tags: str, priority: str) -> None:
        headers = {"Title": title, "Tags": tags, "Priority": priority}
        if self._config.access_token:
            headers["Authorization"] = f"Bearer {self._config.access_token}"
        url = f"{self._config.server.rstrip('/')}/{self._config.topic}"
        try:
            async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(url, content=body.encode(), headers=headers)
                response.raise_for_status()
        except httpx.HTTPError:
            logger.warning("ntfy push to %r failed (non-fatal, dropping this event)", url)
