from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from ynab_auto_sync.sync.import_ids import derive_import_id as shared_derive_import_id
from ynab_auto_sync.sync.sanitize import clean_bank_text
from ynab_auto_sync.sync.ynab_payload import (
    YNAB_MEMO_MAX_LEN,
    YNAB_PAYEE_NAME_MAX_LEN,
    build_create_payload,
)

# Re-exported so any existing caller of transform.YNAB_PAYEE_NAME_MAX_LEN /
# transform.YNAB_MEMO_MAX_LEN keeps working - the values themselves now live
# in sync/ynab_payload.py since they are YNAB's own caps, not SpareBank1-
# specific. Prefer importing from sync.ynab_payload directly in new code.
__all__ = [
    "YNAB_MEMO_MAX_LEN",
    "YNAB_PAYEE_NAME_MAX_LEN",
]

# YNAB caps import_id at 36 chars. "SB1:" (4) + 30 hex chars = 34, leaving
# headroom. This prefix is a FROZEN LITERAL and owns SpareBank1's dedup
# domain - see derive_import_id below and providers/base.py's
# NormalizedTransaction.import_id docstring. The hash length itself is
# shared across all sources, so it comes from sync.import_ids.
IMPORT_ID_PREFIX = "SB1:"

# Field names confirmed against a real transaction payload via
# scripts/probe_transactions.py (see plan §10). The first entry in each
# tuple is the confirmed real field; the rest are kept as a defensive
# fallback in case SpareBank1 varies the shape across account/product types
# that weren't covered by the probe.
ID_FIELD_CANDIDATES = ("id", "transactionId", "transactionKey")
DATE_FIELD_CANDIDATES = ("date", "bookingDate", "transactionDate", "interestDate")
AMOUNT_FIELD_CANDIDATES = ("amount",)
DESCRIPTION_FIELD_CANDIDATES = ("description", "text", "remittanceInformation")
# Preference order confirmed against real bank-transfer AND credit-card
# transactions: remoteAccountName (a bank transfer's named counterparty) is
# cleanest when present; cleanedDescription (SpareBank1's own stripped-down
# merchant string, e.g. "Micro Kaffi AS, Stavanger" vs. the raw
# "Zettle_*Micro Kaffi AS, Stavanger") is next-best for card purchases,
# which have no remoteAccountName at all; raw description is the final
# fallback, always present.
PAYEE_NAME_CANDIDATES = ("remoteAccountName", "cleanedDescription", *DESCRIPTION_FIELD_CANDIDATES)
# Memo's own candidate order - deliberately NOT identical to
# PAYEE_NAME_CANDIDATES. remoteAccountName is excluded: it's specifically
# "who the other party is," which is payee's job, and including it here
# would make memo mathematically guaranteed to equal payee whenever a named
# counterparty exists (same field, same first-hit) - collapsing the
# suppress-when-identical check (below) into "always suppressed," which
# defeats the point. cleanedDescription IS included, ahead of raw
# description - confirmed against real data this is what fixes the actual
# redundancy this whole change targets: a card purchase's raw `description`
# carries a payment-processor prefix ("Zettle_*", "Vipps*", ...) that
# cleanedDescription already strips for payee; preferring it for memo too
# makes the two agree exactly in that case, which is correctly suppressed,
# while still leaving room for memo to diverge when payee comes from
# remoteAccountName and a genuinely separate note (e.g. remittanceInformation)
# exists - see provider.py's _extract_memo docstring for the full write-up.
MEMO_FIELD_CANDIDATES = ("cleanedDescription", *DESCRIPTION_FIELD_CANDIDATES)

BOOKING_STATUS_PENDING = "PENDING"
BOOKING_STATUS_BOOKED = "BOOKED"


class MissingFieldError(Exception):
    """Raised when none of the candidate field names are present on a
    SpareBank1 transaction - a strong signal the guessed names in this
    module need updating from a live probe (scripts/probe_transactions.py)."""


def _get_first(data: dict[str, Any], *candidates: str) -> Any:
    for key in candidates:
        if key in data and data[key] is not None:
            return data[key]
    return None


def get_transaction_id(sb1_tx: dict[str, Any]) -> str:
    value = _get_first(sb1_tx, *ID_FIELD_CANDIDATES)
    if value is None:
        raise MissingFieldError(
            f"No stable id field found among {ID_FIELD_CANDIDATES} in transaction: "
            f"{sb1_tx!r}"
        )
    return str(value)


def get_tracking_key(sb1_tx: dict[str, Any], account_key: str) -> str:
    """The identifier used to detect 'this is the same real-world
    transaction across polls' - deliberately NOT SpareBank1's own `id`
    field, which was confirmed live to change on every poll of a
    still-pending credit-card transaction. Prefers
    creditCardIdentifiers.nonUniqueId when present (booked credit-card
    transactions - a structured, more specific field), else the raw
    nonUniqueId field (pending credit-card transactions, and all regular
    bank-transfer transactions, which never have creditCardIdentifiers).
    account_key scopes the result, since SpareBank1's own field naming
    ("nonUniqueId") warns it is not guaranteed globally unique.
    """
    credit_card_ids = sb1_tx.get("creditCardIdentifiers")
    if isinstance(credit_card_ids, dict) and credit_card_ids.get("nonUniqueId"):
        raw_id = str(credit_card_ids["nonUniqueId"])
    else:
        non_unique_id = sb1_tx.get("nonUniqueId")
        if non_unique_id is None:
            raise MissingFieldError(
                f"No nonUniqueId (or creditCardIdentifiers.nonUniqueId) found in "
                f"transaction, cannot derive a tracking key: {sb1_tx!r}"
            )
        raw_id = str(non_unique_id)
    return f"{account_key}:{raw_id}"


def get_transaction_date(sb1_tx: dict[str, Any]) -> date:
    value = _get_first(sb1_tx, *DATE_FIELD_CANDIDATES)
    if value is None:
        raise MissingFieldError(
            f"No date field found among {DATE_FIELD_CANDIDATES} in transaction: {sb1_tx!r}"
        )
    if isinstance(value, (int, float)):
        # Confirmed via a live probe: SpareBank1 returns Unix epoch
        # milliseconds here, not an ISO string.
        return datetime.fromtimestamp(value / 1000, tz=UTC).date()
    # Accept either a full ISO datetime or a plain date string, in case a
    # different account/product type returns one.
    return date.fromisoformat(str(value)[:10])


def get_booking_status(sb1_tx: dict[str, Any]) -> str:
    # A missing bookingStatus (e.g. from an endpoint variant that doesn't
    # report it) is treated as already booked, matching this project's
    # existing defensive default elsewhere.
    return sb1_tx.get("bookingStatus") or BOOKING_STATUS_BOOKED


# Sentinel SpareBank1 uses for "no remote account number applies" - confirmed
# live on every card-purchase transaction inspected (a merchant purchase has
# no bank account on the other side). Treated as absent, never as a literal
# value to compare - see get_remote_account_number below.
REMOTE_ACCOUNT_NUMBER_NONE = "-1"


def get_account_number(sb1_tx: dict[str, Any]) -> str | None:
    """The owning account's own real account number, e.g. "32096894308" -
    confirmed live to be present, in the same nested {"value": ...,
    "formatted": ..., "unformatted": ...} shape, on both regular bank
    accounts and credit card accounts (the credit card's own accountNumber
    is its masked card number, e.g. "K1861615456" - still present, just a
    different format). Used only for transfer-pair cross-referencing (see
    sync/transfers.py) - never MissingFieldError, since a transaction with
    no recognizable account number just can't participate in that matching
    and should import as an ordinary transaction, not be skipped entirely.
    """
    account_number = sb1_tx.get("accountNumber")
    if not isinstance(account_number, dict):
        return None
    value = account_number.get("value")
    return str(value) if value is not None else None


def get_remote_account_number(sb1_tx: dict[str, Any]) -> str | None:
    """The counterparty account number a transaction names, if any -
    confirmed live: populated with the real account number for a genuine
    bank-to-bank transfer between the user's own accounts, "-1" for a card
    purchase (no bank account applies), and - for a credit card BILL
    PAYMENT specifically - the card issuer's own internal clearing account
    number rather than the card's own account number (confirmed live: a
    real credit card payment's checking-account leg named
    "Sparebank 1 Kreditt AS"'s clearing account, not the card itself, while
    the matching leg on the card's own transaction feed correctly named the
    checking account back). This asymmetry is exactly why
    sync/transfers.py's cross-reference check only requires ONE direction
    to match, not both.
    """
    value = sb1_tx.get("remoteAccountNumber")
    if value is None or str(value) == REMOTE_ACCOUNT_NUMBER_NONE:
        return None
    return str(value)


def derive_import_id(sb1_transaction_id: str) -> str:
    """SpareBank1's import_id namespace. Delegates the hashing to
    sync.import_ids so every source shares one implementation, but the
    "SB1:" prefix stays a frozen literal owned here - never computed from
    a provider's type_name() or anything else that could change (see
    providers/base.py's NormalizedTransaction.import_id docstring).
    """
    return shared_derive_import_id(IMPORT_ID_PREFIX, sb1_transaction_id)


def _to_milliunits(amount: Any) -> int:
    # SpareBank1 amounts are assumed to be plain NOK (e.g. -125.50), not
    # minor units - verify against scripts/probe_transactions.py output
    # before trusting the sign/scale on real money.
    return round(float(amount) * 1000)


def _extract_payee_name(sb1_tx: dict[str, Any]) -> str:
    # The "SpareBank1 transaction" fallback is bank-specific (this is the
    # only source that ever needs it) and stays here rather than in
    # ynab_payload.py, which is deliberately source-agnostic. Truncation to
    # YNAB's own length cap happens once, in build_create_payload.
    name = _get_first(sb1_tx, *PAYEE_NAME_CANDIDATES)
    cleaned = clean_bank_text(str(name)) if name is not None else None
    return cleaned or "SpareBank1 transaction"


def get_amount_milliunits(sb1_tx: dict[str, Any]) -> int:
    amount = _get_first(sb1_tx, *AMOUNT_FIELD_CANDIDATES)
    if amount is None:
        raise MissingFieldError(
            f"No amount field found among {AMOUNT_FIELD_CANDIDATES} in transaction: {sb1_tx!r}"
        )
    return _to_milliunits(amount)


def transform_transaction(
    sb1_tx: dict[str, Any], ynab_account_id: str, account_key: str
) -> dict[str, Any]:
    """Map one SpareBank1 transaction dict to a YNAB transaction payload,
    including the deterministic import_id that is the primary no-duplicates
    safety net (see sync/engine.py).
    """
    sb1_id = get_tracking_key(sb1_tx, account_key)
    tx_date = get_transaction_date(sb1_tx)
    amount_milliunits = get_amount_milliunits(sb1_tx)
    payee_name = _extract_payee_name(sb1_tx)
    # Confirmed live against 25 real booked transactions: memo never once
    # carried information payee didn't already say - see provider.py's
    # _extract_memo docstring (the real, live path this module's fetch()
    # equivalent must stay behaviourally identical to) for the full finding,
    # and MEMO_FIELD_CANDIDATES' own comment for why it's deliberately NOT
    # identical to PAYEE_NAME_CANDIDATES (excludes remoteAccountName, or
    # memo would be mathematically guaranteed to always equal payee).
    # Suppress memo to None whenever it's provably identical to payee
    # (case-insensitive, whitespace-normalized) rather than showing the same
    # text twice.
    description = _get_first(sb1_tx, *MEMO_FIELD_CANDIDATES)
    memo = clean_bank_text(str(description)) if description is not None else None
    if memo is not None and memo.strip().casefold() == payee_name.strip().casefold():
        memo = None

    return build_create_payload(
        ynab_account_id=ynab_account_id,
        tx_date=tx_date,
        amount_milliunits=amount_milliunits,
        payee_name=payee_name,
        memo=memo,
        cleared=(
            "uncleared" if get_booking_status(sb1_tx) == BOOKING_STATUS_PENDING else "cleared"
        ),
        import_id=derive_import_id(sb1_id),
    )
