from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Mapping
from typing import Any, cast

import pytest

import pyowl_core.backends.native_views as native_views_module
from pyowl_core import (
    AxiomScope,
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    OntologySnapshot,
    load_snapshot,
)
from pyowl_core.backends import native
from pyowl_core.backends.python.parser import PythonParser
from pyowl_core.document.fingerprint import (
    document_fingerprint_bytes,
    logical_fingerprint,
    signature_fingerprint,
    snapshot_structural_fingerprint,
)
from pyowl_core.io.resolver import MappingResolver, MappingTarget
from pyowl_core.model import IRI
from tools.benchmark.comparators.adapters import (
    default_options,
    options_digest,
    options_inventory,
)
from tools.benchmark.comparators.common_contract import (
    CommonContractError,
    _canonical_provenance_origins,
    _document_preimage_parts,
    _logical_preimage_parts,
    _signature_preimage_parts,
    _structural_preimage_parts,
    build_core_common_contract,
    build_encoded_core_common_contract,
    common_contract_equality_key,
    validate_common_contract,
)
from tools.benchmark.manifest import generated_bytes, load_manifest
from tools.benchmark.native_redesign.encoded_contract import EncodedContractUnavailable
from tools.benchmark.synthetic import equivalent_source, import_diamond


@pytest.mark.parametrize(
    "format",
    (
        DocumentFormat.FUNCTIONAL,
        DocumentFormat.OWL_XML,
        DocumentFormat.TURTLE,
        DocumentFormat.RDF_XML,
    ),
)
def test_generated_syntaxes_match_all_authoritative_preimage_bytes(
    format: DocumentFormat,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = equivalent_source(format, 4)
    options = default_options(format)
    snapshot = load_snapshot(source, options=options)

    reference = build_core_common_contract(
        snapshot,
        corpus_id=f"generated-{format.value}",
        source_sha256=hashlib.sha256(source).hexdigest(),
        options_sha256=options_digest(options),
    )
    encoded = build_encoded_core_common_contract(
        snapshot,
        corpus_id=f"generated-{format.value}",
        source_sha256=hashlib.sha256(source).hexdigest(),
        options_sha256=options_digest(options),
        require_native_direct=False,
    )

    assert encoded.contract == reference
    _assert_all_preimages_match(snapshot, monkeypatch)


def test_import_diamond_matches_authoritative_document_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, imports = import_diamond()
    options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.RESOLVE_LOCAL,
        backend=BackendPreference.PYTHON,
    )
    snapshot = load_snapshot(
        source,
        options=options,
        resolver=MappingResolver(cast(Mapping[IRI | str, MappingTarget], imports)),
    )

    assert len(snapshot.documents) == 4
    _assert_all_preimages_match(snapshot, monkeypatch)
    contract_kwargs = {
        "corpus_id": "generated-import-diamond",
        "source_sha256": hashlib.sha256(source).hexdigest(),
        "options_sha256": options_digest(options),
    }
    reference = build_core_common_contract(snapshot, **contract_kwargs)
    encoded = build_encoded_core_common_contract(
        snapshot,
        require_native_direct=False,
        **contract_kwargs,
    )

    assert encoded.contract == reference
    assert encoded.evidence.view_count == 5
    assert encoded.evidence.document_view_count == 4


def test_annotated_swrl_logical_preimage_matches_authoritative_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
    )
    source = (
        b"Prefix(:=<urn:test#>) Ontology(<urn:rule> "
        b"SWRLRule(Annotation(:p :note) "
        b"(ClassAtom(:A Variable(:x)))"
        b"(ClassAtom(:B Variable(:x)))))"
    )
    document = PythonParser().parse(
        source,
        options=options,
        allow_swrl=True,
    )
    snapshot = load_snapshot(document, options=options)
    axioms = tuple(snapshot.iter_axioms())
    extensions = tuple(snapshot.iter_extensions())

    assert len(extensions) == 1
    expected = _capture_authoritative_preimage(
        monkeypatch,
        lambda: logical_fingerprint(axioms, extensions),
    )
    assert b"".join(_logical_preimage_parts(axioms, extensions)) == expected
    reference = build_core_common_contract(
        snapshot,
        corpus_id="annotated-swrl",
        source_sha256=hashlib.sha256(source).hexdigest(),
        options_sha256=options_digest(options),
    )
    encoded = build_encoded_core_common_contract(
        snapshot,
        corpus_id="annotated-swrl",
        source_sha256=hashlib.sha256(source).hexdigest(),
        options_sha256=options_digest(options),
        require_native_direct=False,
    )
    assert encoded.contract == reference


def test_core_common_contract_reconstructs_all_four_fingerprint_preimages() -> None:
    corpus = load_manifest().by_id("generated-tiny-functional")
    source = generated_bytes(corpus)
    options = default_options(corpus.format)
    snapshot = load_snapshot(source, options=options)
    options_sha256 = hashlib.sha256(_json(options_inventory(options))).hexdigest()

    contract = build_core_common_contract(
        snapshot,
        corpus_id=corpus.id,
        source_sha256=corpus.sha256,
        options_sha256=options_sha256,
    )

    validate_common_contract(contract)
    fingerprints = cast(dict[str, dict[str, Any]], contract["fingerprints"])
    assert fingerprints["document"]["digest"] == snapshot.root.document_fingerprint.hex
    assert fingerprints["structural"]["digest"] == snapshot.structural_fingerprint.hex
    assert fingerprints["logical"]["digest"] == snapshot.logical_fingerprint.hex
    assert fingerprints["signature"]["digest"] == snapshot.signature_fingerprint.hex
    assert all(value["preimage_bytes"] > 0 for value in fingerprints.values())
    assert contract["ledger"]["inventories"]["axioms"]["count"] == 15
    assert contract["ledger"]["diagnostic_count"] == 0
    assert any(
        occurrence.span is not None
        for occurrences in snapshot.origin_index.entries.values()
        for occurrence in occurrences
    )
    assert all(
        occurrence["span"] is None
        for origin in contract["provenance"]["origins"]
        for occurrence in origin["occurrences"]
    )


def test_rdf_provenance_ordinals_are_canonical_and_parser_order_independent() -> None:
    source = equivalent_source(DocumentFormat.RDF_XML, 3)
    snapshot = load_snapshot(source, options=default_options(DocumentFormat.RDF_XML))
    document_key = snapshot.root_document_key
    first_digest = b"\x11" * 32
    second_digest = b"\x22" * 32
    records = [
        (second_digest, document_key, 7, None),
        (first_digest, document_key, 42, None),
        (first_digest, document_key, 4, None),
        (second_digest, document_key, 1, None),
    ]

    document_formats = {document_key: DocumentFormat.RDF_XML}
    origins, row_count = _canonical_provenance_origins(records, document_formats=document_formats)
    reversed_origins, reversed_row_count = _canonical_provenance_origins(
        reversed(records), document_formats=document_formats
    )

    assert origins == reversed_origins
    assert row_count == reversed_row_count == 4
    assert origins == [
        {
            "structural_sha256": first_digest.hex(),
            "occurrences": [
                {"document_key": document_key, "occurrence": 0, "span": None},
                {"document_key": document_key, "occurrence": 1, "span": None},
            ],
        },
        {
            "structural_sha256": second_digest.hex(),
            "occurrences": [
                {"document_key": document_key, "occurrence": 2, "span": None},
                {"document_key": document_key, "occurrence": 3, "span": None},
            ],
        },
    ]


def test_rdf_provenance_ordinals_span_documents_before_regrouping() -> None:
    first_digest = b"\x11" * 32
    second_digest = b"\x22" * 32
    records = [
        (second_digest, "d:rdf-z", 3, None),
        (first_digest, "d:turtle-b", 8, None),
        (first_digest, "d:functional", 17, None),
        (first_digest, "d:rdf-a", 21, None),
    ]

    origins, row_count = _canonical_provenance_origins(
        records,
        document_formats={
            "d:rdf-a": DocumentFormat.RDF_XML,
            "d:rdf-z": DocumentFormat.RDF_XML,
            "d:turtle-b": DocumentFormat.TURTLE,
            "d:functional": DocumentFormat.FUNCTIONAL,
        },
    )

    assert row_count == 4
    assert origins == [
        {
            "structural_sha256": first_digest.hex(),
            "occurrences": [
                {"document_key": "d:functional", "occurrence": 17, "span": None},
                {"document_key": "d:rdf-a", "occurrence": 0, "span": None},
                {"document_key": "d:turtle-b", "occurrence": 1, "span": None},
            ],
        },
        {
            "structural_sha256": second_digest.hex(),
            "occurrences": [{"document_key": "d:rdf-z", "occurrence": 2, "span": None}],
        },
    ]


def test_common_contract_preserves_non_rdf_source_ordinals() -> None:
    source = equivalent_source(DocumentFormat.FUNCTIONAL, 2)
    snapshot = load_snapshot(source, options=default_options(DocumentFormat.FUNCTIONAL))
    document_key = snapshot.root_document_key
    digest = b"\x33" * 32

    origins, row_count = _canonical_provenance_origins(
        [
            (digest, document_key, 9, None),
            (digest, document_key, 3, None),
        ],
        document_formats={document_key: DocumentFormat.FUNCTIONAL},
    )

    assert row_count == 2
    assert origins == [
        {
            "structural_sha256": digest.hex(),
            "occurrences": [
                {"document_key": document_key, "occurrence": 3, "span": None},
                {"document_key": document_key, "occurrence": 9, "span": None},
            ],
        }
    ]


def test_rdf_common_contract_does_not_rewrite_actual_origin_index() -> None:
    source = equivalent_source(DocumentFormat.RDF_XML, 4)
    options = default_options(DocumentFormat.RDF_XML)
    snapshot = load_snapshot(source, options=options)
    actual_origins = tuple(
        (digest, tuple(occurrences))
        for digest, occurrences in snapshot.origin_index.entries.items()
    )

    contract = build_core_common_contract(
        snapshot,
        corpus_id="rdf-origin-ordinals",
        source_sha256=hashlib.sha256(source).hexdigest(),
        options_sha256=options_digest(options),
    )

    published_occurrences = [
        occurrence["occurrence"]
        for origin in contract["provenance"]["origins"]
        for occurrence in origin["occurrences"]
    ]
    assert published_occurrences == list(range(len(published_occurrences)))
    assert tuple(snapshot.origin_index.entries.items()) == actual_origins


def test_encoded_common_contract_matches_scalar_without_model_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = load_manifest().by_id("generated-tiny-functional")
    source = generated_bytes(corpus)
    options = default_options(corpus.format)
    snapshot = load_snapshot(source, options=options)
    digest = options_digest(options)
    reference = build_core_common_contract(
        snapshot,
        corpus_id=corpus.id,
        source_sha256=corpus.sha256,
        options_sha256=digest,
    )
    initial = build_encoded_core_common_contract(
        snapshot,
        corpus_id=corpus.id,
        source_sha256=corpus.sha256,
        options_sha256=digest,
        require_native_direct=False,
    )

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("bulk encoded contract crossed a scalar model callback")

    for name in ("iter_axioms", "iter_extensions", "ontology_annotations", "signature"):
        monkeypatch.setattr(type(snapshot), name, unexpected)
    encoded = build_encoded_core_common_contract(
        snapshot,
        corpus_id=corpus.id,
        source_sha256=corpus.sha256,
        options_sha256=digest,
        require_native_direct=False,
    )

    assert initial.contract == reference
    assert encoded.contract == reference
    assert encoded.evidence.view_count == 1
    assert encoded.evidence.document_view_count == 0
    assert encoded.evidence.referenced_buffer_bytes > 0
    assert encoded.evidence.referenced_buffer_copy_bytes == 0
    assert encoded.evidence.native_common_contract_summary_count == 0
    assert encoded.evidence.scalar_traversal_calls == 0
    assert encoded.evidence.structural_nodes_materialized == 0


def test_encoded_common_contract_requires_one_retained_exporter_by_default() -> None:
    corpus = load_manifest().by_id("generated-tiny-functional")
    source = generated_bytes(corpus)
    options = default_options(corpus.format)

    with pytest.raises(EncodedContractUnavailable, match="retained immutable"):
        build_encoded_core_common_contract(
            load_snapshot(source, options=options),
            corpus_id=corpus.id,
            source_sha256=corpus.sha256,
            options_sha256=options_digest(options),
        )


def test_encoded_common_contract_normalizes_and_deduplicates_annotated_logical_roots() -> None:
    source = (
        b"Ontology(<urn:annotated-contract> "
        b"SubClassOf(<urn:A> <urn:B>) "
        b"SubClassOf(Annotation(<urn:note> <urn:evidence-1>) <urn:A> <urn:B>) "
        b"SubClassOf(Annotation(<urn:note> <urn:evidence-2>) <urn:A> <urn:B>))"
    )
    options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
    )
    snapshot = load_snapshot(source, options=options)
    reference = build_core_common_contract(
        snapshot,
        corpus_id="annotated-logical",
        source_sha256=hashlib.sha256(source).hexdigest(),
        options_sha256=options_digest(options),
    )

    encoded = build_encoded_core_common_contract(
        snapshot,
        corpus_id="annotated-logical",
        source_sha256=hashlib.sha256(source).hexdigest(),
        options_sha256=options_digest(options),
        require_native_direct=False,
    )

    assert encoded.contract == reference
    assert encoded.contract["ledger"]["inventories"]["axioms"]["count"] == 3


def test_retained_native_encoded_contract_matches_scalar_without_model_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = native.probe(refresh=True)
    if not probe.available or "parse-functional-v1" not in probe.features:
        pytest.skip(probe.reason or "native Functional parser capability is unavailable")
    source = (
        b"Ontology(<urn:native-contract> "
        b"Declaration(Class(<urn:native-contract:A>)) "
        b"Declaration(Class(<urn:native-contract:B>)) "
        b"SubClassOf(<urn:native-contract:A> <urn:native-contract:B>) "
        b"SubClassOf(Annotation(<urn:note> <urn:evidence>) "
        b"<urn:native-contract:A> <urn:native-contract:B>))"
    )
    python_options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
        collect_provenance=True,
    )
    native_options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.NATIVE,
        collect_provenance=True,
    )
    reference_snapshot = load_snapshot(source, options=python_options)
    selected = load_snapshot(source, options=native_options)
    digest = options_digest(python_options)
    source_sha256 = hashlib.sha256(source).hexdigest()
    reference = build_core_common_contract(
        reference_snapshot,
        corpus_id="native-retained-contract",
        source_sha256=source_sha256,
        options_sha256=digest,
    )

    def unexpected(*_arguments: object, **_keywords: object) -> object:
        raise AssertionError("retained native contract crossed a scalar model callback")

    before = cast(Any, selected)._native_python_counters()
    monkeypatch.setattr(native_views_module, "decode_canonical", unexpected)
    for name in (
        "iter_axioms",
        "iter_extensions",
        "ontology_annotations",
        "signature",
        "view",
    ):
        monkeypatch.setattr(type(selected), name, unexpected)
    encoded = build_encoded_core_common_contract(
        selected,
        corpus_id="native-retained-contract",
        source_sha256=source_sha256,
        options_sha256=digest,
    )
    after = cast(Any, selected)._native_python_counters()

    assert encoded.contract == reference
    assert encoded.evidence.view_count == 0
    assert encoded.evidence.document_view_count == 0
    assert encoded.evidence.referenced_buffer_bytes == 0
    assert encoded.evidence.referenced_buffer_copy_bytes == 0
    assert encoded.evidence.native_common_contract_summary_count == 1
    assert encoded.evidence.provenance_rows_streamed == sum(
        len(row["occurrences"]) for row in reference["provenance"]["origins"]
    )
    assert encoded.evidence.scalar_traversal_calls == 0
    assert encoded.evidence.structural_nodes_materialized == 0
    assert after.model_rows_materialized == before.model_rows_materialized
    assert after.auxiliary_rows_decoded == before.auxiliary_rows_decoded


def test_retained_native_common_contract_rejects_tampered_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = native.probe(refresh=True)
    if not probe.available or "parse-functional-v1" not in probe.features:
        pytest.skip(probe.reason or "native Functional parser capability is unavailable")
    source = b"Ontology(<urn:native-contract-tamper> Declaration(Class(<urn:A>)))"
    options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.NATIVE,
        collect_provenance=True,
    )
    selected = load_snapshot(source, options=options)
    summary = copy.copy(cast(Any, selected)._native_common_contract_summary_v1())
    document_fingerprint = copy.copy(summary.document_fingerprint)
    object.__setattr__(document_fingerprint, "sha256", b"\x00" * 32)
    object.__setattr__(summary, "document_fingerprint", document_fingerprint)
    monkeypatch.setattr(
        type(selected),
        "_native_common_contract_summary_v1",
        lambda _self: summary,
    )

    with pytest.raises(CommonContractError, match="document fingerprint disagrees"):
        build_encoded_core_common_contract(
            selected,
            corpus_id="native-retained-contract-tamper",
            source_sha256=hashlib.sha256(source).hexdigest(),
            options_sha256=options_digest(options),
        )


def test_common_contract_is_deterministic_for_identical_bytes_and_options() -> None:
    corpus = load_manifest().by_id("generated-tiny-functional")
    source = generated_bytes(corpus)
    options = default_options(corpus.format)
    digest = hashlib.sha256(_json(options_inventory(options))).hexdigest()

    first = build_core_common_contract(
        load_snapshot(source, options=options),
        corpus_id=corpus.id,
        source_sha256=corpus.sha256,
        options_sha256=digest,
    )
    second_options = LoadOptions(
        format=options.format,
        imports=options.imports,
        backend=BackendPreference.PYTHON,
        limits=options.limits,
        offline=options.offline,
        preserve_source_map=options.preserve_source_map,
        collect_provenance=options.collect_provenance,
        validate_owl2_dl=options.validate_owl2_dl,
        deterministic=options.deterministic,
    )
    second = build_core_common_contract(
        load_snapshot(source, options=second_options),
        corpus_id=corpus.id,
        source_sha256=corpus.sha256,
        options_sha256=digest,
    )

    assert first == second
    assert common_contract_equality_key(first) == common_contract_equality_key(second)


def test_common_contract_rejects_post_timer_inventory_tampering() -> None:
    corpus = load_manifest().by_id("generated-tiny-functional")
    source = generated_bytes(corpus)
    options = default_options(corpus.format)
    contract = build_core_common_contract(
        load_snapshot(source, options=options),
        corpus_id=corpus.id,
        source_sha256=corpus.sha256,
        options_sha256=hashlib.sha256(_json(options_inventory(options))).hexdigest(),
    )
    changed = copy.deepcopy(contract)
    changed["ledger"]["inventories"]["axioms"]["count"] += 1

    with pytest.raises(CommonContractError, match="contract digest mismatch"):
        validate_common_contract(changed)


def test_equality_key_includes_fingerprint_preimage_evidence() -> None:
    corpus = load_manifest().by_id("generated-tiny-functional")
    source = generated_bytes(corpus)
    options = default_options(corpus.format)
    contract = build_core_common_contract(
        load_snapshot(source, options=options),
        corpus_id=corpus.id,
        source_sha256=corpus.sha256,
        options_sha256=hashlib.sha256(_json(options_inventory(options))).hexdigest(),
    )
    changed = copy.deepcopy(contract)
    changed["fingerprints"]["document"]["preimage_bytes"] += 1
    unsigned = dict(changed)
    unsigned.pop("contract_sha256")
    changed["contract_sha256"] = hashlib.sha256(_json(unsigned)).hexdigest()

    assert common_contract_equality_key(changed) != common_contract_equality_key(contract)


def test_equality_key_is_independent_of_inventory_mapping_order() -> None:
    corpus = load_manifest().by_id("generated-tiny-functional")
    source = generated_bytes(corpus)
    options = default_options(corpus.format)
    contract = build_core_common_contract(
        load_snapshot(source, options=options),
        corpus_id=corpus.id,
        source_sha256=corpus.sha256,
        options_sha256=hashlib.sha256(_json(options_inventory(options))).hexdigest(),
    )
    reordered = copy.deepcopy(contract)
    inventories = reordered["ledger"]["inventories"]
    reordered["ledger"]["inventories"] = dict(reversed(tuple(inventories.items())))

    assert common_contract_equality_key(reordered) == common_contract_equality_key(contract)


def _assert_all_preimages_match(
    snapshot: OntologySnapshot,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    axioms = tuple(snapshot.iter_axioms())
    extensions = tuple(snapshot.iter_extensions())
    signature = snapshot.signature(include_builtins=True)
    documents = tuple(
        (
            record.document_key,
            snapshot.ontology_annotations(
                scope=AxiomScope.DOCUMENT,
                document_key=record.document_key,
            ),
            tuple(
                snapshot.iter_axioms(
                    scope=AxiomScope.DOCUMENT,
                    document_key=record.document_key,
                )
            ),
            tuple(
                snapshot.iter_extensions(
                    scope=AxiomScope.DOCUMENT,
                    document_key=record.document_key,
                )
            ),
        )
        for record in snapshot.import_manifest.documents
    )
    expected = {
        "document": document_fingerprint_bytes(snapshot.root),
        "structural": _capture_authoritative_preimage(
            monkeypatch,
            lambda: snapshot_structural_fingerprint(snapshot.import_manifest, documents),
        ),
        "logical": _capture_authoritative_preimage(
            monkeypatch,
            lambda: logical_fingerprint(axioms, extensions),
        ),
        "signature": _capture_authoritative_preimage(
            monkeypatch,
            lambda: signature_fingerprint(signature, include_builtins=True),
        ),
    }
    observed = {
        "document": b"".join(_document_preimage_parts(snapshot.root)),
        "structural": b"".join(_structural_preimage_parts(snapshot)),
        "logical": b"".join(_logical_preimage_parts(axioms, extensions)),
        "signature": b"".join(_signature_preimage_parts(signature, include_builtins=True)),
    }

    assert observed == expected


def _capture_authoritative_preimage(
    monkeypatch: pytest.MonkeyPatch,
    operation: Callable[[], object],
) -> bytes:
    original_sha256 = hashlib.sha256
    captured: list[bytearray] = []

    class RecordingHash:
        def __init__(self, data: bytes = b"") -> None:
            self.data = bytearray(data)
            captured.append(self.data)

        def update(self, data: bytes) -> None:
            self.data.extend(data)

        def digest(self) -> bytes:
            return original_sha256(self.data).digest()

    with monkeypatch.context() as context:
        context.setattr("pyowl_core.document.fingerprint.hashlib.sha256", RecordingHash)
        operation()

    assert len(captured) == 1
    return bytes(captured[0])


def _json(value: object) -> bytes:
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
