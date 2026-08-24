from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ynab_auto_sync.config import SpareBank1Config
from ynab_auto_sync.providers.base import ProviderAuthRequiredError
from ynab_auto_sync.state import JsonStateStore

logger = logging.getLogger(__name__)

BASE_URL = "https://api.sparebank1.no"
AUTHORIZE_URL = f"{BASE_URL}/oauth/authorize"
TOKEN_URL = f"{BASE_URL}/oauth/token"
HELLOWORLD_URL = f"{BASE_URL}/common/helloworld"
ACCEPT_HEADER = "application/vnd.sparebank1.v1+json; charset=utf-8"

# The docs' own example token response shows expires_in=15552000 (180 days)
# directly contradicting the surrounding prose, which states the access
# token is valid for 10 minutes. We trust the prose and apply a safety
# margin under it rather than the field's numeric value.
ACCESS_TOKEN_LIFETIME = timedelta(minutes=9)


class AuthRequiredError(ProviderAuthRequiredError):
    """Raised when there is no usable refresh token and a human must redo
    the interactive BankID login via scripts/auth_setup.py.

    Subclasses the provider-neutral ProviderAuthRequiredError so the
    scheduler can surface "a human needs to log back in" for ANY provider
    without importing a bank-specific exception type. Kept as its own named
    subclass because the remedy is SpareBank1-specific (the BankID flow in
    scripts/auth_setup.py), which the log message tells the user about.
    """


def build_authorize_url(config: SpareBank1Config, state: str) -> str:
    params = {
        "client_id": config.client_id,
        "state": state,
        "redirect_uri": config.redirect_uri,
        "response_type": "code",
    }
    if config.fin_inst:
        params["finInst"] = config.fin_inst
    return str(httpx.URL(AUTHORIZE_URL, params=params))


def _parse_token_response(payload: dict[str, Any]) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "obtained_at": now.isoformat(),
        "expires_at": (now + ACCESS_TOKEN_LIFETIME).isoformat(),
    }


async def exchange_code(
    http_client: httpx.AsyncClient,
    config: SpareBank1Config,
    code: str,
    state: str,
) -> dict[str, Any]:
    response = await http_client.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "state": state,
            "redirect_uri": config.redirect_uri,
        },
    )
    response.raise_for_status()
    return _parse_token_response(response.json())


async def refresh_access_token(
    http_client: httpx.AsyncClient,
    config: SpareBank1Config,
    refresh_token: str,
) -> dict[str, Any]:
    response = await http_client.post(
        TOKEN_URL,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    if response.status_code == 400:
        # SpareBank1's own docs note OAuth errors are deliberately vague
        # ("Oh no, something went wrong!") to avoid helping attackers probe
        # valid parameter values - a 400 here is our best signal that the
        # refresh token itself is dead and a human needs to re-auth.
        raise AuthRequiredError(
            f"SpareBank1 refresh token was rejected (HTTP 400): {response.text}"
        )
    response.raise_for_status()
    return _parse_token_response(response.json())


async def smoke_test(http_client: httpx.AsyncClient, access_token: str) -> bool:
    response = await http_client.get(
        HELLOWORLD_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": ACCEPT_HEADER},
    )
    return response.status_code == 200


class TokenStore:
    """Persists rotating SpareBank1 OAuth tokens to state/tokens.json and
    hands out a valid access token, refreshing (and re-persisting) as
    needed. Always re-reads from disk rather than trusting an in-memory
    copy, so a fresh token written by scripts/auth_setup.py while this
    process is running is picked up on the very next call - no restart
    needed.
    """

    def __init__(self, state_store: JsonStateStore, config: SpareBank1Config):
        self._state_store = state_store
        self._config = config

    def save(self, tokens: dict[str, Any]) -> None:
        self._state_store.write(tokens)

    async def ensure_valid_access_token(self, http_client: httpx.AsyncClient) -> str:
        tokens = self._state_store.read()
        if not tokens or "refresh_token" not in tokens:
            raise AuthRequiredError(
                "No SpareBank1 tokens found. Run scripts/auth_setup.py to complete "
                "the interactive BankID login first."
            )

        expires_at = datetime.fromisoformat(tokens["expires_at"])
        if datetime.now(UTC) < expires_at - timedelta(seconds=60):
            return tokens["access_token"]

        logger.info("SpareBank1 access token expired or near-expiry, refreshing")
        new_tokens = await refresh_access_token(
            http_client, self._config, tokens["refresh_token"]
        )
        # Persist immediately - the old refresh token is now dead server-side,
        # so losing the new one before it hits disk would be unrecoverable
        # without a human redoing BankID login.
        self.save(new_tokens)
        return new_tokens["access_token"]
