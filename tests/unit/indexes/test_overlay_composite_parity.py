from __future__ import annotations

from pyowl_core import (
    IRI,
    AnnotationAssertionIndex,
    AssertedClassHierarchyView,
    AssertedPropertyHierarchyView,
    AxiomTypeIndex,
    CanonicalSet,
    Class,
    DeclarationIndex,
    EntityReferenceIndex,
    ExpressionOccurrenceIndex,
    OntologyDelta,
    PropertyDomainRangeView,
    SignatureView,
    SubClassOf,
    apply_delta,
    canonical_bytes,
    compose_views,
)

from .conftest import snapshot

_VIEW_TYPES = (
    SignatureView,
    AxiomTypeIndex,
    EntityReferenceIndex,
    DeclarationIndex,
    AnnotationAssertionIndex,
    AssertedClassHierarchyView,
    AssertedPropertyHierarchyView,
    PropertyDomainRangeView,
    ExpressionOccurrenceIndex,
)


def _summary(ontology, view_type):  # type: ignore[no-untyped-def]
    index = ontology.view(view_type)
    if view_type is SignatureView:
        return tuple(canonical_bytes(value) for value in index.iter())
    if view_type is AxiomTypeIndex:
        return tuple(canonical_bytes(value) for value in index.iter_all())
    if view_type is EntityReferenceIndex:
        return tuple(
            (
                canonical_bytes(key),
                tuple(
                    (
                        canonical_bytes(value.container),
                        value.constructor_path,
                        value.role,
                        len(value.origins),
                    )
                    for value in index.iter(key)
                ),
            )
            for key in index
        )
    if view_type is DeclarationIndex:
        return tuple(
            (
                canonical_bytes(entity),
                tuple(canonical_bytes(value) for value in index.declarations(entity)),
            )
            for entity in index.entities()
        )
    if view_type is AnnotationAssertionIndex:
        return tuple(
            (
                canonical_bytes(subject),
                tuple(canonical_bytes(value) for value in index.assertions(subject)),
            )
            for subject in index.subjects()
        )
    if view_type is AssertedClassHierarchyView:
        return tuple(
            (
                canonical_bytes(edge.axiom),
                repr(edge.child),
                repr(edge.parent),
                len(edge.origins),
            )
            for edge in index.iter_edges()
        )
    if view_type is AssertedPropertyHierarchyView:
        return (
            tuple(canonical_bytes(edge.axiom) for edge in index.iter_edges()),
            tuple(canonical_bytes(value.axiom) for value in index.chains()),
            tuple(canonical_bytes(value.axiom) for value in index.inverses()),
        )
    if view_type is PropertyDomainRangeView:
        return tuple(
            (
                canonical_bytes(property),
                tuple(
                    (value.kind, canonical_bytes(value.value), len(value.origins))
                    for value in index.iter(property)
                ),
            )
            for property in index.properties()
        )
    if view_type is ExpressionOccurrenceIndex:
        return tuple(
            (
                canonical_bytes(expression),
                tuple(
                    (
                        canonical_bytes(value.container),
                        value.constructor_path,
                        value.role,
                        len(value.origins),
                    )
                    for value in index.iter(expression)
                ),
            )
            for expression in index.expressions()
        )
    raise AssertionError(view_type)


def _rich(identity: str, prefix: str):
    return snapshot(
        f"Declaration(Class(:{prefix}A))",
        f"Declaration(Class(:{prefix}B))",
        f"Declaration(ObjectProperty(:{prefix}p))",
        f"SubClassOf(:{prefix}A ObjectSomeValuesFrom(:{prefix}p :{prefix}B))",
        f"EquivalentClasses(:{prefix}A :{prefix}B)",
        f"ObjectPropertyDomain(:{prefix}p :{prefix}A)",
        f"ObjectPropertyRange(:{prefix}p :{prefix}B)",
        f'AnnotationAssertion(rdfs:label :{prefix}A "{prefix}"@en)',
        ontology_iri=f"urn:index:{identity}",
    )


def test_overlay_indexes_equal_independent_materialized_views() -> None:
    base = _rich("base", "S")
    removed = next(base.iter_axioms(SubClassOf))
    added = SubClassOf(
        Class(IRI("urn:index#SB")),
        Class(IRI("urn:index#SC")),
    )
    overlay = apply_delta(
        base,
        OntologyDelta(
            add_axioms=CanonicalSet((added,)),
            remove_axioms=CanonicalSet((removed,)),
        ),
    )
    materialized = overlay.materialize()
    for view_type in _VIEW_TYPES:
        assert _summary(overlay, view_type) == _summary(materialized, view_type)


def test_composite_indexes_equal_independent_materialized_views() -> None:
    source = _rich("source", "S")
    target = _rich("target", "T")
    bridge = SubClassOf(
        Class(IRI("urn:index#SB")),
        Class(IRI("urn:index#TA")),
    )
    composite = compose_views(
        source,
        target,
        delta=OntologyDelta(add_axioms=CanonicalSet((bridge,))),
        roles=("source", "target"),
    )
    materialized = composite.materialize()
    for view_type in _VIEW_TYPES:
        assert _summary(composite, view_type) == _summary(materialized, view_type)
