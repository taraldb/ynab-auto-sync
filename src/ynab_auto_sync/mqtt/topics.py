from __future__ import annotations

STATE_NAMES = (
    "sync_state",
    "last_run_at",
    "last_success_at",
    "fetched_last_run",
    "imported_last_run",
    "updated_last_run",
    "duplicates_last_run",
    "resolved_deleted_last_run",
    "imported_total",
    "error_message",
    "problem",
    "auth_required",
    "paused",
)

COMMAND_NAMES = ("sync_now", "pause")


def status_topic(base_topic: str) -> str:
    return f"{base_topic}/status"


def state_topic(base_topic: str, name: str) -> str:
    return f"{base_topic}/state/{name}"


def command_topic(base_topic: str, name: str) -> str:
    return f"{base_topic}/command/{name}"
