from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


def _is_retryable(exc: BaseException) -> bool:
    # Connection-level failures (timeouts, resets, DNS) are always safe to
    # retry - no request reached the server. HTTP-level failures are only
    # retried for 429 (rate limited) and 5xx (server-side) - a 4xx other
    # than 429 means the request itself was wrong and retrying won't help.
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or exc.response.status_code >= 500
    return False


# For idempotent GET calls only - see the module-level comment on each call
# site for why writes (POST/PATCH) are deliberately left un-retried at this
# layer (per plan §12 item 2: YNAB's import_id dedup and our idempotent
# update payloads already make a next-cycle retry safe for those).
retry_get = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, min=0.5, max=4),
    retry=retry_if_exception(_is_retryable),
)
