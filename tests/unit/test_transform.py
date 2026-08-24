import pytest

from ynab_auto_sync.providers.sparebank1.transform import (
    MissingFieldError,
    derive_import_id,
    get_booking_status,
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


def test_get_transaction_date_accepts_date_or_datetime_strings():
    assert get_transaction_date({"date": "2026-08-20"}).isoformat() == "2026-08-20"
    assert get_transaction_date({"bookingDate": "2026-08-21T00:00:00Z"}).isoformat() == "2026-08-21"


def test_get_transaction_date_accepts_epoch_milliseconds():
    # Confirmed via a live probe against a real SpareBank1 transaction:
    # "date" is Unix epoch milliseconds, not an ISO string.
    assert get_transaction_date({"date": 1787176800000}).isoformat() == "2026-08-19"


def test_transform_transaction_maps_expected_fields():
    sb1_tx = {
        "id": "sb1-tx-42",
        "nonUniqueId": "42",
        "date": "2026-08-20",
        "amount": -125.5,
        "description": "Kiwi Grünerløkka",
    }
    result = transform_transaction(sb1_tx, ynab_account_id="ynab-acct-1", account_key="acct-1")

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
    result = transform_transaction(sb1_tx, ynab_account_id="ynab-acct-1", account_key="acct-1")
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
    result = transform_transaction(sb1_tx, ynab_account_id="ynab-acct-1", account_key="acct-1")
    assert result["payee_name"] == "Micro Kaffi AS, Stavanger"
    # memo keeps the full raw description (sanitized: '*' separator -> space)
    # regardless of the cleaner payee name
    assert result["memo"] == "Zettle_ Micro Kaffi AS, Stavanger"


def test_transform_transaction_falls_back_to_description_without_remote_account_name():
    sb1_tx = {
        "id": "sb1-tx-44",
        "nonUniqueId": "44",
        "date": "2026-08-20",
        "amount": 10,
        "description": "PULS NORGE",
    }
    result = transform_transaction(sb1_tx, ynab_account_id="ynab-acct-1", account_key="acct-1")
    assert result["payee_name"] == "PULS NORGE"


def test_transform_transaction_raises_on_missing_amount():
    with pytest.raises(MissingFieldError):
        transform_transaction(
            {"id": "x", "nonUniqueId": "x", "date": "2026-08-20"},
            ynab_account_id="a",
            account_key="acct-1",
        )


def test_get_booking_status_defaults_to_booked_when_absent():
    assert get_booking_status({}) == "BOOKED"
    assert get_booking_status({"bookingStatus": "PENDING"}) == "PENDING"
    assert get_booking_status({"bookingStatus": "BOOKED"}) == "BOOKED"


def test_transform_transaction_is_uncleared_when_pending():
    sb1_tx = {
        "id": "sb1-tx-45",
        "nonUniqueId": "45",
        "date": "2026-08-20",
        "amount": -130,
        "description": "Zettle_*Micro Kaffi AS",
        "bookingStatus": "PENDING",
    }
    result = transform_transaction(sb1_tx, ynab_account_id="ynab-acct-1", account_key="acct-1")
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
        transform_transaction(booked_tx, ynab_account_id="a", account_key="acct-1")["cleared"]
        == "cleared"
    )
    assert (
        transform_transaction(no_status_tx, ynab_account_id="a", account_key="acct-1")["cleared"]
        == "cleared"
    )
