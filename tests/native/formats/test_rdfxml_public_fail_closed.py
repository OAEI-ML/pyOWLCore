from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest

from pyowl_core import (
    BackendPreference,
    CancellationSource,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    OntologySyntaxError,
    OperationCancelledError,
    ParseLimits,
    ResourceLimitError,
    load_snapshot,
    parse_document,
)
from pyowl_core.backends import native
from pyowl_core.cancellation import CancellationToken
from tests.native.foundation._support import NativeTestExtension, load_extension

RDFXML_SOURCE = b"""\
<rdf:RDF
  xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
  xmlns:owl="http://www.w3.org/2002/07/owl#">
  <owl:Ontology rdf:about="urn:rdfxml:public-failure"/>
  <owl:Class rdf:about="urn:rdfxml:A">
    <rdfs:subClassOf rdf:resource="urn:rdfxml:B"/>
  </owl:Class>
  <owl:Class rdf:about="urn:rdfxml:B"/>
</rdf:RDF>
"""

_PublicLoader = Callable[..., object]


@pytest.fixture(scope="module", autouse=True)
def extension() -> NativeTestExtension:
    selected = load_extension()
    native._reset_probe_cache_for_tests()
    result = native.probe(refresh=True)
    if not result.available:
        pytest.skip(result.reason or "native backend unavailable")
    if not hasattr(selected, "_parse_rdfxml_retained_v2"):
        pytest.skip("selected native artifact lacks retained RDF/XML production seam")
    return selected


def _options(*, limits: ParseLimits | None = None) -> LoadOptions:
    return LoadOptions(
        format=DocumentFormat.RDF_XML,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.NATIVE,
        limits=ParseLimits() if limits is None else limits,
    )


def _load(
    loader: _PublicLoader,
    source: bytes,
    *,
    options: LoadOptions,
    cancellation_token: CancellationToken | None = None,
) -> object:
    if cancellation_token is None:
        return loader(source, options=options)
    return loader(
        source,
        options=options,
        cancellation_token=cancellation_token,
    )


@pytest.mark.parametrize("loader", (load_snapshot, parse_document))
def test_guarded_public_rdfxml_syntax_failure_never_publishes(
    loader: _PublicLoader,
) -> None:
    unexpected_python = AssertionError("guarded RDF/XML failure crossed the Python parser")
    unexpected_publication = AssertionError("invalid RDF/XML reached owner publication")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch(
            "pyowl_core.backends.python.parser.parse_rdfxml",
            side_effect=unexpected_python,
        ),
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.publish_retained_rdfxml",
            side_effect=unexpected_publication,
        ),
        pytest.raises(OntologySyntaxError) as raised,
    ):
        _load(loader, b"<rdf:RDF", options=_options())

    assert raised.value.code == "RDFXML_SYNTAX"


@pytest.mark.parametrize("loader", (load_snapshot, parse_document))
@pytest.mark.parametrize(
    ("limits", "expected_limit"),
    (
        (ParseLimits(max_triples=1), "max_triples"),
        (ParseLimits(max_axioms=1), "max_axioms"),
    ),
)
def test_guarded_public_rdfxml_limits_never_publish(
    loader: _PublicLoader,
    limits: ParseLimits,
    expected_limit: str,
) -> None:
    unexpected_python = AssertionError("guarded RDF/XML limit crossed the Python parser")
    unexpected_publication = AssertionError("over-limit RDF/XML reached owner publication")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch(
            "pyowl_core.backends.python.parser.parse_rdfxml",
            side_effect=unexpected_python,
        ),
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.publish_retained_rdfxml",
            side_effect=unexpected_publication,
        ),
        pytest.raises(ResourceLimitError) as raised,
    ):
        _load(loader, RDFXML_SOURCE, options=_options(limits=limits))

    assert raised.value.code == "NATIVE_WIRE_LIMIT"
    assert expected_limit in str(raised.value)


def test_guarded_public_rdfxml_cancellation_never_publishes() -> None:
    cancellation = CancellationSource()
    reason = "guarded public RDF/XML parser cancelled"
    real_parse = native._parse_rdfxml_retained_v2

    def cancel_at_native_entry(*args: Any, **kwargs: Any) -> object:
        cancellation.cancel(reason)
        return real_parse(*args, **kwargs)

    unexpected_python = AssertionError("cancelled RDF/XML crossed the Python parser")
    unexpected_publication = AssertionError("cancelled RDF/XML reached owner publication")
    with (
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.select",
            autospec=True,
            return_value="native",
        ),
        patch(
            "pyowl_core.backends.python.parser.parse_rdfxml",
            side_effect=unexpected_python,
        ),
        patch.object(
            native,
            "_parse_rdfxml_retained_v2",
            side_effect=cancel_at_native_entry,
        ),
        patch(
            "pyowl_core.backends.parser._NativeBackendDriver.publish_retained_rdfxml",
            side_effect=unexpected_publication,
        ),
        pytest.raises(OperationCancelledError, match=reason) as raised,
    ):
        _load(
            load_snapshot,
            RDFXML_SOURCE,
            options=_options(),
            cancellation_token=cancellation.token,
        )

    assert raised.value.code == "OPERATION_CANCELLED"
    assert raised.value.reason == reason
