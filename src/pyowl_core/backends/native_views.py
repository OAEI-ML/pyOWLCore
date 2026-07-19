"""Fail-closed Python seam for WP17-owned native view/index/wire bindings."""

from __future__ import annotations

from typing import Protocol, cast

from pyowl_core.exceptions import BackendProtocolError

from . import native


class NativeViewExtension(Protocol):
    VIEW_FEATURES: tuple[str, ...]


def require_view_binding(capability: str) -> NativeViewExtension:
    """Require a capability registered specifically by the view seam."""

    extension = native.require(capability)
    if capability not in extension.VIEW_FEATURES:
        raise BackendProtocolError(
            "native capability is not registered by the view binding seam",
            code="NATIVE_VIEW_REGISTRATION",
        )
    return cast(NativeViewExtension, extension)


__all__ = ["NativeViewExtension", "require_view_binding"]
