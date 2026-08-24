import httpx
import pytest
import respx

from ynab_auto_sync.http_retry import retry_get

URL = "https://example.test/resource"


@retry_get
async def _get(http_client: httpx.AsyncClient) -> httpx.Response:
    response = await http_client.get(URL)
    response.raise_for_status()
    return response


@respx.mock
async def test_retries_on_transient_connection_error_then_succeeds():
    route = respx.get(URL).mock(
        side_effect=[httpx.ConnectError("boom"), httpx.Response(200, json={"ok": True})]
    )
    async with httpx.AsyncClient() as http_client:
        response = await _get(http_client)
    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_retries_on_500_then_succeeds():
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(500), httpx.Response(200, json={"ok": True})]
    )
    async with httpx.AsyncClient() as http_client:
        response = await _get(http_client)
    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_retries_on_429_then_succeeds():
    route = respx.get(URL).mock(
        side_effect=[httpx.Response(429), httpx.Response(200, json={"ok": True})]
    )
    async with httpx.AsyncClient() as http_client:
        response = await _get(http_client)
    assert response.status_code == 200
    assert route.call_count == 2


@respx.mock
async def test_does_not_retry_on_non_429_client_error():
    route = respx.get(URL).mock(return_value=httpx.Response(404))
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(httpx.HTTPStatusError):
            await _get(http_client)
    assert route.call_count == 1


@respx.mock
async def test_gives_up_after_max_attempts():
    route = respx.get(URL).mock(return_value=httpx.Response(500))
    async with httpx.AsyncClient() as http_client:
        with pytest.raises(httpx.HTTPStatusError):
            await _get(http_client)
    assert route.call_count == 3
