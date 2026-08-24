import httpx
import respx

from ynab_auto_sync.alerts.base import CycleStats
from ynab_auto_sync.alerts.ntfy_sink import NtfySink
from ynab_auto_sync.config import NtfyConfig

STATS = CycleStats(fetched=5, created=2, updated=1, duplicates=1, resolved_deleted=0)


def make_config(**overrides) -> NtfyConfig:
    return NtfyConfig(server="https://ntfy.sh", topic="test-topic", **overrides)


@respx.mock
async def test_notify_success_with_changes_posts_to_topic_url():
    config = make_config()
    route = respx.post("https://ntfy.sh/test-topic").mock(return_value=httpx.Response(200))
    sink = NtfySink(config)

    await sink.notify_success_with_changes(STATS)

    assert route.called
    request = route.calls[0].request
    assert request.headers["Title"] == "Sync OK - changes"
    assert request.headers["Tags"] == "white_check_mark"
    assert "Authorization" not in request.headers


@respx.mock
async def test_notify_error_sets_auth_required_title_and_higher_priority():
    config = make_config()
    route = respx.post("https://ntfy.sh/test-topic").mock(return_value=httpx.Response(200))
    sink = NtfySink(config)

    await sink.notify_error("token revoked", auth_required=True)

    request = route.calls[0].request
    assert request.headers["Title"] == "Sync failed - re-authentication required"
    assert request.headers["Priority"] == "4"
    assert request.content == b"token revoked"


@respx.mock
async def test_access_token_sent_as_bearer_header():
    config = make_config(access_token="secret-token")
    route = respx.post("https://ntfy.sh/test-topic").mock(return_value=httpx.Response(200))
    sink = NtfySink(config)

    await sink.notify_error("boom", auth_required=False)

    assert route.calls[0].request.headers["Authorization"] == "Bearer secret-token"


@respx.mock
async def test_disabled_event_type_does_not_send_a_request():
    config = make_config(notify_on_success=False)
    route = respx.post("https://ntfy.sh/test-topic").mock(return_value=httpx.Response(200))
    sink = NtfySink(config)

    await sink.notify_success(STATS)

    assert not route.called


@respx.mock
async def test_http_failure_is_swallowed_not_raised():
    config = make_config()
    respx.post("https://ntfy.sh/test-topic").mock(return_value=httpx.Response(500))
    sink = NtfySink(config)

    await sink.notify_error("boom", auth_required=False)  # must not raise


@respx.mock
async def test_connection_error_is_swallowed_not_raised():
    config = make_config()
    respx.post("https://ntfy.sh/test-topic").mock(side_effect=httpx.ConnectError("no network"))
    sink = NtfySink(config)

    await sink.notify_success(STATS)  # must not raise
