import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ynab_auto_sync.config import SpareBank1Config
from ynab_auto_sync.providers.base import BookingStatus, ProviderAccount
from ynab_auto_sync.providers.sparebank1 import client as sb1_client
from ynab_auto_sync.providers.sparebank1.auth import TokenStore
from ynab_auto_sync.providers.sparebank1.provider import SpareBank1Provider
from ynab_auto_sync.state import JsonStateStore


def make_token_store(tmp_path: Path) -> TokenStore:
    store = JsonStateStore(tmp_path / "tokens.json")
    future = datetime.now(UTC) + timedelta(minutes=5)
    store.write(
        {
            "access_token": "valid-access-token",
            "refresh_token": "valid-refresh-token",
            "obtained_at": datetime.now(UTC).isoformat(),
            "expires_at": future.isoformat(),
        }
    )
    config = SpareBank1Config(
        client_id="cid", client_secret="secret", redirect_uri="http://localhost:8765/callback"
    )
    return TokenStore(store, config)


def make_provider(tmp_path: Path, http_client: httpx.AsyncClient) -> SpareBank1Provider:
    return SpareBank1Provider(http_client, make_token_store(tmp_path))


def mock_transactions(txs: list[dict]) -> None:
    respx.get(sb1_client.TRANSACTIONS_URL).mock(
        return_value=httpx.Response(200, json={"transactions": txs})
    )


SINCE = {"acct-1": datetime(2020, 1, 1, tzinfo=UTC)}


@respx.mock
async def test_key_identity_regression_hardcoded_literals(tmp_path: Path):
    # THE MOST IMPORTANT TEST IN THIS FILE.
    #
    # These expected strings were computed ONCE, offline, from the exact
    # same formula the provider is supposed to use
    # (f"{account_key}:{raw_id}", then "SB1:" + sha256(that)[:30]) and then
    # pasted in as literals below. Deliberately NOT asserting
    # `ntx.import_id == derive_import_id(ntx.tracking_key)` - that would
    # pass even if the provider's own derivation and this test's
    # expectation both drifted from the real, currently-live formula at the
    # same time (e.g. a prefix or hash-length change made in provider.py
    # and "kept in sync" here without ever being checked against reality).
    # A hardcoded literal only passes if the actual byte-for-byte value
    # SpareBank1Provider produces still matches what was true when this
    # test was written - which is exactly what CLAUDE.md invariant
    # 2/6 requires (tracked_transactions is keyed on this string forever,
    # and YNAB permanently burns every import_id it has ever seen).
    plain_transfer = {
        "id": "tx-id-changes-every-poll-1",
        "accountKey": "acct-1",
        "nonUniqueId": "nu-plain-1",
        "date": "2026-08-20",
        "amount": -50,
        "description": "Coffee",
        "bookingStatus": "BOOKED",
    }
    pending_cc = {
        "id": "tx-id-changes-every-poll-2",
        "accountKey": "acct-cc",
        "nonUniqueId": "nu-pending-99",
        "date": "2026-08-21",
        "amount": -75,
        "description": "Pending card charge",
        "bookingStatus": "PENDING",
    }
    booked_cc = {
        "id": "tx-id-changes-every-poll-3",
        "accountKey": "acct-cc",
        "nonUniqueId": "acct-cc-nonUniqueId-partitionKey-suffix",
        "creditCardIdentifiers": {"nonUniqueId": "cc-booked-42"},
        "date": "2026-08-22",
        "amount": -100,
        "description": "Booked card charge",
        "bookingStatus": "BOOKED",
    }
    mock_transactions([plain_transfer, pending_cc, booked_cc])

    since_by_account = {
        "acct-1": datetime(2020, 1, 1, tzinfo=UTC),
        "acct-cc": datetime(2020, 1, 1, tzinfo=UTC),
    }
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        results = await provider.fetch(since_by_account)

    # Only 2, not 3: PENDING rows are skipped entirely (see
    # test_pending_transactions_are_skipped below) - their amount is not
    # trustworthy yet, so pending_cc must never reach import_id derivation.
    assert len(results) == 2
    assert not any(n.tracking_key == "acct-cc:nu-pending-99" for n in results)

    plain_ntx = next(n for n in results if n.tracking_key.startswith("acct-1:"))
    assert plain_ntx.tracking_key == "acct-1:nu-plain-1"
    assert plain_ntx.import_id == "SB1:7e4236d7360c0bc80311d39968652a"

    booked_ntx = next(n for n in results if n.tracking_key == "acct-cc:cc-booked-42")
    assert booked_ntx.import_id == "SB1:33a6835e3969aabf267c9e97e08684"
    assert booked_ntx.booking_status == BookingStatus.BOOKED


@respx.mock
async def test_pending_transactions_are_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    # SpareBank1's amount is not final while PENDING (confirmed against real
    # data) - fetch() must never hand a PENDING row to the engine at all,
    # only once it books with its real, final amount.
    mock_transactions(
        [
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-pending",
                "date": "2026-08-20",
                "amount": -999,
                "description": "still pending",
                "bookingStatus": "PENDING",
            },
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-booked",
                "date": "2026-08-20",
                "amount": -10,
                "description": "already booked",
                "bookingStatus": "BOOKED",
            },
        ]
    )
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        with caplog.at_level(logging.DEBUG):
            results = await provider.fetch(SINCE)

    assert len(results) == 1
    assert results[0].tracking_key == "acct-1:nu-booked"
    assert any("Skipping PENDING" in record.message for record in caplog.records)


@respx.mock
async def test_same_raw_nonuniqueid_scoped_by_account(tmp_path: Path):
    mock_transactions(
        [
            {
                "accountKey": "acct-a",
                "nonUniqueId": "shared-raw-id",
                "date": "2026-08-20",
                "amount": -10,
                "description": "x",
                "bookingStatus": "BOOKED",
            },
            {
                "accountKey": "acct-b",
                "nonUniqueId": "shared-raw-id",
                "date": "2026-08-20",
                "amount": -10,
                "description": "x",
                "bookingStatus": "BOOKED",
            },
        ]
    )
    since_by_account = {
        "acct-a": datetime(2020, 1, 1, tzinfo=UTC),
        "acct-b": datetime(2020, 1, 1, tzinfo=UTC),
    }
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        results = await provider.fetch(since_by_account)

    assert len(results) == 2
    tracking_keys = {ntx.tracking_key for ntx in results}
    import_ids = {ntx.import_id for ntx in results}
    assert tracking_keys == {"acct-a:shared-raw-id", "acct-b:shared-raw-id"}
    assert len(import_ids) == 2
    assert import_ids == {
        "SB1:f8c13acc0d281fc7e029d80930f3de",
        "SB1:31acc93e8af5af307ed71e2d7c2348",
    }


@respx.mock
async def test_unrecognized_account_key_is_skipped_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    mock_transactions(
        [
            {
                "accountKey": "not-a-configured-account",
                "nonUniqueId": "nu-1",
                "date": "2026-08-20",
                "amount": -10,
                "description": "x",
                "bookingStatus": "BOOKED",
            },
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-2",
                "date": "2026-08-20",
                "amount": -10,
                "description": "y",
                "bookingStatus": "BOOKED",
            },
        ]
    )
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        with caplog.at_level(logging.ERROR):
            results = await provider.fetch(SINCE)

    assert len(results) == 1
    assert results[0].tracking_key == "acct-1:nu-2"
    assert any(
        "unrecognized or missing accountKey" in record.message for record in caplog.records
    )
    assert any(record.levelno == logging.ERROR for record in caplog.records)


@respx.mock
async def test_missing_account_key_field_is_skipped_and_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    mock_transactions(
        [
            {
                "nonUniqueId": "nu-no-account",
                "date": "2026-08-20",
                "amount": -10,
                "description": "x",
                "bookingStatus": "BOOKED",
            }
        ]
    )
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        with caplog.at_level(logging.ERROR):
            results = await provider.fetch(SINCE)

    assert results == []
    assert any(
        "unrecognized or missing accountKey" in record.message for record in caplog.records
    )


@respx.mock
async def test_row_missing_date_field_is_skipped_others_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    mock_transactions(
        [
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-no-date",
                "amount": -10,
                "description": "no date here",
                "bookingStatus": "BOOKED",
            },
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-good",
                "date": "2026-08-20",
                "amount": -10,
                "description": "fine",
                "bookingStatus": "BOOKED",
            },
        ]
    )
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        with caplog.at_level(logging.ERROR):
            results = await provider.fetch(SINCE)

    assert len(results) == 1
    assert results[0].tracking_key == "acct-1:nu-good"
    assert any("no recognizable date field" in record.message for record in caplog.records)


@respx.mock
async def test_row_missing_id_field_is_skipped_others_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    mock_transactions(
        [
            {
                "accountKey": "acct-1",
                # no nonUniqueId and no creditCardIdentifiers.nonUniqueId
                "date": "2026-08-20",
                "amount": -10,
                "description": "no id here",
                "bookingStatus": "BOOKED",
            },
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-good-2",
                "date": "2026-08-20",
                "amount": -10,
                "description": "fine",
                "bookingStatus": "BOOKED",
            },
        ]
    )
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        with caplog.at_level(logging.ERROR):
            results = await provider.fetch(SINCE)

    assert len(results) == 1
    assert results[0].tracking_key == "acct-1:nu-good-2"
    assert any("no recognizable id field" in record.message for record in caplog.records)


@respx.mock
async def test_row_missing_amount_field_is_skipped_others_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    mock_transactions(
        [
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-no-amount",
                "date": "2026-08-20",
                "description": "no amount here",
                "bookingStatus": "BOOKED",
            },
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-good-3",
                "date": "2026-08-20",
                "amount": -10,
                "description": "fine",
                "bookingStatus": "BOOKED",
            },
        ]
    )
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        with caplog.at_level(logging.ERROR):
            results = await provider.fetch(SINCE)

    assert len(results) == 1
    assert results[0].tracking_key == "acct-1:nu-good-3"
    assert any("missing required fields" in record.message for record in caplog.records)


@respx.mock
async def test_epoch_millisecond_and_iso_dates_both_parse(tmp_path: Path):
    epoch_ms = int(datetime(2026, 8, 20, tzinfo=UTC).timestamp() * 1000)
    mock_transactions(
        [
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-epoch",
                "date": epoch_ms,
                "amount": -10,
                "description": "epoch date",
                "bookingStatus": "BOOKED",
            },
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-iso",
                "date": "2026-08-21",
                "amount": -10,
                "description": "iso date",
                "bookingStatus": "BOOKED",
            },
        ]
    )
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        results = await provider.fetch(SINCE)

    assert len(results) == 2
    by_tracking_key = {ntx.tracking_key: ntx for ntx in results}
    assert by_tracking_key["acct-1:nu-epoch"].date.isoformat() == "2026-08-20"
    assert by_tracking_key["acct-1:nu-iso"].date.isoformat() == "2026-08-21"


@respx.mock
async def test_fetch_sets_account_number_and_remote_account_number(tmp_path: Path):
    mock_transactions(
        [
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-transfer",
                "date": "2026-08-20",
                "amount": -100,
                "description": "transfer out",
                "bookingStatus": "BOOKED",
                "accountNumber": {"value": "32096894308", "formatted": "3209 68 94308"},
                "remoteAccountNumber": "32096220447",
            },
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-purchase",
                "date": "2026-08-20",
                "amount": -50,
                "description": "card purchase",
                "bookingStatus": "BOOKED",
                "accountNumber": {"value": "K1861615456", "formatted": "K186 16 15456"},
                # "-1" is SpareBank1's own sentinel for "not applicable".
                "remoteAccountNumber": "-1",
            },
        ]
    )
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        results = await provider.fetch(SINCE)

    by_tracking_key = {ntx.tracking_key: ntx for ntx in results}
    transfer_ntx = by_tracking_key["acct-1:nu-transfer"]
    assert transfer_ntx.account_number == "32096894308"
    assert transfer_ntx.remote_account_number == "32096220447"

    purchase_ntx = by_tracking_key["acct-1:nu-purchase"]
    assert purchase_ntx.account_number == "K1861615456"
    assert purchase_ntx.remote_account_number is None


@respx.mock
async def test_fetch_does_not_drop_rows_older_than_since(tmp_path: Path):
    # fetch() over-fetches on purpose (providers/base.py's fetch() docstring)
    # - the engine, not the provider, is responsible for dropping rows older
    # than an account's own `since`. A row far older than `since` must still
    # come back from fetch() unfiltered.
    mock_transactions(
        [
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-ancient",
                "date": "2015-01-01",
                "amount": -10,
                "description": "very old",
                "bookingStatus": "BOOKED",
            }
        ]
    )
    since_by_account = {"acct-1": datetime(2026, 1, 1, tzinfo=UTC)}
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        results = await provider.fetch(since_by_account)

    assert len(results) == 1
    assert results[0].tracking_key == "acct-1:nu-ancient"
    assert results[0].date.isoformat() == "2015-01-01"


@respx.mock
async def test_on_skip_called_for_each_malformed_row(tmp_path: Path):
    mock_transactions(
        [
            {
                "accountKey": "not-a-configured-account",
                "nonUniqueId": "nu-1",
                "date": "2026-08-20",
                "amount": -10,
                "bookingStatus": "BOOKED",
            },
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-no-date",
                "amount": -10,
                "bookingStatus": "BOOKED",
            },
            {
                "accountKey": "acct-1",
                "date": "2026-08-20",
                "amount": -10,
                "bookingStatus": "BOOKED",
            },
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-no-amount",
                "date": "2026-08-20",
                "bookingStatus": "BOOKED",
            },
        ]
    )
    skipped: list[tuple[str, dict]] = []

    async def on_skip(reason: str, context: dict) -> None:
        skipped.append((reason, context))

    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        results = await provider.fetch(SINCE, on_skip=on_skip)

    assert results == []
    assert len(skipped) == 4
    reasons = [reason for reason, _ in skipped]
    assert any("accountKey" in r for r in reasons)
    assert any("date field" in r for r in reasons)
    assert any("id field" in r for r in reasons)
    assert any("required fields" in r for r in reasons)
    # Every malformed row still known to belong to acct-1 carries that
    # account_key through in its context, so the engine can attribute the
    # skip to an account for the Audit Log's account filter/column.
    acct1_contexts = [ctx for reason, ctx in skipped if reason != "malformed: unrecognized or missing accountKey"]
    assert all(ctx.get("account_key") == "acct-1" for ctx in acct1_contexts)


@respx.mock
async def test_on_skip_not_called_for_pending_transactions(tmp_path: Path):
    # PENDING is routine, expected per-cycle behavior, not a diagnostic
    # "something went wrong" - must never be reported via on_skip (that
    # would flood the audit log every cycle for every still-pending
    # transaction).
    mock_transactions(
        [
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-pending",
                "date": "2026-08-20",
                "amount": -999,
                "bookingStatus": "PENDING",
            }
        ]
    )
    skipped: list[tuple[str, dict]] = []

    async def on_skip(reason: str, context: dict) -> None:
        skipped.append((reason, context))

    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        results = await provider.fetch(SINCE, on_skip=on_skip)

    assert results == []
    assert skipped == []


@respx.mock
async def test_fetch_without_on_skip_still_works(tmp_path: Path):
    # Backward compatibility: on_skip defaults to None, so every caller from
    # before this parameter existed keeps working unchanged.
    mock_transactions(
        [
            {
                "accountKey": "acct-1",
                "nonUniqueId": "nu-good",
                "date": "2026-08-20",
                "amount": -10,
                "bookingStatus": "BOOKED",
            }
        ]
    )
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        results = await provider.fetch(SINCE)

    assert len(results) == 1


@respx.mock
async def test_list_accounts_maps_fields(tmp_path: Path):
    respx.get(sb1_client.ACCOUNTS_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "accounts": [
                    {
                        "accountKey": "acct-1",
                        "name": "Brukskonto",
                        "type": "checking",
                        "currencyCode": "NOK",
                    }
                ]
            },
        )
    )
    async with httpx.AsyncClient() as http_client:
        provider = make_provider(tmp_path, http_client)
        accounts = await provider.list_accounts()

    assert accounts == [
        ProviderAccount(
            provider_account_id="acct-1",
            display_name="Brukskonto",
            account_type="checking",
            currency="NOK",
        )
    ]
