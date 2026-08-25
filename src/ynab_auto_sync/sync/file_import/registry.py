from __future__ import annotations

from .base import TransformerBase

REGISTRY: list[type[TransformerBase]] = []


def register(cls: type[TransformerBase]) -> type[TransformerBase]:
    REGISTRY.append(cls)
    return cls


def detect_transformer(headers: list[str]) -> TransformerBase | None:
    for cls in REGISTRY:
        if cls.can_handle(headers):
            return cls()
    return None


def list_transformer_names() -> list[str]:
    return [cls.name() for cls in REGISTRY]
