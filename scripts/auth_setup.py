#!/usr/bin/env python3
"""One-time interactive SpareBank1 BankID login.

Run this manually on a machine with a real browser (your laptop, or a
container run with -it and a browser available to open the printed URL) -
NOT inside the unattended docker-host deployment, which has no browser.

Usage:
    python scripts/auth_setup.py [--config config/config.yaml] [--state-dir state]

After a successful run, state/tokens.json contains the access/refresh
tokens. If the long-running service is already deployed, it will pick up
the new tokens on its next sync cycle without needing a restart - copy
state/tokens.json onto the docker host's state volume if this script was
run somewhere else.
"""

from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ynab_auto_sync.config import load_config
from ynab_auto_sync.provider_setup import (
    resolve_sparebank1_provider,
    token_store_path,
)
from ynab_auto_sync.providers.sparebank1 import auth as sb1_auth
from ynab_auto_sync.state import JsonStateStore

CALLBACK_RESULT: dict[str, str] = {}


def make_callback_handler(expected_path: str):
    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                return
            params = parse_qs(parsed.query)
            CALLBACK_RESULT["code"] = params.get("code", [""])[0]
            CALLBACK_RESULT["state"] = params.get("state", [""])[0]
            CALLBACK_RESULT["error"] = params.get("error", [""])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body><h1>Authentication received</h1>"
                b"<p>You may close this window and return to the terminal.</p>"
                b"</body></html>"
            )

        def log_message(self, format, *args):
            return  # silence default request logging

    return CallbackHandler


async def exchange_and_persist(provider_config, code: str, state: str, state_store: JsonStateStore):
    async with httpx.AsyncClient(timeout=30) as http_client:
        tokens = await sb1_auth.exchange_code(http_client, provider_config, code, state)
        state_store.write(tokens)

        print("Verifying token against /common/helloworld ...")
        ok = await sb1_auth.smoke_test(http_client, tokens["access_token"])
        if ok:
            print("Smoke test OK - SpareBank1 API call succeeded with the new token.")
        else:
            print(
                "WARNING: smoke test call did not return 200. Tokens were saved "
                "anyway; investigate before relying on them."
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config/config.yaml")
    parser.add_argument("--state-dir", default="state")
    parser.add_argument(
        "--provider",
        default=None,
        help="Which configured SpareBank1 provider to use "
             "(required only if several are configured)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    provider_name, provider_config = resolve_sparebank1_provider(config, args.provider)
    state_store = JsonStateStore(token_store_path(args.state_dir, provider_name))

    state_nonce = secrets.token_urlsafe(16)
    authorize_url = sb1_auth.build_authorize_url(provider_config, state_nonce)

    redirect = urlparse(provider_config.redirect_uri)
    host = redirect.hostname or "localhost"
    port = redirect.port or 80
    callback_path = redirect.path or "/callback"

    print("Opening browser for SpareBank1 BankID login...")
    print(f"If it doesn't open automatically, visit:\n\n  {authorize_url}\n")
    webbrowser.open(authorize_url)

    server = HTTPServer((host, port), make_callback_handler(callback_path))
    print(f"Waiting for the redirect on {host}:{port}{callback_path} ...")
    server.handle_request()  # blocks for exactly one request, then returns

    if CALLBACK_RESULT.get("error"):
        print(f"Authorization failed: {CALLBACK_RESULT['error']}")
        return 1

    code = CALLBACK_RESULT.get("code")
    returned_state = CALLBACK_RESULT.get("state")
    if not code:
        print("No authorization code received.")
        return 1
    if returned_state != state_nonce:
        print("State mismatch - possible CSRF or stale request. Aborting.")
        return 1

    print("Exchanging authorization code for tokens (must happen within ~2 minutes)...")
    asyncio.run(exchange_and_persist(provider_config, code, returned_state, state_store))

    print(f"\nTokens saved to {state_store.path}")
    print("You can now start (or restart) the ynab-auto-sync service.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
