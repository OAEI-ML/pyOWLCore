from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable, Mapping
from typing import Any, cast

import pytest

from pyowl_core import (
    AxiomScope,
    BackendPreference,
    DocumentFormat,
    ImportPolicy,
    LoadOptions,
    OntologySnapshot,
    load_snapshot,
)
from pyowl_core.backends.python.parser import PythonParser
from pyowl_core.document.fingerprint import (
    document_fingerprint_bytes,
    logical_fingerprint,
    signature_fingerprint,
    snapshot_structural_fingerprint,
)
from pyowl_core.io.resolver import MappingResolver, MappingTarget
from pyowl_core.model import IRI
from tools.benchmark.comparators.adapters import default_options, options_inventory
from tools.benchmark.comparators.common_contract import (
    CommonContractError,
    _document_preimage_parts,
    _logical_preimage_parts,
    _signature_preimage_parts,
    _structural_preimage_parts,
    build_core_common_contract,
    common_contract_equality_key,
    validate_common_contract,
)
from tools.benchmark.manifest import generated_bytes, load_manifest
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
    snapshot = load_snapshot(
        equivalent_source(format, 4),
        options=default_options(format),
    )

    _assert_all_preimages_match(snapshot, monkeypatch)


def test_import_diamond_matches_authoritative_document_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, imports = import_diamond()
    snapshot = load_snapshot(
        source,
        options=LoadOptions(
            format=DocumentFormat.FUNCTIONAL,
            imports=ImportPolicy.RESOLVE_LOCAL,
            backend=BackendPreference.PYTHON,
        ),
        resolver=MappingResolver(cast(Mapping[IRI | str, MappingTarget], imports)),
    )

    assert len(snapshot.documents) == 4
    _assert_all_preimages_match(snapshot, monkeypatch)


def test_annotated_swrl_logical_preimage_matches_authoritative_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    options = LoadOptions(
        format=DocumentFormat.FUNCTIONAL,
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
    )
    document = PythonParser().parse(
        b"Prefix(:=<urn:test#>) Ontology(<urn:rule> "
        b"SWRLRule(Annotation(:p :note) "
        b"(ClassAtom(:A Variable(:x)))"
        b"(ClassAtom(:B Variable(:x)))))",
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
