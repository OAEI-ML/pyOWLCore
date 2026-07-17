"""Explicit ordered composition for import resolvers."""

from __future__ import annotations

from collections.abc import Iterable

from pyowl_core.model import encode_varint

from .base import (
    ImportRequest,
    ImportResolver,
    ResolutionAttempt,
    ResolutionKind,
    ResolutionMode,
    ResolvedDocument,
    ResolverOutcome,
    resolve_with_mode,
    resolver_configuration_fingerprint,
)


class CompositeResolver:
    """Try configured children in order and retain the complete outcome trace."""

    __slots__ = ("_resolvers",)
    name = "composite"

    def __init__(self, resolvers: Iterable[ImportResolver]) -> None:
        values = tuple(resolvers)
        if not values:
            raise ValueError("CompositeResolver requires at least one child")
        if not all(isinstance(value, ImportResolver) for value in values):
            raise TypeError("resolvers must implement ImportResolver")
        self._resolvers = values

    @property
    def resolvers(self) -> tuple[ImportResolver, ...]:
        return self._resolvers

    @property
    def network_capable(self) -> bool:
        return any(bool(getattr(item, "network_capable", False)) for item in self._resolvers)

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None:
        return self.resolve_outcome(request, mode=ResolutionMode.NETWORK).resolved

    def resolve_outcome(self, request: ImportRequest, *, mode: ResolutionMode) -> ResolverOutcome:
        attempts: list[ResolutionAttempt] = []
        for index, resolver in enumerate(self._resolvers, 1):
            request.limits.enforce("max_resolver_attempts", index)
            outcome = resolve_with_mode(resolver, request, mode=mode)
            attempts.extend(outcome.attempts)
            if outcome.kind is ResolutionKind.NOT_FOUND:
                continue
            return ResolverOutcome(
                outcome.kind,
                outcome.resolver_name,
                outcome.resolved,
                tuple(attempts),
                outcome.error,
            )
        return ResolverOutcome.missing(self.name, attempts=tuple(attempts))

    def configuration_bytes(self) -> bytes:
        pieces = [b"composite:v1", encode_varint(len(self._resolvers))]
        for resolver in self._resolvers:
            pieces.append(resolver_configuration_fingerprint(resolver))
        return b"".join(pieces)


__all__ = ["CompositeResolver"]
