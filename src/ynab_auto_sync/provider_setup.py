"""Shared helpers for locating a provider's config and token store.

Used by both the running service (__main__.py) and the manual admin/probe
scripts under scripts/. They have to agree exactly on where tokens live -
if a script wrote to a different path than the service reads, an
interactive BankID login would appear to succeed and then be ignored.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ynab_auto_sync.config import AppConfig, SpareBank1Config

logger = logging.getLogger(__name__)

# The single-provider layout this project used before `providers:` existed.
LEGACY_TOKENS_FILENAME = "tokens.json"
SPAREBANK1_TYPE = "sparebank1"


def token_store_path(state_dir: Path | str, provider_name: str) -> Path:
    """Where one provider's rotating OAuth tokens live.

    Tokens used to be a single `state/tokens.json`, which only worked while
    there was exactly one provider; they are now per-provider under
    `state/tokens/`. The legacy file is MOVED into place on first use for a
    provider named "sparebank1" - without that migration an existing install
    would look like it had never authenticated and push the user back
    through an interactive BankID login for no reason.

    The rename is only attempted when the new path doesn't already exist, so
    it can never clobber newer tokens.
    """
    state_dir = Path(state_dir)
    tokens_dir = state_dir / "tokens"
    tokens_dir.mkdir(parents=True, exist_ok=True)
    new_path = tokens_dir / f"{provider_name}.json"

    legacy_path = state_dir / LEGACY_TOKENS_FILENAME
    if not new_path.exists() and legacy_path.exists() and provider_name == SPAREBANK1_TYPE:
        legacy_path.rename(new_path)
        logger.info("Migrated legacy token store %s -> %s", legacy_path, new_path)
    return new_path


def resolve_sparebank1_provider(
    config: AppConfig, name: str | None = None
) -> tuple[str, SpareBank1Config]:
    """Pick which configured SpareBank1 provider a script should act on.

    Returns (provider_name, provider_config). With exactly one configured,
    `name` may be omitted. With several, omitting it is an error rather than
    a guess - these scripts perform interactive logins and live API probes,
    and silently picking the wrong bank's credentials would be worse than
    refusing.
    """
    candidates = {
        provider_name: provider_config
        for provider_name, provider_config in config.providers.items()
        if provider_config.type == SPAREBANK1_TYPE
    }
    if not candidates:
        raise SystemExit(
            "No SpareBank1 provider is configured. Add one under `providers:` in "
            "config/config.yaml (see config/config.example.yaml)."
        )
    if name is not None:
        if name not in candidates:
            raise SystemExit(
                f"No SpareBank1 provider named {name!r}. Configured: "
                f"{', '.join(sorted(candidates))}"
            )
        return name, candidates[name]
    if len(candidates) > 1:
        raise SystemExit(
            f"Several SpareBank1 providers are configured ({', '.join(sorted(candidates))}) "
            "- pass --provider to choose one."
        )
    return next(iter(candidates.items()))
