"""Connector registry: connectors are discovered by key; destinations are rows, never code."""
from app.connectors.base import BaseConnector
from app.connectors.platforms import (AssistedConnector, DevtoConnector, DiscourseConnector, GhostConnector,
                                      LemmyConnector, WebhookConnector)
from app.errors import AppError

_CLASSES: list[type[BaseConnector]] = [DiscourseConnector, LemmyConnector, DevtoConnector, GhostConnector,
                                       WebhookConnector, AssistedConnector]
_REGISTRY: dict[str, BaseConnector] = {}


def get_registry() -> dict[str, BaseConnector]:
    if not _REGISTRY:
        for cls in _CLASSES:
            inst = cls()
            _REGISTRY[inst.key] = inst
    return _REGISTRY


def get_connector(key: str) -> BaseConnector:
    reg = get_registry()
    if key not in reg:
        raise AppError("NOT_FOUND", f"unknown connector '{key}'", {"known": sorted(reg)}, 404)
    return reg[key]
