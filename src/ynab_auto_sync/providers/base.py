from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# Optional, purely observational hook for skip diagnostics (backs the GUI's
# Audit Log "skipped" category) - mirrors engine.py's ProgressCallback shape
# exactly. A provider that never calls it is still fully conformant: this
# defaults to None everywhere, so no existing/future provider implementation
# is required to support it. Called once per row a provider drops for being
# structurally malformed (never for a row skipped by ordinary, expected
# per-cycle logic like a still-PENDING transaction - see fetch()'s docstring
# below) - reason is a short human-readable string, context is whatever the
# provider has on hand to help diagnose it (e.g. the raw row).
SkipCallback = Callable[[str, dict[str, Any]], Awaitable[None]]


async def report_skip(
    on_skip: SkipCallback | None, reason: str, context: dict[str, Any]
) -> None:
    """Best-effort skip notification - same contract as engine.py's
    _report(): must never affect fetch()'s own control flow, so any
    exception from the callback is caught and logged here, never raised.
    A free function (not a TransactionProvider method) so any provider can
    call it without providers/ needing to import from sync/engine.py -
    that layering (providers sit below sync/) is deliberate, see CLAUDE.md.
    """
    if on_skip is None:
        return
    try:
        await on_skip(reason, context)
    except Exception:
        logger.exception("Skip callback failed (non-fatal, fetch continues)")


class BookingStatus(StrEnum):
    """Mirrors the exact literal strings already persisted in the SQLite
    tracked_transactions.booking_status column (see state_db.py) and
    compared elsewhere as plain string literals (e.g.
    prune_booked_transactions()'s WHERE booking_status = 'BOOKED'). These
    three values are a stable on-disk/API contract, not just an in-memory
    convenience - do not rename or reorder them, and do not add a member
    without checking every existing string comparison against
    'PENDING'/'BOOKED'/'DELETED' first.
    """

    PENDING = "PENDING"
    BOOKED = "BOOKED"
    DELETED = "DELETED"


@dataclass(frozen=True)
class ProviderAccount:
    """One account as reported by a provider - e.g. one SpareBank1 checking
    account or credit card. provider_account_id is the provider's own
    identifier for the account (e.g. SpareBank1's accountKey); it is *not*
    itself the dedup key for transactions (see NormalizedTransaction.
    tracking_key) - it only scopes one."""

    provider_account_id: str
    display_name: str
    # Provider-defined, no fixed vocabulary across providers - e.g.
    # SpareBank1 confirmed live to send "USER"/"SAVING"/"CREDITCARD", not
    # "checking"/"credit_card" as an earlier, unverified version of this
    # comment guessed. Display-only; nothing in this codebase branches on
    # a specific value.
    account_type: str
    currency: str


@dataclass
class NormalizedTransaction:
    """A provider-agnostic transaction, already run through one provider's
    field-mapping/dedup-key logic. This is the shape engine.py is meant to
    consume regardless of which provider produced it - each provider owns
    turning its own API's messy reality into exactly this.
    """

    # The identifier used to detect "this is the same real-world
    # transaction across polls." A real, live production bug (see
    # CLAUDE.md invariant 2) proved that SpareBank1's own top-level `id`
    # field changes on EVERY poll of a still-pending credit-card
    # transaction - two fetches of the same real charge, hours apart,
    # returned completely different `id` strings, which caused continuous
    # duplicate imports until the bug was found and fixed. A provider must
    # derive tracking_key from a field proven stable across polls of the
    # *same* real-world transaction (verified live against that provider's
    # real API, not assumed from docs - see CLAUDE.md's "Standing rules"),
    # never from a field that might just be a per-response/per-page id.
    # Scope it by provider_account_id: a provider's own "stable" id field
    # may warn (as SpareBank1's nonUniqueId literally does by name) that it
    # is not guaranteed globally unique across accounts.
    tracking_key: str

    # The authoritative no-duplicates guarantee submitted to YNAB as the
    # transaction's import_id (see CLAUDE.md invariant 1 - local SQLite
    # state may be lost or reset without ever causing a real duplicate,
    # because YNAB's own import_id matching is what's relied on). Each
    # provider owns a distinct, permanent literal prefix (SpareBank1 uses
    # "SB1:", file-import uses "FILE:") baked into how that provider
    # derives this value. Two rules, both load-bearing:
    #   1. A provider's prefix must be a frozen literal, never computed
    #      from type_name() or anything else that could change - YNAB
    #      permanently reserves every import_id it has ever seen, even
    #      after the transaction using it is deleted (invariant 6), so a
    #      changed prefix would silently and permanently re-import a
    #      provider's entire history as duplicates the next time it polls.
    #   2. Two providers must never share a prefix, or their dedup domains
    #      merge permanently and irreversibly in YNAB - the same failure
    #      mode invariant 7 already calls out between the live-poll
    #      ("SB1:") and file-import ("FILE:") domains.
    # YNAB caps import_id at 36 characters total, prefix included.
    import_id: str

    provider_account_id: str
    date: date
    # Major-unit currency amount (e.g. Decimal("158.48") kr), not milliunits -
    # exact to 3 decimal places (sync/money.py's internal quantum), matching
    # YNAB's own milliunits granularity. Converted to int milliunits only at
    # the YNAB-payload boundary (sync/ynab_payload.py) via
    # sync/money.py::to_milliunits().
    amount: Decimal
    payee_name: str
    memo: str | None
    booking_status: BookingStatus

    # Real account-number cross-reference, used only for transfer-pair
    # matching (see sync/transfers.py) - not a category guess but a direct
    # identity check ("does one leg literally name the other account?"),
    # confirmed more reliable against real data than a provider-specific
    # transaction-type code (a credit card bill payment's two legs don't
    # share any type code that reads as "transfer", but they DO name each
    # other's real account number, at least in one direction - see
    # transform.get_account_number/get_remote_account_number's docstrings
    # for the live-confirmed details). Both default to None so any
    # provider/test that doesn't populate them just never participates in
    # transfer-matching - a safe default, not a regression, since SpareBank1
    # is the only provider that sets these today.
    account_number: str | None = None
    remote_account_number: str | None = None


class ProviderAuthRequiredError(Exception):
    """Raised only when a provider's fetch/list_accounts call fails because
    it genuinely needs the user to re-authenticate (e.g. an OAuth refresh
    token has been revoked or expired past recovery) - the scheduler
    surfaces this as an auth_required flag distinct from an ordinary
    transient failure. Do not raise this for a parse error, an unexpected
    field shape, or any other problem that isn't actually "a human needs to
    go log back in" - see fetch()'s per-row robustness contract below for
    how those should be handled instead.
    """


class TransactionProvider(ABC):
    """The OCP contract a bank/card aggregator integration implements to
    plug into the sync engine, mirrored on this project's existing
    file-import pattern (sync/file_import/base.py's TransformerBase).
    Purely additive as of its introduction - nothing yet adapts
    sparebank1/client.py into an implementation of this, and nothing in
    engine.py/scheduler.py constructs or calls a TransactionProvider.
    """

    @staticmethod
    @abstractmethod
    def type_name() -> str:
        """A short, stable identifier for this provider (e.g.
        "sparebank1"), used as the registry.REGISTRY key. Not the same
        thing as - and must never be used to derive - a
        NormalizedTransaction.import_id prefix (see that field's
        docstring): the prefix is a frozen literal chosen once, while
        type_name() is free to exist purely for registry lookup/display.
        """
        ...

    @abstractmethod
    async def list_accounts(self, force_refresh: bool = False) -> list[ProviderAccount]:
        """List this provider's accounts, for the Mappings tab's drop
        targets. force_refresh, when True, bypasses any caching a provider
        chooses to do internally (e.g. SpareBank1Provider's TTL cache,
        added so a Mappings-tab visit doesn't re-hit a live API every time)
        - wired from the GUI's explicit "Refresh" button. Default False and
        purely additive: a provider that ignores it (never caches at all)
        is still fully conformant.
        """
        ...

    @abstractmethod
    async def fetch(
        self,
        since_by_account: dict[str, datetime],
        on_skip: SkipCallback | None = None,
    ) -> list[NormalizedTransaction]:
        """Fetch transactions for the given accounts, each fetched no
        earlier than its own per-account cutoff.

        since_by_account maps provider_account_id -> the earliest
        transaction timestamp the caller still wants for that account.
        A provider that can only issue one upstream request covering
        multiple accounts (e.g. SpareBank1 batches accounts into a single
        call via a repeated accountKey query param - see
        sparebank1/client.py) should use min(since_by_account.values()) as
        that single request's cutoff and return the union of all accounts'
        transactions tagged with each row's own provider_account_id; the
        engine re-applies the authoritative per-account cutoff itself as a
        backstop, so over-fetching here is safe and expected.

        A provider must NOT drop a row for being older than its account's
        own since_by_account entry - only the caller decides what counts
        as "new." It may only drop a row it structurally cannot parse.

        Per-row robustness is this method's responsibility, not the
        caller's: a single malformed row must be logged (including enough
        of the raw row to diagnose what went wrong) and skipped - never
        allowed to raise and abort the whole batch, since one bad row
        should not cost every other account's legitimate transactions that
        cycle. Likewise, a row whose account is not among the requested
        since_by_account keys must be logged as an error and skipped
        rather than returned or silently dropped without a trace.

        on_skip, when given, should be called (via report_skip() above) once
        for each row dropped for being structurally malformed - it backs the
        GUI's Audit Log "skipped" category. Do NOT call it for a row skipped
        by ordinary, expected per-cycle logic that isn't actually a problem
        (e.g. SpareBank1's provider skips every still-PENDING row by design,
        every cycle - that would flood the audit log for something that
        isn't diagnostic information). Optional and best-effort: a provider
        that never calls it is still fully conformant.

        Raise ProviderAuthRequiredError only for a genuine
        authentication failure requiring the user to re-authenticate -
        never for a parse or field-shape problem, which falls under the
        per-row robustness contract above instead.
        """
        ...
