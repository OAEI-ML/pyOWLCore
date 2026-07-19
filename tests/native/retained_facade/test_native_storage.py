from __future__ import annotations

import copy
import pickle
from collections.abc import Callable
from dataclasses import replace
from typing import cast

import pytest

from pyowl_core._immutable import FrozenMap
from pyowl_core.backends import native_handoff_v2
from pyowl_core.backends.native_handoff import (
    NativeDocumentPublicationV1,
    NativeImportManifestPublicationV1,
    freeze_native_snapshot_publication_v1,
)
from pyowl_core.backends.native_handoff_v2 import (
    NativeDocumentHandleV2,
    NativeFacadePageV2,
    NativeSnapshotHandleV2,
)
from pyowl_core.document import OntologyDocument, OntologySnapshot
from pyowl_core.document.fingerprint import document_fingerprint_bytes
from pyowl_core.document.native_storage import (
    ontology_snapshot_from_native_publication_v2,
)
from pyowl_core.document.snapshot import AxiomScope
from pyowl_core.exceptions import (
    BackendProtocolError,
    ClosedSnapshotError,
    SnapshotInUseError,
)
from pyowl_core.model import (
    IRI,
    CanonicalSet,
    Class,
    Declaration,
    StructuralNode,
    canonical_bytes,
    structural_digest,
)

from ..publication_handoff._support import publication_fields
from ..publication_handoff._support_v2 import publication


def _snapshot() -> OntologySnapshot:
    return ontology_snapshot_from_native_publication_v2(publication())


def _fixture_axiom() -> Declaration:
    return Declaration(Class(IRI("urn:handoff:Class")))


def test_factory_and_zero_page_metadata_access_do_not_materialize_roots() -> None:
    published = publication()
    before = published.handle._facade_counters_v2()
    snapshot = ontology_snapshot_from_native_publication_v2(published)

    assert type(snapshot) is not OntologySnapshot
    assert isinstance(snapshot, OntologySnapshot)
    assert isinstance(snapshot.root, OntologyDocument)
    assert snapshot.root is snapshot.documents[0]
    assert snapshot.document(snapshot.root_document_key) is snapshot.root
    assert len(snapshot.root.ontology_annotations) == 0
    assert len(snapshot.root.axioms) == 1
    assert len(snapshot.root.extension_components) == 0
    assert len(snapshot.ontology_annotations()) == 0
    assert snapshot.root.rdf_mapping_report is None
    assert snapshot.owl2_dl_report is None
    assert "storage='native'" in repr(snapshot)
    assert "storage='native'" in repr(snapshot.root)
    assert "<native" in repr(snapshot.root.axioms)

    after = published.handle._facade_counters_v2()
    assert after.page_requests == before.page_requests == 0
    assert after.rows_emitted == before.rows_emitted == 0
    counters = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert counters.publication_objects > 0
    assert counters.model_rows_materialized == 0
    assert counters.auxiliary_rows_decoded == 0
    assert counters.cache_current_entries == 0


def test_factory_closes_document_owner_prefix_when_a_later_fork_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = publication()
    first_metadata = published.documents[0]
    object.__setattr__(published, "documents", (first_metadata, first_metadata))
    created: list[NativeDocumentHandleV2] = []
    close_calls = 0
    original_document = NativeSnapshotHandleV2._facade_document_v2
    original_close = NativeDocumentHandleV2.close

    def tracked_document(
        handle: NativeSnapshotHandleV2,
        document_ordinal: int,
    ) -> NativeDocumentHandleV2:
        document = original_document(handle, document_ordinal)
        created.append(document)
        return document

    def tracked_close(handle: NativeDocumentHandleV2) -> None:
        nonlocal close_calls
        if created and handle is created[0]:
            close_calls += 1
        original_close(handle)

    monkeypatch.setattr(NativeSnapshotHandleV2, "_facade_document_v2", tracked_document)
    monkeypatch.setattr(NativeDocumentHandleV2, "close", tracked_close)

    with pytest.raises(BackendProtocolError, match="ordinal"):
        ontology_snapshot_from_native_publication_v2(published)

    assert len(created) == 1
    assert created[0].closed
    assert close_calls == 1


def test_canonical_set_preserves_order_equality_hash_and_exact_page_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _snapshot()
    native_values = snapshot.root.axioms
    expected_axiom = _fixture_axiom()
    expected = CanonicalSet((expected_axiom,))
    calls = 0
    original = NativeFacadePageV2._validated_rows_v2

    def counted(page: NativeFacadePageV2) -> tuple[object, ...]:
        nonlocal calls
        calls += 1
        return original(page)

    monkeypatch.setattr(NativeFacadePageV2, "_validated_rows_v2", counted)

    assert hash(native_values) == hash(expected)
    assert calls == 1
    assert snapshot._native_python_counters().model_rows_materialized == 0  # type: ignore[attr-defined]

    first = native_values.as_tuple()
    assert first == (expected_axiom,)
    assert calls == 2
    first_counters = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert first_counters.model_rows_materialized == 1
    assert first_counters.cache_misses == 1

    second = native_values.as_tuple()
    assert second == first
    assert second[0] is first[0]
    assert calls == 3
    second_counters = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert second_counters.model_rows_materialized == 1
    assert second_counters.cache_hits == 1
    assert native_values == expected
    assert expected == native_values


def test_canonical_set_abc_algebra_returns_ordinary_canonical_values() -> None:
    snapshot = _snapshot()
    native_values = snapshot.root.axioms
    expected = CanonicalSet((_fixture_axiom(),))
    empty: CanonicalSet[Declaration] = CanonicalSet()

    assert native_values <= expected
    assert native_values >= expected
    assert not native_values < expected
    assert not native_values > expected
    assert not native_values.isdisjoint(expected)
    assert native_values.isdisjoint(empty)
    assert native_values | empty == expected
    assert empty | native_values == expected
    assert native_values & expected == expected
    assert native_values - expected == empty
    assert native_values ^ expected == empty
    assert type(native_values | empty) is CanonicalSet
    with pytest.raises(TypeError, match="structural nodes"):
        _ = native_values | {object()}

    snapshot.root.close()  # type: ignore[attr-defined]
    with pytest.raises(ClosedSnapshotError):
        _ = native_values | empty


def test_snapshot_scopes_contains_and_origin_queries_use_exact_native_roles() -> None:
    snapshot = _snapshot()
    axiom = _fixture_axiom()
    absent = Declaration(Class(IRI("urn:handoff:absent")))
    document_key = snapshot.root_document_key

    assert tuple(snapshot.iter_axioms(scope=AxiomScope.ROOT)) == (axiom,)
    assert tuple(
        snapshot.iter_axioms(
            scope=AxiomScope.DOCUMENT,
            document_key=document_key,
        )
    ) == (axiom,)
    assert tuple(snapshot.iter_axioms(scope=AxiomScope.CLOSURE)) == (axiom,)
    assert snapshot.contains(axiom, scope=AxiomScope.ROOT)
    assert snapshot.contains(
        axiom,
        scope=AxiomScope.DOCUMENT,
        document_key=document_key,
    )
    assert snapshot.contains(axiom, scope=AxiomScope.CLOSURE)
    assert not snapshot.contains(absent)

    digest = structural_digest(axiom)
    expected_origin = snapshot.origin_index.entries[digest]
    assert snapshot.origin_index.origins_for(axiom) == expected_origin
    assert snapshot.root.origin_index is not None
    assert snapshot.root.origin_index.origins_for(axiom) == expected_origin
    assert hash(snapshot.origin_index.entries) == hash(
        FrozenMap(dict(snapshot.origin_index.entries.items()))
    )

    native = snapshot._native_python_counters()  # type: ignore[attr-defined]
    assert native.model_rows_materialized == 1
    assert native.auxiliary_rows_decoded >= 1


def test_native_contains_decodes_once_at_the_bound_owner_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = publication()
    snapshot = ontology_snapshot_from_native_publication_v2(published)
    original_decode = cast(
        Callable[..., object],
        vars(native_handoff_v2)["decode_canonical"],
    )
    decode_calls = 0

    def counted_decode(*args: object, **kwargs: object) -> object:
        nonlocal decode_calls
        decode_calls += 1
        return original_decode(*args, **kwargs)

    monkeypatch.setattr(native_handoff_v2, "decode_canonical", counted_decode)
    assert snapshot.contains(_fixture_axiom(), scope=AxiomScope.CLOSURE)
    assert decode_calls == 1


def test_oversized_valid_absent_axiom_is_rejected_locally_with_close_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pyowl_core.document import native_storage

    published = publication()
    snapshot = ontology_snapshot_from_native_publication_v2(published)
    document = snapshot.root
    oversized = Declaration(
        Class(IRI("urn:oversized:" + "x" * (published.max_facade_row_bytes + 1)))
    )
    assert len(canonical_bytes(oversized)) > published.max_facade_row_bytes
    original = canonical_bytes
    encode_calls = 0

    def counted(value: StructuralNode) -> bytes:
        nonlocal encode_calls
        encode_calls += 1
        return original(value)

    monkeypatch.setattr(native_storage, "canonical_bytes", counted)

    assert oversized not in document.axioms
    assert not snapshot.contains(oversized, scope=AxiomScope.ROOT)
    assert not snapshot.contains(oversized, scope=AxiomScope.CLOSURE)
    assert encode_calls == 3
    assert published.handle._facade_counters_v2().contains_requests == 0

    document.close()  # type: ignore[attr-defined]
    with pytest.raises(ClosedSnapshotError):
        _ = oversized in document.axioms
    assert encode_calls == 3

    snapshot.close()  # type: ignore[attr-defined]
    for scope in (AxiomScope.ROOT, AxiomScope.CLOSURE):
        with pytest.raises(ClosedSnapshotError):
            snapshot.contains(oversized, scope=scope)
    assert encode_calls == 3


def test_document_and_snapshot_owners_close_independently_and_cache_cannot_bypass_close() -> None:
    snapshot = _snapshot()
    document = snapshot.root
    axiom = document.axioms.as_tuple()[0]

    document.close()  # type: ignore[attr-defined]
    document.close()  # type: ignore[attr-defined]
    assert document.closed  # type: ignore[attr-defined]
    assert not snapshot.closed  # type: ignore[attr-defined]
    assert document.ontology_id.ontology_iri == IRI("urn:handoff:ontology")
    assert "state='closed'" in repr(document)
    with pytest.raises(ClosedSnapshotError):
        len(document.axioms)
    with pytest.raises(ClosedSnapshotError):
        document.axioms.as_tuple()
    with pytest.raises(ClosedSnapshotError):
        hash(document.axioms)
    with pytest.raises(ClosedSnapshotError):
        _ = axiom in document.axioms
    assert snapshot.contains(axiom)

    snapshot.close()  # type: ignore[attr-defined]
    snapshot.close()  # type: ignore[attr-defined]
    assert snapshot.closed  # type: ignore[attr-defined]
    assert snapshot.report.effective_axiom_count == 1
    assert "state='closed'" in repr(snapshot)
    with pytest.raises(ClosedSnapshotError):
        tuple(snapshot.iter_axioms())
    with pytest.raises(ClosedSnapshotError):
        snapshot.contains(axiom)


def test_snapshot_close_leaves_forked_document_owner_open() -> None:
    snapshot = _snapshot()
    document = snapshot.root
    snapshot.close()  # type: ignore[attr-defined]

    assert snapshot.closed  # type: ignore[attr-defined]
    assert not document.closed  # type: ignore[attr-defined]
    assert document.axioms.as_tuple() == (_fixture_axiom(),)
    document.close()  # type: ignore[attr-defined]


def test_snapshot_context_manager_closes_only_the_snapshot_owner() -> None:
    snapshot = _snapshot()
    document = snapshot.root

    with snapshot as entered:  # type: ignore[attr-defined]
        assert entered is snapshot
        assert tuple(entered.iter_axioms()) == (_fixture_axiom(),)

    assert snapshot.closed
    assert not document.closed  # type: ignore[attr-defined]
    assert tuple(document.axioms) == (_fixture_axiom(),)


def test_dependent_lease_blocks_close_until_released() -> None:
    snapshot = _snapshot()
    lease = snapshot._retain_dependent()  # type: ignore[attr-defined]

    with pytest.raises(SnapshotInUseError, match="dependent"):
        snapshot.close()  # type: ignore[attr-defined]
    lease.release()
    snapshot.close()  # type: ignore[attr-defined]
    assert snapshot.closed  # type: ignore[attr-defined]


def test_copy_deepcopy_pickle_and_dataclass_replace_lifecycle_contracts() -> None:
    snapshot = _snapshot()
    document = snapshot.root
    values = document.axioms

    for value in (snapshot, document, values):
        assert copy.copy(value) is value
        assert copy.deepcopy(value) is value
        with pytest.raises(TypeError, match="cannot be pickled"):
            pickle.dumps(value)

    with pytest.raises(TypeError, match="cannot be replaced"):
        replace(document, diagnostics=())
    with pytest.raises(TypeError, match="cannot be replaced"):
        replace(snapshot, diagnostics=())


def test_document_and_snapshot_match_materialized_python_values_symmetrically() -> None:
    initial = _snapshot()
    metadata = initial.root
    ordinary_document = OntologyDocument(
        metadata.ontology_id,
        metadata.document_iri,
        metadata.direct_imports,
        CanonicalSet(),
        CanonicalSet((_fixture_axiom(),)),
        CanonicalSet(),
        metadata.provenance,
        diagnostics=metadata.diagnostics,
    )
    values = publication_fields()
    published_documents = cast(
        tuple[NativeDocumentPublicationV1, ...],
        values["documents"],
    )
    published_manifest = cast(
        NativeImportManifestPublicationV1,
        values["import_manifest"],
    )
    fingerprint = ordinary_document.document_fingerprint
    values["documents"] = (replace(published_documents[0], document_fingerprint=fingerprint),)
    values["import_manifest"] = replace(
        published_manifest,
        documents=(
            replace(
                published_manifest.documents[0],
                document_fingerprint=fingerprint,
            ),
        ),
    )
    native_snapshot = ontology_snapshot_from_native_publication_v2(
        publication(
            values=values,
            preimages=(
                document_fingerprint_bytes(ordinary_document),
                b"structural",
                b"logical",
                b"signature",
            ),
        )
    )
    native_document = native_snapshot.root

    assert native_document == ordinary_document
    assert ordinary_document == native_document
    assert hash(native_document) == hash(ordinary_document)
    assert native_document.document_fingerprint == fingerprint

    ordinary_snapshot = OntologySnapshot(
        ordinary_document,
        (ordinary_document,),
        native_snapshot.import_manifest,
        native_snapshot.root_document_key,
        native_snapshot.load_options,
        diagnostics=native_snapshot.diagnostics,
        _preserve_document_scopes=True,
        _origin_index_override=native_snapshot.origin_index,
        _structural_fingerprint_override=native_snapshot.structural_fingerprint,
    )
    assert native_snapshot == ordinary_snapshot
    assert ordinary_snapshot == native_snapshot
    assert hash(native_snapshot) == hash(ordinary_snapshot)


def test_factory_rejects_legacy_v1_publication_instead_of_rebuilding() -> None:
    with pytest.raises(BackendProtocolError, match="V2 paged"):
        ontology_snapshot_from_native_publication_v2(
            freeze_native_snapshot_publication_v1(publication_fields())  # type: ignore[arg-type]
        )
