from __future__ import annotations

from .base import TransactionProvider

REGISTRY: dict[str, type[TransactionProvider]] = {}


def register(cls: type[TransactionProvider]) -> type[TransactionProvider]:
    REGISTRY[cls.type_name()] = cls
    return cls


def get_provider_class(type_name: str) -> type[TransactionProvider]:
    try:
        return REGISTRY[type_name]
    except KeyError:
        available = sorted(REGISTRY) or ["<none registered>"]
        raise ValueError(
            f"Unknown provider type {type_name!r}. Available: {available}"
        ) from None
