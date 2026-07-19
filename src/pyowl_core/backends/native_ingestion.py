"""Fail-closed Python seam for WP16-owned native ingestion bindings."""

from __future__ import annotations

from typing import Protocol, cast

from pyowl_core.exceptions import BackendProtocolError

from . import native


class NativeIngestionExtension(Protocol):
    INGESTION_FEATURES: tuple[str, ...]


def require_ingestion_binding(capability: str) -> NativeIngestionExtension:
    """Require a capability registered specifically by the ingestion seam."""

    extension = native.require(capability)
    if capability not in extension.INGESTION_FEATURES:
        raise BackendProtocolError(
            "native capability is not registered by the ingestion binding seam",
            code="NATIVE_INGESTION_REGISTRATION",
        )
    return cast(NativeIngestionExtension, extension)


__all__ = ["NativeIngestionExtension", "require_ingestion_binding"]
