from __future__ import annotations

import hashlib
from dataclasses import fields

from pyowl_core import (
    IRI,
    LOGICAL_AXIOM_TYPES,
    Annotation,
    AnnotationProperty,
    AxiomScope,
    BackendPreference,
    CanonicalSet,
    Class,
    Declaration,
    Entity,
    ImportPolicy,
    LoadOptions,
    OntologyDelta,
    PythonParser,
    StructuralContextKind,
    StructuralNode,
    SubClassOf,
    apply_delta,
    canonical_bytes,
    compose_views,
    encode_varint,
    load_snapshot,
)


def _frame(value: bytes) -> bytes:
    return encode_varint(len(value)) + value


def _collection(values) -> bytes:  # type: ignore[no-untyped-def]
    encoded = tuple(canonical_bytes(item) for item in values)
    return encode_varint(len(encoded)) + b"".join(_frame(item) for item in encoded)


def _fingerprint_bytes(value) -> bytes:  # type: ignore[no-untyped-def]
    return _frame(value.algorithm.encode("ascii")) + encode_varint(value.schema) + value.digest


def _independent_document(document) -> bytes:  # type: ignore[no-untyped-def]
    pieces = [b"pyowl-core:document-fingerprint:v1\x00"]
    for iri in (document.ontology_id.ontology_iri, document.ontology_id.version_iri):
        pieces.append(b"0" if iri is None else b"1" + _frame(canonical_bytes(iri)))
    for values in (
        document.direct_imports,
        document.ontology_annotations,
        document.axioms,
        document.extension_components,
    ):
        pieces.append(_collection(values))
    return hashlib.sha256(b"".join(pieces)).digest()


def _independent_snapshot(snapshot) -> bytes:  # type: ignore[no-untyped-def]
    pieces = [
        b"pyowl-core:snapshot-structural:v1\x00",
        _frame(snapshot.import_manifest.canonical_bytes()),
    ]
    for record in snapshot.import_manifest.documents:
        key = record.document_key
        pieces.append(_frame(key.encode("ascii")))
        pieces.append(
            _collection(snapshot.ontology_annotations(scope=AxiomScope.DOCUMENT, document_key=key))
        )
        pieces.append(
            _collection(snapshot.iter_axioms(scope=AxiomScope.DOCUMENT, document_key=key))
        )
        pieces.append(
            _collection(snapshot.iter_extensions(scope=AxiomScope.DOCUMENT, document_key=key))
        )
    return hashlib.sha256(b"".join(pieces)).digest()


def _context_bytes(context) -> bytes:  # type: ignore[no-untyped-def]
    fingerprints = context.fingerprints
    pieces = [
        b"pyowl-core:view-structure-context:v1\x00",
        _frame(context.kind.value.encode("ascii")),
        encode_varint(len(fingerprints)),
    ]
    pieces.extend(_frame(_fingerprint_bytes(item)) for item in fingerprints)
    return b"".join(pieces)


def _independent_effective(view) -> bytes:  # type: ignore[no-untyped-def]
    context = view.structural_context
    domain = {
        StructuralContextKind.OVERLAY: b"pyowl-core:overlay-structural:v1\x00",
        StructuralContextKind.COMPOSITE: b"pyowl-core:composite-structural:v1\x00",
    }[context.kind]
    pieces = [domain, _frame(_context_bytes(context))]
    pieces.append(_collection(view.ontology_annotations()))
    pieces.append(_collection(view.iter_axioms()))
    pieces.append(_collection(view.iter_extensions()))
    return hashlib.sha256(b"".join(pieces)).digest()


def _without_annotations(value: StructuralNode) -> StructuralNode:
    if not hasattr(value, "annotations") or not value.annotations:
        return value
    values = {item.name: getattr(value, item.name) for item in fields(value)}
    values["annotations"] = CanonicalSet()
    return type(value)(**values)


def _independent_logical(view) -> bytes:  # type: ignore[no-untyped-def]
    axioms = sorted(
        {
            canonical_bytes(_without_annotations(item))
            for item in view.iter_axioms()
            if isinstance(item, LOGICAL_AXIOM_TYPES)
        }
    )
    extensions = sorted(
        {canonical_bytes(_without_annotations(item)) for item in view.iter_extensions()}
    )
    pieces = [
        b"pyowl-core:snapshot-logical:v1\x00",
        b"datatype-policy:owl2-v1\x00",
        encode_varint(len(axioms)),
        *(_frame(item) for item in axioms),
        encode_varint(len(extensions)),
        *(b"E" + _frame(item) for item in extensions),
    ]
    return hashlib.sha256(b"".join(pieces)).digest()


def _independent_signature(view) -> bytes:  # type: ignore[no-untyped-def]
    values = tuple(view.signature())
    pieces = [
        b"pyowl-core:snapshot-signature:v1\x00",
        b"\x01",
        encode_varint(len(values)),
        *(_frame(canonical_bytes(item)) for item in values),
    ]
    return hashlib.sha256(b"".join(pieces)).digest()


def _snapshot(identity: str, axioms: str):  # type: ignore[no-untyped-def]
    return load_snapshot(
        f"Prefix(:=<urn:test#>) Ontology(<urn:{identity}> {axioms})".encode(),
        options=LoadOptions(
            imports=ImportPolicy.IGNORE,
            backend=BackendPreference.PYTHON,
        ),
    )


def _rule_snapshot(*, annotated: bool):  # type: ignore[no-untyped-def]
    options = LoadOptions(
        imports=ImportPolicy.IGNORE,
        backend=BackendPreference.PYTHON,
    )
    annotation = "Annotation(:p :note) " if annotated else ""
    document = PythonParser().parse(
        (
            "Prefix(:=<urn:test#>) Ontology(<urn:rule> "
            f"SWRLRule({annotation}(ClassAtom(:A Variable(:x)))"
            "(ClassAtom(:B Variable(:x)))))"
        ).encode(),
        options=options,
        allow_swrl=True,
    )
    return load_snapshot(document, options=options)


def test_document_and_snapshot_domains_match_independent_encoder() -> None:
    snapshot = _snapshot(
        "root",
        "Declaration(Class(:A)) SubClassOf(:A :B)",
    )
    document = snapshot.root

    assert document.document_fingerprint.digest == _independent_document(document)
    assert snapshot.structural_fingerprint.digest == _independent_snapshot(snapshot)
    assert snapshot.logical_fingerprint.digest == _independent_logical(snapshot)
    assert snapshot.signature_fingerprint.digest == _independent_signature(snapshot)


def test_overlay_and_composite_domains_match_independent_full_content_hashing() -> None:
    first = _snapshot("one", "Declaration(Class(:A)) SubClassOf(:A :B)")
    second = _snapshot("two", "Declaration(Class(:C))")
    overlay = apply_delta(
        first,
        OntologyDelta(add_axioms={Declaration(Class(IRI("urn:test#D")))}),
    )
    composite = compose_views(
        overlay,
        second,
        delta=OntologyDelta(
            add_axioms={SubClassOf(Class(IRI("urn:test#B")), Class(IRI("urn:test#C")))}
        ),
    )

    for view in (overlay, composite):
        assert view.structural_fingerprint.digest == _independent_effective(view)
        assert view.logical_fingerprint.digest == _independent_logical(view)
        assert view.signature_fingerprint.digest == _independent_signature(view)


def test_fingerprint_domains_separate_structure_logic_and_signature() -> None:
    base = _snapshot("root", "SubClassOf(:A :B)")
    declaration = Declaration(Class(IRI("urn:test#C")))
    declared = apply_delta(base, OntologyDelta(add_axioms={declaration}))
    logical = apply_delta(
        base,
        OntologyDelta(add_axioms={SubClassOf(Class(IRI("urn:test#B")), Class(IRI("urn:test#C")))}),
    )

    assert declared.structural_fingerprint != base.structural_fingerprint
    assert declared.logical_fingerprint == base.logical_fingerprint
    assert declared.signature_fingerprint != base.signature_fingerprint
    assert logical.structural_fingerprint != base.structural_fingerprint
    assert logical.logical_fingerprint != base.logical_fingerprint
    assert logical.signature_fingerprint != base.signature_fingerprint


def test_axiom_annotations_change_structure_but_not_logical_content() -> None:
    base = _snapshot(
        "root",
        "Declaration(AnnotationProperty(:p)) SubClassOf(:A :B)",
    )
    plain = SubClassOf(Class(IRI("urn:test#A")), Class(IRI("urn:test#B")))
    annotated = SubClassOf(
        plain.sub_class,
        plain.super_class,
        CanonicalSet(
            (
                Annotation(
                    AnnotationProperty(IRI("urn:test#p")),
                    IRI("urn:test#note"),
                ),
            )
        ),
    )
    changed = apply_delta(
        base,
        OntologyDelta(add_axioms={annotated}, remove_axioms={plain}),
    )

    assert changed.structural_fingerprint != base.structural_fingerprint
    assert changed.logical_fingerprint == base.logical_fingerprint
    assert changed.signature_fingerprint == base.signature_fingerprint


def test_rule_annotations_are_structural_while_rule_content_is_logical() -> None:
    plain = _rule_snapshot(annotated=False)
    annotated = _rule_snapshot(annotated=True)
    composite = compose_views(plain, annotated)

    assert plain.structural_fingerprint != annotated.structural_fingerprint
    assert plain.logical_fingerprint == annotated.logical_fingerprint
    assert len(tuple(composite.iter_extensions())) == 2
    assert composite.logical_fingerprint == plain.logical_fingerprint
    materialized = composite.materialize()
    assert tuple(materialized.iter_extensions()) == tuple(composite.iter_extensions())
    assert materialized.structural_fingerprint == composite.structural_fingerprint
    assert materialized.logical_fingerprint == composite.logical_fingerprint


def test_signature_is_entity_kind_plus_iri_not_iri_alone() -> None:
    class_entity = Class(IRI("urn:test#Punned"))
    declaration = Declaration(class_entity)
    view = apply_delta(_snapshot("root", ""), OntologyDelta(add_axioms={declaration}))
    values = view.signature()
    assert values == (class_entity,)
    assert all(isinstance(item, Entity) for item in values)
