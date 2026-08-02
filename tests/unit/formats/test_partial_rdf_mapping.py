from __future__ import annotations

import io

import pytest

from pyowl_core import (
    BackendPreference,
    LoadOptions,
    OptionConflictError,
    UnsupportedSyntaxError,
    coerce_snapshot,
    load_snapshot,
    parse_document,
)

SOURCE = b"""\
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:e="urn:diagnostic:">
  <rdf:Description rdf:about="urn:subject">
    <e:unknown rdf:resource="urn:object"/>
  </rdf:Description>
</rdf:RDF>
"""


class _UnreadableSource(io.BytesIO):
    def read(self, *_args: object, **_kwargs: object) -> bytes:
        raise AssertionError("source must not be consumed")


def _partial_options() -> LoadOptions:
    return LoadOptions(
        backend=BackendPreference.PYTHON,
        allow_partial_rdf_mapping=True,
    )


def test_curated_partial_mode_is_explicit_and_diagnostic_only() -> None:
    with pytest.raises(UnsupportedSyntaxError) as strict:
        parse_document(
            SOURCE,
            format="rdfxml",
            options=LoadOptions(backend=BackendPreference.PYTHON),
        )

    document = parse_document(SOURCE, format="rdfxml", options=_partial_options())
    assert document.rdf_mapping_report is not None
    assert not document.rdf_mapping_report.conformant
    assert document.rdf_mapping_report.dropped_triples == 1
    assert document.rdf_mapping_report == strict.value.rdf_mapping_report


@pytest.mark.parametrize("format", ("functional", "owlxml"))
def test_non_rdf_partial_mode_rejects_before_source_consumption(format: str) -> None:
    with pytest.raises(OptionConflictError) as caught:
        parse_document(_UnreadableSource(), format=format, options=_partial_options())
    assert caught.value.code == "PARTIAL_RDF_MAPPING_FORMAT_CONFLICT"


def test_partial_autodetection_rejects_before_source_consumption() -> None:
    with pytest.raises(OptionConflictError) as caught:
        parse_document(_UnreadableSource(), options=_partial_options())
    assert caught.value.code == "PARTIAL_RDF_MAPPING_FORMAT_REQUIRED"


def test_snapshot_loading_rejects_partial_option_before_source_consumption() -> None:
    with pytest.raises(OptionConflictError) as caught:
        load_snapshot(_UnreadableSource(), options=_partial_options())
    assert caught.value.code == "PARTIAL_RDF_MAPPING_SNAPSHOT_FORBIDDEN"


def test_nonconformant_document_cannot_enter_snapshot_or_coercion_routes() -> None:
    document = parse_document(SOURCE, format="rdfxml", options=_partial_options())
    for operation in (load_snapshot, coerce_snapshot):
        with pytest.raises(OptionConflictError) as caught:
            operation(document)
        assert caught.value.code == "PARTIAL_RDF_MAPPING_SNAPSHOT_FORBIDDEN"


def test_load_options_rejects_non_boolean_partial_switch() -> None:
    with pytest.raises(TypeError, match="allow_partial_rdf_mapping"):
        LoadOptions(allow_partial_rdf_mapping=1)  # type: ignore[arg-type]
