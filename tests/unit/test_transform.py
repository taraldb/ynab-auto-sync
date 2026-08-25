import pytest

from ynab_auto_sync.providers.sparebank1.transform import (
    MissingFieldError,
    derive_import_id,
    get_account_number,
    get_booking_status,
    get_remote_account_number,
    get_tracking_key,
    get_transaction_date,
    get_transaction_id,
    transform_transaction,
)


def test_derive_import_id_is_deterministic_and_within_length_limit():
    id_a = derive_import_id("abc123")
    id_b = derive_import_id("abc123")
    id_c = derive_import_id("different")

    assert id_a == id_b
    assert id_a != id_c
    assert id_a.startswith("SB1:")
    assert len(id_a) <= 36


def test_get_transaction_id_tries_candidate_fields():
    assert get_transaction_id({"transactionId": "tx-1"}) == "tx-1"
    assert get_transaction_id({"id": "tx-2"}) == "tx-2"


def test_get_transaction_id_raises_when_no_candidate_present():
    with pytest.raises(MissingFieldError):
        get_transaction_id({"unrelated": "field"})


def test_get_tracking_key_uses_bare_non_unique_id_for_pending_credit_card():
    sb1_tx = {"nonUniqueId": "575043327"}
    assert get_tracking_key(sb1_tx, "4526895") == "4526895:575043327"


def test_get_tracking_key_prefers_credit_card_identifiers_when_present():
    sb1_tx = {
        "nonUniqueId": "2926243-707498431",
        "creditCardIdentifiers": {"partitionKey": "2926243", "nonUniqueId": "707498431"},
    }
    assert get_tracking_key(sb1_tx, "4526895") == "4526895:707498431"


def test_get_tracking_key_uses_bare_non_unique_id_for_regular_bank_transfer():
    sb1_tx = {"nonUniqueId": "12345"}
    assert get_tracking_key(sb1_tx, "acct-1") == "acct-1:12345"


def test_get_tracking_key_raises_when_no_id_present():
    with pytest.raises(MissingFieldError):
        get_tracking_key({"unrelated": "field"}, "acct-1")


def test_get_tracking_key_raises_when_credit_card_identifiers_missing_non_unique_id():
    sb1_tx = {"creditCardIdentifiers": {"partitionKey": "2926243"}}
    with pytest.raises(MissingFieldError):
        get_tracking_key(sb1_tx, "acct-1")


def test_get_tracking_key_correlates_pending_and_booked_same_reservation():
    # The key regression test: a pending credit-card transaction's bare
    # nonUniqueId (the reservation number) must match the tracking key
    # derived once the same transaction books and gains
    # creditCardIdentifiers - proving the pending->booked reconciliation
    # correlation design works when the hypothesis holds.
    pending_tx = {"nonUniqueId": "575043327"}
    booked_tx = {
        "nonUniqueId": "2926243-575043327",
        "creditCardIdentifiers": {"partitionKey": "2926243", "nonUniqueId": "575043327"},
    }
    assert get_tracking_key(pending_tx, "4526895") == get_tracking_key(booked_tx, "4526895")


def test_get_tracking_key_normalizes_prefixed_credit_card_non_unique_id_to_bare():
    # Confirmed live in production: creditCardIdentifiers.nonUniqueId can
    # arrive already prefixed with "{partitionKey}-" (observed once a
    # transaction aged from source "RECENT" into "HISTORIC"). Must still
    # collapse to the same bare-form key the un-prefixed observation
    # produces, or the same real transaction gets two different tracking
    # keys/import_ids across two polls.
    sb1_tx = {
        "creditCardIdentifiers": {"partitionKey": "2926243", "nonUniqueId": "2926243-708046574"},
    }
    assert get_tracking_key(sb1_tx, "4526895") == "4526895:708046574"


def test_get_tracking_key_same_transaction_stable_across_recent_and_historic_forms():
    # The real RECENT vs HISTORIC pair from the production bug report -
    # both must collapse to the identical literal tracking key.
    recent_tx = {
        "creditCardIdentifiers": {"partitionKey": "2926243", "nonUniqueId": "708046574"},
    }
    historic_tx = {
        "creditCardIdentifiers": {"partitionKey": "2926243", "nonUniqueId": "2926243-708046574"},
    }
    assert get_tracking_key(recent_tx, "4526895") == "4526895:708046574"
    assert get_tracking_key(historic_tx, "4526895") == "4526895:708046574"


def test_get_tracking_key_leaves_non_unique_id_unchanged_when_not_partition_prefixed():
    # Guard against over-aggressive stripping: only an exact
    # "{partitionKey}-" prefix match is stripped.
    sb1_tx = {
        "creditCardIdentifiers": {"partitionKey": "9999999", "nonUniqueId": "2926243-708046574"},
    }
    assert get_tracking_key(sb1_tx, "4526895") == "4526895:2926243-708046574"


def test_get_transaction_date_accepts_date_or_datetime_strings():
    assert get_transaction_date({"date": "2026-08-20"}, "Europe/Oslo").isoformat() == "2026-08-20"
    assert (
        get_transaction_date({"bookingDate": "2026-08-21T00:00:00Z"}, "Europe/Oslo").isoformat()
        == "2026-08-21"
    )


def test_get_transaction_date_accepts_epoch_milliseconds():
    # Confirmed via a live probe against a real SpareBank1 transaction:
    # "date" is Unix epoch milliseconds, not an ISO string - and it encodes
    # LOCAL (Europe/Oslo) midnight for the transaction's real calendar day,
    # not UTC midnight (see CLAUDE.md's "Resolved: SpareBank1 transaction
    # dates were a day early"). 1787176800000 is Oslo-local midnight
    # 2026-08-20 - truncating in UTC instead (the pre-fix bug) would give
    # 2026-08-19, one day early.
    assert get_transaction_date({"date": 1787176800000}, "Europe/Oslo").isoformat() == "2026-08-20"


def test_get_transaction_date_real_kanelsnurren_transaction():
    # Regression test pinning a real, live-confirmed transaction (see
    # responses-dev/250820261943.json, nonUniqueId "2926243-708150808"):
    # its CSV export confirms Kjøpsdato = 2026-08-24, and the pre-fix code
    # (UTC truncation) returned 2026-08-23 - one day early.
    assert get_transaction_date({"date": 1787522400000}, "Europe/Oslo").isoformat() == "2026-08-24"


def test_transform_transaction_maps_expected_fields():
    sb1_tx = {
        "id": "sb1-tx-42",
        "nonUniqueId": "42",
        "date": "2026-08-20",
        "amount": -125.5,
        "description": "Kiwi Grünerløkka",
    }
    result = transform_transaction(
        sb1_tx, ynab_account_id="ynab-acct-1", account_key="acct-1", timezone="Europe/Oslo"
    )

    assert result["account_id"] == "ynab-acct-1"
    assert result["date"] == "2026-08-20"
    assert result["amount"] == -125500
    assert result["payee_name"] == "Kiwi Grünerløkka"
    assert result["cleared"] == "cleared"
    assert result["approved"] is False
    assert result["import_id"] == derive_import_id(get_tracking_key(sb1_tx, "acct-1"))


def test_transform_transaction_prefers_remote_account_name_over_description():
    sb1_tx = {
        "id": "sb1-tx-43",
        "nonUniqueId": "43",
        "date": "2026-08-20",
        "amount": 10,
        "description": "raw description",
        "remoteAccountName": "Rema 1000",
    }
    result = transform_transaction(
        sb1_tx, ynab_account_id="ynab-acct-1", account_key="acct-1", timezone="Europe/Oslo"
    )
    assert result["payee_name"] == "Rema 1000"


def test_transform_transaction_prefers_cleaned_description_over_raw_description():
    # Confirmed via a live probe against a real credit-card transaction:
    # no remoteAccountName on card purchases, but cleanedDescription strips
    # the payment-processor prefix that the raw description carries.
    sb1_tx = {
        "id": "sb1-tx-cc-1",
        "nonUniqueId": "cc-1",
        "date": "2026-08-20",
        "amount": -130,
        "description": "Zettle_*Micro Kaffi AS, Stavanger",
        "cleanedDescription": "Micro Kaffi AS, Stavanger",
    }
    result = transform_transaction(
        sb1_tx, ynab_account_id="ynab-acct-1", account_key="acct-1", timezone="Europe/Oslo"
    )
    assert result["payee_name"] == "Micro Kaffi AS, Stavanger"
    # Confirmed against 25 real transactions: once memo is derived from the
    # same cleanedDescription-preferring candidates payee uses, a
    # processor-prefixed raw description collapses to the exact same text
    # as payee - real information, not noise, so it's suppressed (None)
    # rather than shown twice.
    assert result["memo"] is None


def test_transform_transaction_falls_back_to_description_without_remote_account_name():
    sb1_tx = {
        "id": "sb1-tx-44",
        "nonUniqueId": "44",
        "date": "2026-08-20",
        "amount": 10,
        "description": "PULS NORGE",
    }
    result = transform_transaction(
        sb1_tx, ynab_account_id="ynab-acct-1", account_key="acct-1", timezone="Europe/Oslo"
    )
    assert result["payee_name"] == "PULS NORGE"
    # Same single raw field backs both payee and memo here, so once memo
    # uses the same candidate preference as payee, they agree exactly and
    # memo must be suppressed rather than duplicating payee.
    assert result["memo"] is None


def test_transform_transaction_preserves_memo_genuinely_different_from_payee():
    sb1_tx = {
        "id": "sb1-tx-45",
        "nonUniqueId": "45",
        "date": "2026-08-20",
        "amount": -100,
        "remoteAccountName": "Kari Nordmann",
        "remittanceInformation": "KID 12345678901",
    }
    result = transform_transaction(
        sb1_tx, ynab_account_id="ynab-acct-1", account_key="acct-1", timezone="Europe/Oslo"
    )
    assert result["payee_name"] == "Kari Nordmann"
    assert result["memo"] == "KID 12345678901"


def test_transform_transaction_raises_on_missing_amount():
    with pytest.raises(MissingFieldError):
        transform_transaction(
            {"id": "x", "nonUniqueId": "x", "date": "2026-08-20"},
            ynab_account_id="a",
            account_key="acct-1",
            timezone="Europe/Oslo",
        )


def test_get_booking_status_defaults_to_booked_when_absent():
    assert get_booking_status({}) == "BOOKED"
    assert get_booking_status({"bookingStatus": "PENDING"}) == "PENDING"
    assert get_booking_status({"bookingStatus": "BOOKED"}) == "BOOKED"


def test_get_account_number_reads_nested_value():
    sb1_tx = {"accountNumber": {"value": "32096894308", "formatted": "3209 68 94308"}}
    assert get_account_number(sb1_tx) == "32096894308"


def test_get_account_number_returns_none_when_absent_or_wrong_shape():
    assert get_account_number({}) is None
    assert get_account_number({"accountNumber": "not-a-dict"}) is None
    assert get_account_number({"accountNumber": {}}) is None


def test_get_remote_account_number_passthrough():
    assert get_remote_account_number({"remoteAccountNumber": "32096220447"}) == "32096220447"


def test_get_remote_account_number_treats_sentinel_and_missing_as_none():
    # "-1" is SpareBank1's own sentinel for "no remote account applies" -
    # confirmed live on every card-purchase transaction seen - must never
    # be compared as a literal value.
    assert get_remote_account_number({"remoteAccountNumber": "-1"}) is None
    assert get_remote_account_number({}) is None


def test_transform_transaction_is_uncleared_when_pending():
    sb1_tx = {
        "id": "sb1-tx-45",
        "nonUniqueId": "45",
        "date": "2026-08-20",
        "amount": -130,
        "description": "Zettle_*Micro Kaffi AS",
        "bookingStatus": "PENDING",
    }
    result = transform_transaction(
        sb1_tx, ynab_account_id="ynab-acct-1", account_key="acct-1", timezone="Europe/Oslo"
    )
    assert result["cleared"] == "uncleared"


def test_transform_transaction_is_cleared_when_booked_or_missing_status():
    booked_tx = {
        "id": "sb1-tx-46",
        "nonUniqueId": "46",
        "date": "2026-08-20",
        "amount": -10,
        "bookingStatus": "BOOKED",
    }
    no_status_tx = {"id": "sb1-tx-47", "nonUniqueId": "47", "date": "2026-08-20", "amount": -10}
    assert (
        transform_transaction(
            booked_tx, ynab_account_id="a", account_key="acct-1", timezone="Europe/Oslo"
        )["cleared"]
        == "cleared"
    )
    assert (
        transform_transaction(
            no_status_tx, ynab_account_id="a", account_key="acct-1", timezone="Europe/Oslo"
        )["cleared"]
        == "cleared"
    )
