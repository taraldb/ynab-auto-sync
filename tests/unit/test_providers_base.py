from datetime import datetime

import pytest

from ynab_auto_sync.providers.base import (
    BookingStatus,
    ProviderAccount,
    TransactionProvider,
)
from ynab_auto_sync.providers.registry import REGISTRY, get_provider_class, register


def test_booking_status_string_values_are_exact_literals():
    # These exact strings are already persisted in the SQLite
    # tracked_transactions.booking_status column and compared as literals
    # elsewhere (see state_db.py) - a rename here would be a silent schema
    # break, not just a refactor.
    assert BookingStatus.PENDING == "PENDING"
    assert BookingStatus.BOOKED == "BOOKED"
    assert BookingStatus.DELETED == "DELETED"
    assert str(BookingStatus.PENDING) == "PENDING"


def test_register_adds_to_registry_keyed_by_type_name():
    REGISTRY.clear()

    @register
    class FakeProvider(TransactionProvider):
        @staticmethod
        def type_name() -> str:
            return "fake"

        async def list_accounts(self) -> list[ProviderAccount]:
            return []

        async def fetch(self, since_by_account: dict[str, datetime]) -> list:
            return []

    assert REGISTRY == {"fake": FakeProvider}
    assert get_provider_class("fake") is FakeProvider

    REGISTRY.clear()


def test_get_provider_class_raises_clear_error_for_unknown_type():
    REGISTRY.clear()

    @register
    class OtherProvider(TransactionProvider):
        @staticmethod
        def type_name() -> str:
            return "other"

        async def list_accounts(self) -> list[ProviderAccount]:
            return []

        async def fetch(self, since_by_account: dict[str, datetime]) -> list:
            return []

    with pytest.raises(ValueError, match="unknown-provider"):
        get_provider_class("unknown-provider")

    with pytest.raises(ValueError, match="other"):
        get_provider_class("unknown-provider")

    REGISTRY.clear()


def test_get_provider_class_error_message_when_registry_empty():
    REGISTRY.clear()

    with pytest.raises(ValueError, match="none registered"):
        get_provider_class("anything")
