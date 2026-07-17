from __future__ import annotations

from dataclasses import replace

import pytest

from pyowl_core import (
    IRI,
    AccessDeniedError,
    CompositeResolver,
    ImportCycleError,
    ImportRequest,
    MappingResolver,
    ParseLimits,
    ResolutionKind,
    ResolutionMode,
    ResolvedDocument,
    ResolverOutcome,
    resolver_configuration_fingerprint,
)


def request(iri: str = "urn:test") -> ImportRequest:
    return ImportRequest(IRI(iri), None, (IRI(iri),), ParseLimits())


def test_mapping_exact_alias_and_configuration_are_deterministic() -> None:
    document = ResolvedDocument(b"Ontology()", IRI("urn:canonical"))
    first = MappingResolver({"urn:b": IRI("urn:a"), "urn:a": document})
    second = MappingResolver({"urn:a": document, "urn:b": IRI("urn:a")})

    outcome = first.resolve_outcome(request("urn:b"), mode=ResolutionMode.LOCAL_ONLY)

    assert outcome.kind is ResolutionKind.RESOLVED
    assert outcome.resolved is document
    assert len(outcome.attempts) == 2
    assert first.resolve(request("urn:missing")) is None
    assert resolver_configuration_fingerprint(first) == resolver_configuration_fingerprint(second)


def test_mapping_alias_cycle_and_limit_are_distinct() -> None:
    cycle = MappingResolver({"urn:a": IRI("urn:b"), "urn:b": IRI("urn:a")})
    with pytest.raises(ImportCycleError, match="alias cycle") as caught:
        cycle.resolve(request("urn:a"))
    assert caught.value.code == "IMPORT_ALIAS_CYCLE"

    chain = MappingResolver(
        {
            "urn:a": IRI("urn:b"),
            "urn:b": IRI("urn:c"),
            "urn:c": b"Ontology()",
        }
    )
    limited = replace(request("urn:a"), limits=replace(ParseLimits(), max_catalog_rewrites=1))
    with pytest.raises(ImportCycleError) as caught_limit:
        chain.resolve(limited)
    assert caught_limit.value.code == "IMPORT_ALIAS_LIMIT"


class _Missing:
    name = "missing"

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None:
        del request
        return None

    def configuration_bytes(self) -> bytes:
        return b"missing"


class _Denied:
    name = "denied"

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None:
        del request
        raise AccessDeniedError("denied")

    def configuration_bytes(self) -> bytes:
        return b"denied"


class _Network:
    name = "network"
    network_capable = True

    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, request: ImportRequest) -> ResolvedDocument | None:
        self.calls += 1
        return ResolvedDocument(b"Ontology()", request.import_iri)

    def configuration_bytes(self) -> bytes:
        return b"network"


def test_composite_order_trace_denial_and_local_network_filter() -> None:
    network = _Network()
    composite = CompositeResolver((_Missing(), network))
    local = composite.resolve_outcome(request(), mode=ResolutionMode.LOCAL_ONLY)
    assert local.kind is ResolutionKind.NOT_FOUND
    assert network.calls == 0
    assert [item.resolver_name for item in local.attempts] == ["missing", "network"]

    online = composite.resolve_outcome(request(), mode=ResolutionMode.NETWORK)
    assert online.kind is ResolutionKind.RESOLVED
    assert network.calls == 1

    denied = CompositeResolver((_Denied(), network)).resolve_outcome(
        request(), mode=ResolutionMode.NETWORK
    )
    assert denied.kind is ResolutionKind.DENIED
    assert network.calls == 1


def test_contract_values_reject_invalid_inputs() -> None:
    with pytest.raises(TypeError):
        ImportRequest("urn:x", None, (), ParseLimits())  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ResolvedDocument(b"x", IRI("urn:x"), expected_sha256=b"short")
    with pytest.raises(ValueError):
        ResolverOutcome(ResolutionKind.RESOLVED, "mapping")
