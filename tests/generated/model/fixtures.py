"""One valid structural fixture for every permanent model-schema constructor."""

from __future__ import annotations

import pyowl_core.extensions.swrl as swrl
import pyowl_core.model as m


def _iri(local: str) -> m.IRI:
    return m.IRI(f"https://example.org/wp01/{local}")


def _set(*values: m.StructuralNode) -> m.CanonicalSet[m.StructuralNode]:
    return m.CanonicalSet(values)


def typed_entity_fixtures() -> dict[m.EntityKind, m.Entity]:
    """Return one top-level fixture for every W3C typed-entity production."""

    iri = _iri("typed-entity")
    return {
        m.EntityKind.CLASS: m.Class(iri),
        m.EntityKind.DATATYPE: m.Datatype(iri),
        m.EntityKind.OBJECT_PROPERTY: m.ObjectProperty(iri),
        m.EntityKind.DATA_PROPERTY: m.DataProperty(iri),
        m.EntityKind.ANNOTATION_PROPERTY: m.AnnotationProperty(iri),
        m.EntityKind.NAMED_INDIVIDUAL: m.NamedIndividual(iri),
    }


def model_fixtures() -> dict[type[m.StructuralNode], m.StructuralNode]:
    """Build a fresh exhaustive fixture mapping keyed by registry constructor."""

    standalone_iri = _iri("standalone-iri")
    class_a = m.Class(_iri("ClassA"))
    class_b = m.Class(_iri("ClassB"))
    datatype_a = m.Datatype(_iri("DatatypeA"))
    datatype_b = m.Datatype(_iri("DatatypeB"))
    object_property_a = m.ObjectProperty(_iri("objectPropertyA"))
    object_property_b = m.ObjectProperty(_iri("objectPropertyB"))
    object_property_c = m.ObjectProperty(_iri("objectPropertyC"))
    data_property_a = m.DataProperty(_iri("dataPropertyA"))
    data_property_b = m.DataProperty(_iri("dataPropertyB"))
    annotation_property_a = m.AnnotationProperty(_iri("annotationPropertyA"))
    annotation_property_b = m.AnnotationProperty(_iri("annotationPropertyB"))
    named_a = m.NamedIndividual(_iri("namedA"))
    named_b = m.NamedIndividual(_iri("namedB"))
    anonymous_a = m.AnonymousIndividual(b"a" * 32, b"anonymous-a")
    anonymous_b = m.AnonymousIndividual(b"a" * 32, b"anonymous-b")
    literal_a = m.Literal("alpha", m.XSD_STRING)
    literal_b = m.Literal("beta", m.XSD_STRING)
    nested_annotation = m.Annotation(annotation_property_b, _iri("nested-value"))
    annotation = m.Annotation(
        annotation_property_a,
        literal_a,
        _set(nested_annotation),
    )
    annotations = _set(annotation)

    inverse = m.ObjectInverseOf(object_property_a)
    chain = m.ObjectPropertyChain((object_property_a, inverse, object_property_b))
    facet = m.FacetRestriction(_iri("facet"), literal_a)
    data_intersection = m.DataIntersectionOf(_set(datatype_a, datatype_b))
    data_union = m.DataUnionOf(_set(datatype_a, datatype_b))
    data_complement = m.DataComplementOf(datatype_a)
    data_one_of = m.DataOneOf(_set(literal_a, literal_b))
    datatype_restriction = m.DatatypeRestriction(datatype_a, _set(facet))

    object_intersection = m.ObjectIntersectionOf(_set(class_a, class_b))
    object_union = m.ObjectUnionOf(_set(class_a, class_b))
    object_complement = m.ObjectComplementOf(class_a)
    object_one_of = m.ObjectOneOf(_set(named_a, anonymous_a))
    object_some = m.ObjectSomeValuesFrom(object_property_a, class_a)
    object_all = m.ObjectAllValuesFrom(inverse, class_b)
    object_has_value = m.ObjectHasValue(object_property_a, anonymous_a)
    object_has_self = m.ObjectHasSelf(object_property_a)
    object_min = m.ObjectMinCardinality(0, object_property_a, class_a)
    object_max = m.ObjectMaxCardinality(2**80, inverse, class_b)
    object_exact = m.ObjectExactCardinality(7, object_property_b, class_a)
    data_some = m.DataSomeValuesFrom((data_property_a, data_property_b), datatype_a)
    data_all = m.DataAllValuesFrom((data_property_b, data_property_a), data_union)
    data_has_value = m.DataHasValue(data_property_a, literal_a)
    data_min = m.DataMinCardinality(0, data_property_a, datatype_a)
    data_max = m.DataMaxCardinality(2**80, data_property_b, data_complement)
    data_exact = m.DataExactCardinality(7, data_property_a, datatype_restriction)

    variable_a = swrl.Variable(_iri("variableA"))
    variable_b = swrl.Variable(_iri("variableB"))
    class_atom = swrl.ClassAtom(class_a, variable_a)
    data_range_atom = swrl.DataRangeAtom(datatype_a, variable_b)
    object_property_atom = swrl.ObjectPropertyAtom(
        object_property_a,
        variable_a,
        named_a,
    )
    data_property_atom = swrl.DataPropertyAtom(
        data_property_a,
        variable_a,
        literal_a,
    )
    built_in_atom = swrl.BuiltInAtom(_iri("builtin"), (variable_a, literal_a))
    same_atom = swrl.SameIndividualAtom(variable_a, named_a)
    different_atom = swrl.DifferentIndividualsAtom(variable_a, variable_b)
    rule = swrl.SWRLRule(
        _set(class_atom, data_range_atom, object_property_atom, built_in_atom),
        _set(data_property_atom, same_atom, different_atom),
        annotations,
    )

    return {
        m.IRI: standalone_iri,
        m.Entity: class_a,
        m.AnonymousIndividual: anonymous_b,
        m.Literal: literal_b,
        m.Annotation: annotation,
        m.ObjectInverseOf: inverse,
        m.ObjectPropertyChain: chain,
        m.FacetRestriction: facet,
        m.DataIntersectionOf: data_intersection,
        m.DataUnionOf: data_union,
        m.DataComplementOf: data_complement,
        m.DataOneOf: data_one_of,
        m.DatatypeRestriction: datatype_restriction,
        m.ObjectIntersectionOf: object_intersection,
        m.ObjectUnionOf: object_union,
        m.ObjectComplementOf: object_complement,
        m.ObjectOneOf: object_one_of,
        m.ObjectSomeValuesFrom: object_some,
        m.ObjectAllValuesFrom: object_all,
        m.ObjectHasValue: object_has_value,
        m.ObjectHasSelf: object_has_self,
        m.ObjectMinCardinality: object_min,
        m.ObjectMaxCardinality: object_max,
        m.ObjectExactCardinality: object_exact,
        m.DataSomeValuesFrom: data_some,
        m.DataAllValuesFrom: data_all,
        m.DataHasValue: data_has_value,
        m.DataMinCardinality: data_min,
        m.DataMaxCardinality: data_max,
        m.DataExactCardinality: data_exact,
        m.Declaration: m.Declaration(class_a, annotations),
        m.SubClassOf: m.SubClassOf(class_a, object_some, annotations),
        m.EquivalentClasses: m.EquivalentClasses(_set(class_a, object_union), annotations),
        m.DisjointClasses: m.DisjointClasses(_set(class_a, object_complement), annotations),
        m.DisjointUnion: m.DisjointUnion(class_a, _set(class_b, object_some), annotations),
        m.SubObjectPropertyOf: m.SubObjectPropertyOf(chain, object_property_c, annotations),
        m.EquivalentObjectProperties: m.EquivalentObjectProperties(
            _set(object_property_a, inverse), annotations
        ),
        m.DisjointObjectProperties: m.DisjointObjectProperties(
            _set(object_property_a, object_property_b), annotations
        ),
        m.InverseObjectProperties: m.InverseObjectProperties(
            object_property_a, object_property_b, annotations
        ),
        m.ObjectPropertyDomain: m.ObjectPropertyDomain(
            object_property_a, object_intersection, annotations
        ),
        m.ObjectPropertyRange: m.ObjectPropertyRange(inverse, class_b, annotations),
        m.FunctionalObjectProperty: m.FunctionalObjectProperty(object_property_a, annotations),
        m.InverseFunctionalObjectProperty: m.InverseFunctionalObjectProperty(
            object_property_a, annotations
        ),
        m.ReflexiveObjectProperty: m.ReflexiveObjectProperty(object_property_a, annotations),
        m.IrreflexiveObjectProperty: m.IrreflexiveObjectProperty(object_property_a, annotations),
        m.SymmetricObjectProperty: m.SymmetricObjectProperty(object_property_a, annotations),
        m.AsymmetricObjectProperty: m.AsymmetricObjectProperty(object_property_a, annotations),
        m.TransitiveObjectProperty: m.TransitiveObjectProperty(object_property_a, annotations),
        m.SubDataPropertyOf: m.SubDataPropertyOf(data_property_a, data_property_b, annotations),
        m.EquivalentDataProperties: m.EquivalentDataProperties(
            _set(data_property_a, data_property_b), annotations
        ),
        m.DisjointDataProperties: m.DisjointDataProperties(
            _set(data_property_a, data_property_b), annotations
        ),
        m.DataPropertyDomain: m.DataPropertyDomain(data_property_a, class_a, annotations),
        m.DataPropertyRange: m.DataPropertyRange(data_property_a, data_intersection, annotations),
        m.FunctionalDataProperty: m.FunctionalDataProperty(data_property_a, annotations),
        m.DatatypeDefinition: m.DatatypeDefinition(datatype_b, datatype_restriction, annotations),
        m.HasKey: m.HasKey(
            class_a,
            _set(object_property_a, inverse),
            _set(data_property_a),
            annotations,
        ),
        m.SameIndividual: m.SameIndividual(_set(named_a, anonymous_a), annotations),
        m.DifferentIndividuals: m.DifferentIndividuals(_set(named_a, named_b), annotations),
        m.ClassAssertion: m.ClassAssertion(object_intersection, named_a, annotations),
        m.ObjectPropertyAssertion: m.ObjectPropertyAssertion(
            object_property_a, named_a, anonymous_a, annotations
        ),
        m.NegativeObjectPropertyAssertion: m.NegativeObjectPropertyAssertion(
            inverse, anonymous_a, named_b, annotations
        ),
        m.DataPropertyAssertion: m.DataPropertyAssertion(
            data_property_a, named_a, literal_a, annotations
        ),
        m.NegativeDataPropertyAssertion: m.NegativeDataPropertyAssertion(
            data_property_b, anonymous_a, literal_b, annotations
        ),
        m.AnnotationAssertion: m.AnnotationAssertion(
            annotation_property_a, _iri("subject"), anonymous_a, annotations
        ),
        m.SubAnnotationPropertyOf: m.SubAnnotationPropertyOf(
            annotation_property_a, annotation_property_b, annotations
        ),
        m.AnnotationPropertyDomain: m.AnnotationPropertyDomain(
            annotation_property_a, _iri("annotation-domain"), annotations
        ),
        m.AnnotationPropertyRange: m.AnnotationPropertyRange(
            annotation_property_a, _iri("annotation-range"), annotations
        ),
        swrl.Variable: variable_a,
        swrl.ClassAtom: class_atom,
        swrl.DataRangeAtom: data_range_atom,
        swrl.ObjectPropertyAtom: object_property_atom,
        swrl.DataPropertyAtom: data_property_atom,
        swrl.BuiltInAtom: built_in_atom,
        swrl.SameIndividualAtom: same_atom,
        swrl.DifferentIndividualsAtom: different_atom,
        swrl.SWRLRule: rule,
    }


__all__ = ["model_fixtures", "typed_entity_fixtures"]
