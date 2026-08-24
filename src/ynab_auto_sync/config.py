from __future__ import annotations

import math
from pathlib import Path
from typing import Annotated, Literal

import yaml
from croniter import croniter
from pydantic import BaseModel, Field, model_validator


class SpareBank1Config(BaseModel):
    """Credentials for one SpareBank1 connection.

    `type` is the discriminator that selects which TransactionProvider
    implementation gets built for this entry (see providers/registry.py). It
    must match SpareBank1Provider.type_name(). More than one entry of the
    same type is allowed and expected - the SpareBank1 alliance is many
    separate banks, so two sets of credentials are a realistic setup.
    """

    type: Literal["sparebank1"] = "sparebank1"
    enabled: bool = True
    client_id: str
    client_secret: str
    redirect_uri: str
    fin_inst: str = ""


# Discriminated on `type` so a second provider implementation slots in by
# adding its config model here and to this union - no changes anywhere else
# in the config layer. One member today; the machinery is what matters.
ProviderConfig = Annotated[SpareBank1Config, Field(discriminator="type")]


class YnabConfig(BaseModel):
    personal_access_token: str
    budgets: dict[str, str]


class AccountMapping(BaseModel):
    sparebank1_account_key: str
    ynab_account_id: str
    ynab_budget: str
    display_name: str = ""
    # Matches a file-import transformer's name() (see sync/file_import) so a
    # file upload with no explicit account override can auto-resolve its
    # target account. Blank means "not an auto-match target for any
    # file-import source" - most accounts will leave this unset.
    import_source_name: str = ""


class SyncConfig(BaseModel):
    # A single cron expression rather than a fixed interval - nothing needs
    # fetching overnight. Default: 06:00/08:00/10:00/12:00/16:00/20:00 daily.
    cron_expression: str = "0 6,8,10,12,16,20 * * *"
    lookback_overlap_hours: int = 72
    initial_backfill_days: int = 30
    # How long a BOOKED tracked_transactions row survives before it's
    # eligible for pruning (see state_db.prune_booked_transactions). Must
    # stay past the fetch horizon (initial_backfill_days +
    # lookback_overlap_hours) - see the validator below - since pruning a
    # row still inside that window would let a cycle's own overlap re-fetch
    # recreate it as a false "new" transaction.
    retention_days: int = 270
    # Two legs of the same real transfer between your own accounts don't
    # always post on the same calendar day - confirmed live that a
    # credit-card payment can take until the "next working day" to become
    # visible, which can be more than 1 calendar day across a weekend. This
    # is the max date difference (in either direction) between two
    # candidate legs still considered the same transfer (see
    # sync/transfers.py). Tunable without a code change if real-world
    # matching turns out to need more or less slack than this default.
    transfer_match_window_days: int = 4
    # Shared by transfer_match_window_days above AND manual_match_window_days
    # below - both windows exist for the same reason (a transaction can
    # settle a few days late because of weekends/holidays), so one toggle
    # rather than two. "working_days" counts business days (Mon-Fri,
    # excluding Norwegian public holidays, via the workalendar library) -
    # see sync/date_window.py. Default "calendar_days" preserves today's
    # exact transfer-matching behavior for every existing install.
    match_window_unit: Literal["calendar_days", "working_days"] = "calendar_days"
    # Opt-in (0 = disabled, the default): how many days of slack, in
    # match_window_unit's terms, to look for a pre-existing, manually-typed
    # YNAB transaction (no import_id) with the exact same amount as an
    # incoming bank transaction on the same account, before creating a new
    # transaction. See sync/engine.py's _classify() "matched_manual" branch
    # and CLAUDE.md's "Manual-transaction matching" section for the full
    # design and its known false-positive risk (no account-number
    # cross-check exists for this case, unlike transfer-matching).
    manual_match_window_days: int = 0
    # A failed cycle (provider fetch error, or a YNAB create/update call
    # failing) is retried with exponential backoff: base * 2**attempt,
    # capped at retry_backoff_max_seconds. scheduler.py stops scheduling
    # further retries once the next backoff delay would land at or after
    # the next scheduled cron fire - that fire will attempt a fresh cycle
    # anyway, so a retry racing it would be redundant. See CLAUDE.md's
    # retry/backoff section.
    retry_backoff_base_seconds: float = 60
    retry_backoff_max_seconds: float = 1800


class GuiConfig(BaseModel):
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8080
    static_dir: str = "frontend/dist"


class MqttConfig(BaseModel):
    # Absent `mqtt:` in config.yaml, or `enabled: false`, means the app runs
    # with a NullSink instead - MQTT is entirely optional, not required
    # infrastructure. See notifications/ for the sink abstraction.
    enabled: bool = True
    host: str
    port: int = 1883
    username: str = ""
    password: str = ""
    tls: bool = False
    base_topic: str = "ynab_auto_sync"
    discovery_prefix: str = "homeassistant"


class NtfyConfig(BaseModel):
    """Push discrete per-cycle events (success / success-with-changes /
    error) to ntfy.sh, or a self-hosted ntfy server, via alerts.NtfySink.
    Independent of mqtt/gui - this is the "notification" seam (alerts/),
    not the "state" seam (notifications/), which is unaffected by this
    config entirely. See CLAUDE.md for the distinction."""

    enabled: bool = True
    server: str = "https://ntfy.sh"
    topic: str
    # Only needed for a private/protected topic - blank means an
    # unauthenticated POST (fine for ntfy.sh's default public-but-obscure
    # topics).
    access_token: str = ""
    notify_on_success: bool = False
    notify_on_success_with_changes: bool = True
    notify_on_error: bool = True


class NotificationsConfig(BaseModel):
    # Nested (not a flat top-level `ntfy:` field) so a second future
    # provider (Slack/Discord/Pushover/...) slots in as another field here
    # without a new top-level AppConfig field each time.
    ntfy: NtfyConfig | None = None


class LoggingConfig(BaseModel):
    level: str = "INFO"


class ApiResponseLoggingConfig(BaseModel):
    """Optional debug aid: write every raw provider/YNAB HTTP response to a
    gitignored file under state/api_logs/ for troubleshooting. Off by
    default - this is a debugging feature, not something a normal
    deployment needs running. Authorization headers and known-secret body
    fields (client_secret/refresh_token/access_token/password) are always
    redacted before anything is written - see api_response_logging.py's
    redact(). retention_days controls how long log files are kept before
    the scheduler's regular pruning pass (see scheduler.py) deletes them,
    same shape as sync.retention_days but for files instead of DB rows."""

    enabled: bool = False
    retention_days: int = Field(default=7, ge=1)


class AppConfig(BaseModel):
    # Keyed by an arbitrary user-chosen name (the key is what
    # account_mappings.provider stores), so two entries of the same `type`
    # can coexist - e.g. two SpareBank1-alliance banks with separate
    # credentials.
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    ynab: YnabConfig
    # Legacy static mappings. Read ONCE at startup to seed the
    # account_mappings table, then ignored forever - the table is
    # authoritative so the GUI can edit mappings at runtime. Kept only so an
    # existing install migrates itself; new installs can omit it entirely.
    accounts: list[AccountMapping] = Field(default_factory=list)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    gui: GuiConfig = Field(default_factory=GuiConfig)
    mqtt: MqttConfig | None = None
    notifications: NotificationsConfig | None = None
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    api_response_logging: ApiResponseLoggingConfig = Field(default_factory=ApiResponseLoggingConfig)

    @model_validator(mode="after")
    def _validate_account_references(self) -> AppConfig:
        errors: list[str] = []

        # NOTE: duplicate-account and duplicate-import_source_name checks
        # deliberately do NOT live here any more. Mappings are mutable at
        # runtime, so those invariants are enforced on every write in
        # StateDB.create_mapping/update_mapping (raising
        # MappingValidationError, surfaced as HTTP 422) - a startup-only
        # check would be trivially bypassed by the GUI. What remains here is
        # only what config.yaml alone can still get wrong.
        for account in self.accounts:
            if account.ynab_budget not in self.ynab.budgets:
                name = account.display_name or account.sparebank1_account_key
                available = ", ".join(sorted(self.ynab.budgets)) or "(none configured)"
                errors.append(
                    f"Account '{name}' references unknown YNAB budget alias "
                    f"'{account.ynab_budget}' - available aliases: {available}"
                )

        if not croniter.is_valid(self.sync.cron_expression):
            errors.append(
                f"sync.cron_expression '{self.sync.cron_expression}' is not a valid cron "
                "expression"
            )

        fetch_horizon_days = self.sync.initial_backfill_days + math.ceil(
            self.sync.lookback_overlap_hours / 24
        )
        if self.sync.retention_days < fetch_horizon_days:
            errors.append(
                f"sync.retention_days ({self.sync.retention_days}) must be >= "
                f"initial_backfill_days + ceil(lookback_overlap_hours / 24) "
                f"({fetch_horizon_days}) - pruning inside the still-reachable fetch "
                "horizon would let a normal cycle's overlap window resurrect a pruned "
                "transaction as a false 'new' one"
            )

        if errors:
            raise ValueError("; ".join(errors))
        return self


DEFAULT_CONFIG_PATH = Path("config/config.yaml")


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> AppConfig:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found at {path}. Copy config/config.example.yaml to "
            f"{path} and fill in your credentials."
        )
    raw = yaml.safe_load(path.read_text()) or {}
    return AppConfig.model_validate(raw)
