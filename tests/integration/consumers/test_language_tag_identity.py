from __future__ import annotations

from pathlib import Path

import pyowl_core

from ._support import (
    FIXTURE_DOCUMENT_IRI,
    FIXTURE_SOURCE,
    language_assertion,
    load_options,
    source_language,
)


def _elk_legacy_key(literal: pyowl_core.Literal, source_tag: str | None) -> tuple[str, str]:
    """Fixture-only model of pyELK's explicitly private source-spelling key."""

    return literal.lexical_form, source_tag or literal.language or ""


def _hermit_canonical_key(literal: pyowl_core.Literal) -> tuple[str, str]:
    return literal.lexical_form, literal.language or ""


def test_one_canonical_core_identity_and_consumer_local_language_keys(tmp_path: Path) -> None:
    direct = pyowl_core.load_snapshot(
        FIXTURE_SOURCE,
        document_iri=FIXTURE_DOCUMENT_IRI,
        options=load_options(preserve_source_map=True),
    )
    lower = pyowl_core.load_snapshot(
        FIXTURE_SOURCE.replace(b"@EN-gb", b"@en-GB"),
        document_iri=FIXTURE_DOCUMENT_IRI,
        options=load_options(preserve_source_map=True),
    )
    wire = pyowl_core.encode_snapshot(direct)
    decoded = pyowl_core.decode_snapshot(wire)
    path = tmp_path / "language.pyocore"
    path.write_bytes(wire)
    mapped = pyowl_core.open_snapshot(path, mmap=True, verify=True)
    try:
        direct_assertion = language_assertion(direct)
        lower_assertion = language_assertion(lower)
        decoded_assertion = language_assertion(decoded)
        mapped_assertion = language_assertion(mapped)
        literals = (
            direct_assertion.value,
            lower_assertion.value,
            decoded_assertion.value,
            mapped_assertion.value,
        )
        assert all(isinstance(item, pyowl_core.Literal) for item in literals)
        assert literals[0] == literals[1] == literals[2] == literals[3]
        assert {item.language for item in literals if isinstance(item, pyowl_core.Literal)} == {
            "en-gb"
        }
        assert (
            direct.logical_fingerprint == lower.logical_fingerprint == decoded.logical_fingerprint
        )
        assert direct.signature_fingerprint == lower.signature_fingerprint

        assert source_language(direct.root) == "EN-gb"
        assert source_language(lower.root) == "en-GB"
        assert source_language(decoded.root) is None
        assert source_language(mapped.root) is None

        direct_literal = direct_assertion.value
        lower_literal = lower_assertion.value
        decoded_literal = decoded_assertion.value
        assert isinstance(direct_literal, pyowl_core.Literal)
        assert isinstance(lower_literal, pyowl_core.Literal)
        assert isinstance(decoded_literal, pyowl_core.Literal)

        assert _elk_legacy_key(direct_literal, source_language(direct.root)) == (
            "Canonical name",
            "EN-gb",
        )
        assert _elk_legacy_key(lower_literal, source_language(lower.root)) == (
            "Canonical name",
            "en-GB",
        )
        assert _elk_legacy_key(decoded_literal, None) == ("Canonical name", "en-gb")
        assert _hermit_canonical_key(direct_literal) == _hermit_canonical_key(lower_literal)
        assert _hermit_canonical_key(decoded_literal) == ("Canonical name", "en-gb")
    finally:
        mapped.close()


def test_language_source_spelling_is_provenance_only_not_cache_identity() -> None:
    from ._support import consumer_cache_key

    upper = pyowl_core.load_snapshot(
        FIXTURE_SOURCE,
        document_iri=FIXTURE_DOCUMENT_IRI,
        options=load_options(preserve_source_map=True),
    )
    lower = pyowl_core.load_snapshot(
        FIXTURE_SOURCE.replace(b"@EN-gb", b"@en-gb"),
        document_iri=FIXTURE_DOCUMENT_IRI,
        options=load_options(preserve_source_map=True),
    )

    assert consumer_cache_key(upper) == consumer_cache_key(lower)
    assert upper.logical_fingerprint == lower.logical_fingerprint
    assert upper.structural_fingerprint == lower.structural_fingerprint
    assert upper.root.provenance.source_sha256 != lower.root.provenance.source_sha256
