from __future__ import annotations

from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from pyowl_core import BackendPreference, LoadOptions, ParseLimits, PyOWLCoreError, parse_document
from tools.security.mutations import mutations

ROOT = Path(__file__).parents[3]
CORPUS = ROOT / "tests" / "data" / "corpus" / "w3c"
SEEDS = {
    "functional": CORPUS / "functional" / "minimal.ofn",
    "owlxml": CORPUS / "owlxml" / "minimal.owx",
    "rdfxml": CORPUS / "rdfxml" / "minimal.rdf",
    "turtle": CORPUS / "turtle" / "minimal.ttl",
}
OPTIONS = LoadOptions(
    backend=BackendPreference.PYTHON,
    limits=ParseLimits(
        max_source_bytes=16 * 1024,
        max_nesting_depth=32,
        max_terms=10_000,
        max_axioms=1_000,
        max_triples=5_000,
        max_canonical_work=100_000,
    ),
)


def _parse(data: bytes, format: str) -> None:
    try:
        document = parse_document(
            data,
            format=format,
            document_iri="https://example.org/fuzz/document",
            options=OPTIONS,
        )
    except PyOWLCoreError as error:
        assert error.code and error.code == error.code.upper()
    else:
        assert len(document.document_fingerprint.digest) == 32


def test_mutated_parser_seeds_stay_inside_public_error_boundary() -> None:
    for format, path in SEEDS.items():
        for mutation in mutations(path.read_bytes(), maximum=96):
            _parse(mutation.data, format)


@settings(max_examples=64, deadline=None, derandomize=True)
@given(
    format=st.sampled_from(tuple(SEEDS)),
    data=st.binary(min_size=0, max_size=512),
)
def test_arbitrary_parser_bytes_stay_inside_public_error_boundary(
    format: str,
    data: bytes,
) -> None:
    _parse(data, format)
