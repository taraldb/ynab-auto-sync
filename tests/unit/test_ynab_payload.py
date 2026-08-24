from datetime import date

from ynab_auto_sync.sync.ynab_payload import build_create_payload


def test_build_create_payload_uses_payee_name_when_no_payee_id():
    payload = build_create_payload(
        ynab_account_id="acct-1",
        tx_date=date(2026, 8, 24),
        amount_milliunits=-12340,
        payee_name="Some Merchant",
        memo="a memo",
        cleared="cleared",
        import_id="SB1:abcdef0123456789",
    )
    assert payload == {
        "account_id": "acct-1",
        "date": "2026-08-24",
        "amount": -12340,
        "memo": "a memo",
        "cleared": "cleared",
        "approved": False,
        "import_id": "SB1:abcdef0123456789",
        "payee_name": "Some Merchant",
    }


def test_build_create_payload_uses_payee_id_when_given_and_omits_payee_name():
    # payee_id anchors the transaction to a payee YNAB already knows about
    # (e.g. one the user has since renamed/merged) - YNAB treats payee_id
    # and payee_name as mutually exclusive on create, confirmed live via
    # scripts/verify_ynab_payee_id.py, so payee_name must not be sent too.
    payload = build_create_payload(
        ynab_account_id="acct-1",
        tx_date=date(2026, 8, 24),
        amount_milliunits=-12340,
        payee_name="Some Merchant",
        memo="a memo",
        cleared="cleared",
        import_id="SB1:abcdef0123456789",
        payee_id="payee-abc-123",
    )
    assert payload == {
        "account_id": "acct-1",
        "date": "2026-08-24",
        "amount": -12340,
        "memo": "a memo",
        "cleared": "cleared",
        "approved": False,
        "import_id": "SB1:abcdef0123456789",
        "payee_id": "payee-abc-123",
    }
    assert "payee_name" not in payload
