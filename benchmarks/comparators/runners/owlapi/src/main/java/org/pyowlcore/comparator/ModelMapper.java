package org.pyowlcore.comparator;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Comparator;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.NavigableSet;
import java.util.Set;
import java.util.TreeSet;
import java.util.function.Function;
import java.util.stream.Collectors;
import java.util.stream.Stream;

import org.semanticweb.owlapi.model.*;

/** Maps OWLAPI objects to the pyowl-core model-schema-v1 canonical encoding. */
final class ModelMapper {
    private static final Comparator<byte[]> UNSIGNED_BYTES = Arrays::compareUnsigned;
    private static final String RDF_PLAIN_LITERAL =
            "http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral";

    static final class IneligibleException extends RuntimeException {
        IneligibleException(String message) {
            super(message);
        }
    }

    static final class MappedAxiom {
        final byte[] value;
        final byte[] logical;

        MappedAxiom(byte[] value, byte[] logical) {
            this.value = value;
            this.logical = logical;
        }
    }

    static final class MappedDocument {
        final String ontologyIri;
        final String versionIri;
        final List<byte[]> imports;
        final List<byte[]> annotations;
        final List<MappedAxiom> axioms;
        final List<MappedAxiom> extensions;
        final List<byte[]> signature;
        final List<byte[]> provenanceRoots;

        MappedDocument(
                String ontologyIri,
                String versionIri,
                List<byte[]> imports,
                List<byte[]> annotations,
                List<MappedAxiom> axioms,
                List<MappedAxiom> extensions,
                List<byte[]> signature,
                List<byte[]> provenanceRoots) {
            this.ontologyIri = ontologyIri;
            this.versionIri = versionIri;
            this.imports = imports;
            this.annotations = annotations;
            this.axioms = axioms;
            this.extensions = extensions;
            this.signature = signature;
            this.provenanceRoots = provenanceRoots;
        }
    }

    private static final class SourceAxiom {
        final OWLAxiom source;
        final MappedAxiom mapped;

        SourceAxiom(OWLAxiom source, MappedAxiom mapped) {
            this.source = source;
            this.mapped = mapped;
        }
    }

    private static final class EquivalenceComponent<T> {
        final Set<T> members = new LinkedHashSet<>();
        final Set<OWLAnnotation> annotations = new LinkedHashSet<>();

        EquivalenceComponent(
                Collection<? extends T> members,
                Collection<OWLAnnotation> annotations) {
            this.members.addAll(members);
            this.annotations.addAll(annotations);
        }

        boolean overlaps(Collection<? extends T> values) {
            for (T value : values) {
                if (members.contains(value)) {
                    return true;
                }
            }
            return false;
        }

        void merge(EquivalenceComponent<T> other) {
            members.addAll(other.members);
            annotations.addAll(other.annotations);
        }
    }

    private final NavigableSet<byte[]> signature = new TreeSet<>(UNSIGNED_BYTES);

    MappedDocument map(OWLOntology ontology, String format) {
        if (!List.of("functional", "owlxml", "rdfxml", "turtle").contains(format)) {
            throw new IllegalArgumentException("OWLAPI provenance format is unsupported");
        }
        if (ontology.anonymousIndividuals().findAny().isPresent()) {
            throw new IneligibleException(
                    "OWLAPI anonymous-individual labels cannot be proven parser-independent");
        }
        OWLOntologyID id = ontology.getOntologyID();
        String ontologyIri = id.getOntologyIRI().map(IRI::toString).orElse(null);
        String versionIri = id.getVersionIRI().map(IRI::toString).orElse(null);
        if (versionIri != null && ontologyIri == null) {
            throw new IneligibleException("version IRI has no ontology IRI");
        }

        List<byte[]> imports = ontology.importsDeclarations()
                .map(value -> Canonical.iri(value.getIRI().toString()))
                .collect(Collectors.toCollection(ArrayList::new));
        List<byte[]> annotations = ontology.annotations()
                .map(this::annotation)
                .collect(Collectors.toCollection(ArrayList::new));
        List<MappedAxiom> axioms = new ArrayList<>();
        List<MappedAxiom> extensions = new ArrayList<>();
        List<SourceAxiom> sourceAxioms = new ArrayList<>();
        List<OWLAxiom> ontologyAxioms =
                ontology.axioms().collect(Collectors.toCollection(ArrayList::new));
        if (isRdfGraph(format)) {
            ontologyAxioms = coalesceRdfEquivalenceAxioms(
                    ontologyAxioms, ontology.getOWLOntologyManager().getOWLDataFactory());
        }
        ontologyAxioms.forEach(axiom -> {
            byte[] mapped = axiom(axiom, true);
            byte[] logical = axiom.isLogicalAxiom() ? axiom(axiom, false) : null;
            MappedAxiom row = new MappedAxiom(mapped, logical);
            sourceAxioms.add(new SourceAxiom(axiom, row));
            if (axiom instanceof SWRLRule) {
                extensions.add(row);
            } else {
                axioms.add(row);
            }
        });

        imports = Canonical.normalizeSet(imports);
        annotations = Canonical.normalizeSet(annotations);
        axioms.sort((left, right) -> UNSIGNED_BYTES.compare(left.value, right.value));
        deduplicate(axioms);
        extensions.sort((left, right) -> UNSIGNED_BYTES.compare(left.value, right.value));
        deduplicate(extensions);
        List<byte[]> provenanceRoots = provenanceRoots(format, annotations, sourceAxioms,
                axioms, extensions);
        return new MappedDocument(
                ontologyIri,
                versionIri,
                imports,
                annotations,
                axioms,
                extensions,
                new ArrayList<>(signature),
                provenanceRoots);
    }

    private static boolean isRdfGraph(String format) {
        return "rdfxml".equals(format) || "turtle".equals(format);
    }

    private static List<OWLAxiom> coalesceRdfEquivalenceAxioms(
            List<OWLAxiom> axioms, OWLDataFactory dataFactory) {
        List<OWLAxiom> output = new ArrayList<>();
        List<EquivalenceComponent<OWLClassExpression>> classes = new ArrayList<>();
        List<EquivalenceComponent<OWLObjectPropertyExpression>> objectProperties =
                new ArrayList<>();
        List<EquivalenceComponent<OWLDataPropertyExpression>> dataProperties =
                new ArrayList<>();
        List<EquivalenceComponent<OWLIndividual>> individuals = new ArrayList<>();

        for (OWLAxiom value : axioms) {
            Set<OWLAnnotation> annotations = value.annotations()
                    .collect(Collectors.toCollection(LinkedHashSet::new));
            if (value instanceof OWLEquivalentClassesAxiom) {
                mergeComponent(
                        classes,
                        ((OWLEquivalentClassesAxiom) value).classExpressions()
                                .collect(Collectors.toCollection(LinkedHashSet::new)),
                        annotations);
            } else if (value instanceof OWLEquivalentObjectPropertiesAxiom) {
                mergeComponent(
                        objectProperties,
                        ((OWLEquivalentObjectPropertiesAxiom) value).properties()
                                .collect(Collectors.toCollection(LinkedHashSet::new)),
                        annotations);
            } else if (value instanceof OWLEquivalentDataPropertiesAxiom) {
                mergeComponent(
                        dataProperties,
                        ((OWLEquivalentDataPropertiesAxiom) value).properties()
                                .collect(Collectors.toCollection(LinkedHashSet::new)),
                        annotations);
            } else if (value instanceof OWLSameIndividualAxiom) {
                mergeComponent(
                        individuals,
                        ((OWLSameIndividualAxiom) value).individuals()
                                .collect(Collectors.toCollection(LinkedHashSet::new)),
                        annotations);
            } else {
                output.add(value);
            }
        }

        classes.forEach(component -> output.add(dataFactory.getOWLEquivalentClassesAxiom(
                component.members, component.annotations)));
        objectProperties.forEach(component ->
                output.add(dataFactory.getOWLEquivalentObjectPropertiesAxiom(
                        component.members, component.annotations)));
        dataProperties.forEach(component ->
                output.add(dataFactory.getOWLEquivalentDataPropertiesAxiom(
                        component.members, component.annotations)));
        individuals.forEach(component -> output.add(dataFactory.getOWLSameIndividualAxiom(
                component.members, component.annotations)));
        return output;
    }

    private static <T> void mergeComponent(
            List<EquivalenceComponent<T>> components,
            Collection<? extends T> members,
            Collection<OWLAnnotation> annotations) {
        EquivalenceComponent<T> merged = new EquivalenceComponent<>(members, annotations);
        for (int index = components.size() - 1; index >= 0; index--) {
            EquivalenceComponent<T> candidate = components.get(index);
            if (merged.overlaps(candidate.members)) {
                merged.merge(candidate);
                components.remove(index);
            }
        }
        components.add(merged);
    }

    private static List<byte[]> provenanceRoots(
            String format,
            List<byte[]> annotations,
            List<SourceAxiom> sourceAxioms,
            List<MappedAxiom> axioms,
            List<MappedAxiom> extensions) {
        if (!isRdfGraph(format)) {
            List<byte[]> roots = new ArrayList<>(annotations);
            axioms.forEach(value -> roots.add(value.value));
            extensions.forEach(value -> roots.add(value.value));
            return roots;
        }

        List<byte[]> roots = new ArrayList<>(sourceAxioms.size());
        for (SourceAxiom value : sourceAxioms) {
            if (value.source instanceof SWRLRule) {
                throw new IneligibleException(
                        "RDF SWRL common-contract mapping cannot be proven exact from OWLAPI");
            }
            roots.add(value.mapped.value);
        }
        return roots;
    }

    private static void deduplicate(List<MappedAxiom> values) {
        for (int index = values.size() - 1; index > 0; index--) {
            if (Arrays.equals(values.get(index - 1).value, values.get(index).value)) {
                values.remove(index);
            }
        }
    }

    private byte[] entity(String kind, OWLEntity value) {
        return entity(kind, value.getIRI().toString());
    }

    private byte[] entity(String kind, String iri) {
        byte[] mapped = Canonical.entity(kind, iri);
        signature.add(mapped);
        return mapped;
    }

    private byte[] entity(OWLEntity value) {
        if (value instanceof OWLClass) {
            return entity("class", value);
        }
        if (value instanceof OWLDatatype) {
            return entity("datatype", value);
        }
        if (value instanceof OWLObjectProperty) {
            return entity("object_property", value);
        }
        if (value instanceof OWLDataProperty) {
            return entity("data_property", value);
        }
        if (value instanceof OWLAnnotationProperty) {
            return entity("annotation_property", value);
        }
        if (value instanceof OWLNamedIndividual) {
            return entity("named_individual", value);
        }
        throw unsupported(value);
    }

    private byte[] individual(OWLIndividual value) {
        if (value.isAnonymous()) {
            throw new IneligibleException("anonymous individuals are not eligible for OWLAPI comparison");
        }
        return entity(value.asOWLNamedIndividual());
    }

    private byte[] literal(OWLLiteral value) {
        String language = value.hasLang() ? value.getLang().toLowerCase(java.util.Locale.ROOT) : null;
        String datatype = value.hasLang() ? RDF_PLAIN_LITERAL : value.getDatatype().getIRI().toString();
        return Canonical.node(
                Canonical.LITERAL,
                Canonical.text(value.getLiteral()),
                Canonical.nodeField(entity("datatype", datatype)),
                language == null ? Canonical.none() : Canonical.text(language));
    }

    private byte[] annotationValue(OWLAnnotationValue value) {
        if (value instanceof IRI) {
            return Canonical.iri(value.toString());
        }
        if (value instanceof OWLLiteral) {
            return literal((OWLLiteral) value);
        }
        if (value instanceof OWLAnonymousIndividual) {
            throw new IneligibleException("anonymous annotation values are not eligible");
        }
        throw unsupported(value);
    }

    private byte[] annotationSubject(OWLAnnotationSubject value) {
        if (value instanceof IRI) {
            return Canonical.iri(value.toString());
        }
        throw new IneligibleException("anonymous annotation subjects are not eligible");
    }

    private byte[] annotation(OWLAnnotation value) {
        return Canonical.node(
                Canonical.ANNOTATION,
                Canonical.nodeField(entity(value.getProperty())),
                Canonical.nodeField(annotationValue(value.getValue())),
                Canonical.set(map(value.annotations(), this::annotation)));
    }

    private Canonical.Field annotations(OWLAxiom axiom, boolean include) {
        return Canonical.set(include ? map(axiom.annotations(), this::annotation) : List.of());
    }

    private byte[] objectProperty(OWLObjectPropertyExpression value) {
        if (!value.isAnonymous()) {
            return entity(value.asOWLObjectProperty());
        }
        return Canonical.node(
                Canonical.OBJECT_INVERSE_OF,
                Canonical.nodeField(entity(value.getNamedProperty())));
    }

    private byte[] dataProperty(OWLDataPropertyExpression value) {
        if (value.isAnonymous()) {
            throw new IneligibleException("anonymous data properties are unsupported by OWL 2");
        }
        return entity(value.asOWLDataProperty());
    }

    private byte[] facet(OWLFacetRestriction value) {
        return Canonical.node(
                Canonical.FACET_RESTRICTION,
                Canonical.nodeField(Canonical.iri(value.getFacet().getIRI().toString())),
                Canonical.nodeField(literal(value.getFacetValue())));
    }

    private byte[] dataRange(OWLDataRange value) {
        if (value instanceof OWLDatatype) {
            return entity((OWLDatatype) value);
        }
        if (value instanceof OWLDataIntersectionOf) {
            List<byte[]> values = new ArrayList<>();
            collectDataOperands(value, true, values);
            return Canonical.node(Canonical.DATA_INTERSECTION_OF, Canonical.set(values));
        }
        if (value instanceof OWLDataUnionOf) {
            List<byte[]> values = new ArrayList<>();
            collectDataOperands(value, false, values);
            return Canonical.node(Canonical.DATA_UNION_OF, Canonical.set(values));
        }
        if (value instanceof OWLDataComplementOf) {
            return Canonical.node(
                    Canonical.DATA_COMPLEMENT_OF,
                    Canonical.nodeField(dataRange(((OWLDataComplementOf) value).getDataRange())));
        }
        if (value instanceof OWLDataOneOf) {
            return Canonical.node(
                    Canonical.DATA_ONE_OF,
                    Canonical.set(map(((OWLDataOneOf) value).values(), this::literal)));
        }
        if (value instanceof OWLDatatypeRestriction) {
            OWLDatatypeRestriction restriction = (OWLDatatypeRestriction) value;
            return Canonical.node(
                    Canonical.DATATYPE_RESTRICTION,
                    Canonical.nodeField(entity(restriction.getDatatype())),
                    Canonical.set(map(restriction.facetRestrictions(), this::facet)));
        }
        throw unsupported(value);
    }

    private void collectDataOperands(OWLDataRange value, boolean intersection, List<byte[]> output) {
        if (intersection && value instanceof OWLDataIntersectionOf) {
            ((OWLDataIntersectionOf) value).operands()
                    .forEach(child -> collectDataOperands(child, true, output));
        } else if (!intersection && value instanceof OWLDataUnionOf) {
            ((OWLDataUnionOf) value).operands()
                    .forEach(child -> collectDataOperands(child, false, output));
        } else {
            output.add(dataRange(value));
        }
    }

    private byte[] classExpression(OWLClassExpression value) {
        if (value instanceof OWLClass) {
            return entity((OWLClass) value);
        }
        if (value instanceof OWLObjectIntersectionOf) {
            List<byte[]> values = new ArrayList<>();
            collectClassOperands(value, true, values);
            return Canonical.node(Canonical.OBJECT_INTERSECTION_OF, Canonical.set(values));
        }
        if (value instanceof OWLObjectUnionOf) {
            List<byte[]> values = new ArrayList<>();
            collectClassOperands(value, false, values);
            return Canonical.node(Canonical.OBJECT_UNION_OF, Canonical.set(values));
        }
        if (value instanceof OWLObjectComplementOf) {
            return Canonical.node(
                    Canonical.OBJECT_COMPLEMENT_OF,
                    Canonical.nodeField(classExpression(((OWLObjectComplementOf) value).getOperand())));
        }
        if (value instanceof OWLObjectOneOf) {
            return Canonical.node(
                    Canonical.OBJECT_ONE_OF,
                    Canonical.set(map(((OWLObjectOneOf) value).individuals(), this::individual)));
        }
        if (value instanceof OWLObjectSomeValuesFrom) {
            OWLObjectSomeValuesFrom restriction = (OWLObjectSomeValuesFrom) value;
            return objectQuantifier(Canonical.OBJECT_SOME_VALUES_FROM, restriction);
        }
        if (value instanceof OWLObjectAllValuesFrom) {
            OWLObjectAllValuesFrom restriction = (OWLObjectAllValuesFrom) value;
            return objectQuantifier(Canonical.OBJECT_ALL_VALUES_FROM, restriction);
        }
        if (value instanceof OWLObjectHasValue) {
            OWLObjectHasValue restriction = (OWLObjectHasValue) value;
            return Canonical.node(
                    Canonical.OBJECT_HAS_VALUE,
                    Canonical.nodeField(objectProperty(restriction.getProperty())),
                    Canonical.nodeField(individual(restriction.getFiller())));
        }
        if (value instanceof OWLObjectHasSelf) {
            return Canonical.node(
                    Canonical.OBJECT_HAS_SELF,
                    Canonical.nodeField(objectProperty(((OWLObjectHasSelf) value).getProperty())));
        }
        if (value instanceof OWLObjectMinCardinality) {
            return objectCardinality(Canonical.OBJECT_MIN_CARDINALITY, (OWLObjectCardinalityRestriction) value);
        }
        if (value instanceof OWLObjectMaxCardinality) {
            return objectCardinality(Canonical.OBJECT_MAX_CARDINALITY, (OWLObjectCardinalityRestriction) value);
        }
        if (value instanceof OWLObjectExactCardinality) {
            return objectCardinality(Canonical.OBJECT_EXACT_CARDINALITY, (OWLObjectCardinalityRestriction) value);
        }
        if (value instanceof OWLDataSomeValuesFrom) {
            return dataQuantifier(Canonical.DATA_SOME_VALUES_FROM, (OWLQuantifiedDataRestriction) value);
        }
        if (value instanceof OWLDataAllValuesFrom) {
            return dataQuantifier(Canonical.DATA_ALL_VALUES_FROM, (OWLQuantifiedDataRestriction) value);
        }
        if (value instanceof OWLDataHasValue) {
            OWLDataHasValue restriction = (OWLDataHasValue) value;
            return Canonical.node(
                    Canonical.DATA_HAS_VALUE,
                    Canonical.nodeField(dataProperty(restriction.getProperty())),
                    Canonical.nodeField(literal(restriction.getFiller())));
        }
        if (value instanceof OWLDataMinCardinality) {
            return dataCardinality(Canonical.DATA_MIN_CARDINALITY, (OWLDataCardinalityRestriction) value);
        }
        if (value instanceof OWLDataMaxCardinality) {
            return dataCardinality(Canonical.DATA_MAX_CARDINALITY, (OWLDataCardinalityRestriction) value);
        }
        if (value instanceof OWLDataExactCardinality) {
            return dataCardinality(Canonical.DATA_EXACT_CARDINALITY, (OWLDataCardinalityRestriction) value);
        }
        throw unsupported(value);
    }

    private void collectClassOperands(
            OWLClassExpression value, boolean intersection, List<byte[]> output) {
        if (intersection && value instanceof OWLObjectIntersectionOf) {
            ((OWLObjectIntersectionOf) value).operands()
                    .forEach(child -> collectClassOperands(child, true, output));
        } else if (!intersection && value instanceof OWLObjectUnionOf) {
            ((OWLObjectUnionOf) value).operands()
                    .forEach(child -> collectClassOperands(child, false, output));
        } else {
            output.add(classExpression(value));
        }
    }

    private byte[] objectQuantifier(long tag, OWLQuantifiedObjectRestriction restriction) {
        return Canonical.node(
                tag,
                Canonical.nodeField(objectProperty(restriction.getProperty())),
                Canonical.nodeField(classExpression(restriction.getFiller())));
    }

    private byte[] dataQuantifier(long tag, OWLQuantifiedDataRestriction restriction) {
        return Canonical.node(
                tag,
                Canonical.sequence(List.of(dataProperty(restriction.getProperty()))),
                Canonical.nodeField(dataRange(restriction.getFiller())));
    }

    private byte[] objectCardinality(long tag, OWLObjectCardinalityRestriction restriction) {
        return Canonical.node(
                tag,
                Canonical.integer(restriction.getCardinality()),
                Canonical.nodeField(objectProperty(restriction.getProperty())),
                Canonical.nodeField(classExpression(restriction.getFiller())));
    }

    private byte[] dataCardinality(long tag, OWLDataCardinalityRestriction restriction) {
        return Canonical.node(
                tag,
                Canonical.integer(restriction.getCardinality()),
                Canonical.nodeField(dataProperty(restriction.getProperty())),
                Canonical.nodeField(dataRange(restriction.getFiller())));
    }

    private byte[] variable(SWRLVariable value) {
        return Canonical.node(
                Canonical.VARIABLE,
                Canonical.nodeField(Canonical.iri(value.getIRI().toString())));
    }

    private byte[] iArgument(SWRLIArgument value) {
        if (value instanceof SWRLVariable) {
            return variable((SWRLVariable) value);
        }
        if (value instanceof SWRLIndividualArgument) {
            return individual(((SWRLIndividualArgument) value).getIndividual());
        }
        throw unsupported(value);
    }

    private byte[] dArgument(SWRLDArgument value) {
        if (value instanceof SWRLVariable) {
            return variable((SWRLVariable) value);
        }
        if (value instanceof SWRLLiteralArgument) {
            return literal(((SWRLLiteralArgument) value).getLiteral());
        }
        throw unsupported(value);
    }

    private byte[] atom(SWRLAtom value) {
        if (value instanceof SWRLClassAtom) {
            SWRLClassAtom atom = (SWRLClassAtom) value;
            return Canonical.node(
                    Canonical.CLASS_ATOM,
                    Canonical.nodeField(classExpression(atom.getPredicate())),
                    Canonical.nodeField(iArgument(atom.getArgument())));
        }
        if (value instanceof SWRLDataRangeAtom) {
            SWRLDataRangeAtom atom = (SWRLDataRangeAtom) value;
            return Canonical.node(
                    Canonical.DATA_RANGE_ATOM,
                    Canonical.nodeField(dataRange(atom.getPredicate())),
                    Canonical.nodeField(dArgument(atom.getArgument())));
        }
        if (value instanceof SWRLObjectPropertyAtom) {
            SWRLObjectPropertyAtom atom = (SWRLObjectPropertyAtom) value;
            return Canonical.node(
                    Canonical.OBJECT_PROPERTY_ATOM,
                    Canonical.nodeField(objectProperty(atom.getPredicate())),
                    Canonical.nodeField(iArgument(atom.getFirstArgument())),
                    Canonical.nodeField(iArgument(atom.getSecondArgument())));
        }
        if (value instanceof SWRLDataPropertyAtom) {
            SWRLDataPropertyAtom atom = (SWRLDataPropertyAtom) value;
            return Canonical.node(
                    Canonical.DATA_PROPERTY_ATOM,
                    Canonical.nodeField(dataProperty(atom.getPredicate())),
                    Canonical.nodeField(iArgument(atom.getFirstArgument())),
                    Canonical.nodeField(dArgument(atom.getSecondArgument())));
        }
        if (value instanceof SWRLBuiltInAtom) {
            SWRLBuiltInAtom atom = (SWRLBuiltInAtom) value;
            return Canonical.node(
                    Canonical.BUILT_IN_ATOM,
                    Canonical.nodeField(Canonical.iri(atom.getPredicate().toString())),
                    Canonical.sequence(map(atom.arguments(), this::dArgument)));
        }
        if (value instanceof SWRLSameIndividualAtom) {
            SWRLSameIndividualAtom atom = (SWRLSameIndividualAtom) value;
            return Canonical.node(
                    Canonical.SAME_INDIVIDUAL_ATOM,
                    Canonical.nodeField(iArgument(atom.getFirstArgument())),
                    Canonical.nodeField(iArgument(atom.getSecondArgument())));
        }
        if (value instanceof SWRLDifferentIndividualsAtom) {
            SWRLDifferentIndividualsAtom atom = (SWRLDifferentIndividualsAtom) value;
            return Canonical.node(
                    Canonical.DIFFERENT_INDIVIDUALS_ATOM,
                    Canonical.nodeField(iArgument(atom.getFirstArgument())),
                    Canonical.nodeField(iArgument(atom.getSecondArgument())));
        }
        throw unsupported(value);
    }

    private byte[] axiom(OWLAxiom value, boolean includeAnnotations) {
        Canonical.Field ann = annotations(value, includeAnnotations);
        if (value instanceof SWRLRule) {
            SWRLRule rule = (SWRLRule) value;
            return Canonical.node(
                    Canonical.SWRL_RULE,
                    Canonical.set(map(rule.body(), this::atom)),
                    Canonical.set(map(rule.head(), this::atom)),
                    ann);
        }
        if (value instanceof OWLDeclarationAxiom) {
            return Canonical.node(
                    Canonical.DECLARATION,
                    Canonical.nodeField(entity(((OWLDeclarationAxiom) value).getEntity())),
                    ann);
        }
        if (value instanceof OWLSubClassOfAxiom) {
            OWLSubClassOfAxiom axiom = (OWLSubClassOfAxiom) value;
            return Canonical.node(
                    Canonical.SUB_CLASS_OF,
                    Canonical.nodeField(classExpression(axiom.getSubClass())),
                    Canonical.nodeField(classExpression(axiom.getSuperClass())),
                    ann);
        }
        if (value instanceof OWLEquivalentClassesAxiom) {
            return Canonical.node(
                    Canonical.EQUIVALENT_CLASSES,
                    Canonical.set(map(((OWLEquivalentClassesAxiom) value).classExpressions(), this::classExpression)),
                    ann);
        }
        if (value instanceof OWLDisjointClassesAxiom) {
            return Canonical.node(
                    Canonical.DISJOINT_CLASSES,
                    Canonical.set(map(((OWLDisjointClassesAxiom) value).classExpressions(), this::classExpression)),
                    ann);
        }
        if (value instanceof OWLDisjointUnionAxiom) {
            OWLDisjointUnionAxiom axiom = (OWLDisjointUnionAxiom) value;
            return Canonical.node(
                    Canonical.DISJOINT_UNION,
                    Canonical.nodeField(entity(axiom.getOWLClass())),
                    Canonical.set(map(axiom.classExpressions(), this::classExpression)),
                    ann);
        }
        if (value instanceof OWLSubPropertyChainOfAxiom) {
            OWLSubPropertyChainOfAxiom axiom = (OWLSubPropertyChainOfAxiom) value;
            return Canonical.node(
                    Canonical.SUB_OBJECT_PROPERTY_OF,
                    Canonical.nodeField(Canonical.node(
                            Canonical.OBJECT_PROPERTY_CHAIN,
                            Canonical.sequence(map(axiom.getPropertyChain(), this::objectProperty)))),
                    Canonical.nodeField(objectProperty(axiom.getSuperProperty())),
                    ann);
        }
        if (value instanceof OWLSubObjectPropertyOfAxiom) {
            OWLSubObjectPropertyOfAxiom axiom = (OWLSubObjectPropertyOfAxiom) value;
            return Canonical.node(
                    Canonical.SUB_OBJECT_PROPERTY_OF,
                    Canonical.nodeField(objectProperty(axiom.getSubProperty())),
                    Canonical.nodeField(objectProperty(axiom.getSuperProperty())),
                    ann);
        }
        if (value instanceof OWLEquivalentObjectPropertiesAxiom) {
            return naryObjectProperty(Canonical.EQUIVALENT_OBJECT_PROPERTIES,
                    (OWLNaryPropertyAxiom<OWLObjectPropertyExpression>) value, ann);
        }
        if (value instanceof OWLDisjointObjectPropertiesAxiom) {
            return naryObjectProperty(Canonical.DISJOINT_OBJECT_PROPERTIES,
                    (OWLNaryPropertyAxiom<OWLObjectPropertyExpression>) value, ann);
        }
        if (value instanceof OWLInverseObjectPropertiesAxiom) {
            OWLInverseObjectPropertiesAxiom axiom = (OWLInverseObjectPropertiesAxiom) value;
            List<byte[]> pair = new ArrayList<>(List.of(
                    objectProperty(axiom.getFirstProperty()),
                    objectProperty(axiom.getSecondProperty())));
            pair.sort(UNSIGNED_BYTES);
            return Canonical.node(
                    Canonical.INVERSE_OBJECT_PROPERTIES,
                    Canonical.nodeField(pair.get(0)),
                    Canonical.nodeField(pair.get(1)),
                    ann);
        }
        if (value instanceof OWLObjectPropertyDomainAxiom) {
            OWLObjectPropertyDomainAxiom axiom = (OWLObjectPropertyDomainAxiom) value;
            return Canonical.node(Canonical.OBJECT_PROPERTY_DOMAIN,
                    Canonical.nodeField(objectProperty(axiom.getProperty())),
                    Canonical.nodeField(classExpression(axiom.getDomain())), ann);
        }
        if (value instanceof OWLObjectPropertyRangeAxiom) {
            OWLObjectPropertyRangeAxiom axiom = (OWLObjectPropertyRangeAxiom) value;
            return Canonical.node(Canonical.OBJECT_PROPERTY_RANGE,
                    Canonical.nodeField(objectProperty(axiom.getProperty())),
                    Canonical.nodeField(classExpression(axiom.getRange())), ann);
        }
        if (value instanceof OWLFunctionalObjectPropertyAxiom) {
            return objectCharacteristic(Canonical.FUNCTIONAL_OBJECT_PROPERTY,
                    ((OWLFunctionalObjectPropertyAxiom) value).getProperty(), ann);
        }
        if (value instanceof OWLInverseFunctionalObjectPropertyAxiom) {
            return objectCharacteristic(Canonical.INVERSE_FUNCTIONAL_OBJECT_PROPERTY,
                    ((OWLInverseFunctionalObjectPropertyAxiom) value).getProperty(), ann);
        }
        if (value instanceof OWLReflexiveObjectPropertyAxiom) {
            return objectCharacteristic(Canonical.REFLEXIVE_OBJECT_PROPERTY,
                    ((OWLReflexiveObjectPropertyAxiom) value).getProperty(), ann);
        }
        if (value instanceof OWLIrreflexiveObjectPropertyAxiom) {
            return objectCharacteristic(Canonical.IRREFLEXIVE_OBJECT_PROPERTY,
                    ((OWLIrreflexiveObjectPropertyAxiom) value).getProperty(), ann);
        }
        if (value instanceof OWLSymmetricObjectPropertyAxiom) {
            return objectCharacteristic(Canonical.SYMMETRIC_OBJECT_PROPERTY,
                    ((OWLSymmetricObjectPropertyAxiom) value).getProperty(), ann);
        }
        if (value instanceof OWLAsymmetricObjectPropertyAxiom) {
            return objectCharacteristic(Canonical.ASYMMETRIC_OBJECT_PROPERTY,
                    ((OWLAsymmetricObjectPropertyAxiom) value).getProperty(), ann);
        }
        if (value instanceof OWLTransitiveObjectPropertyAxiom) {
            return objectCharacteristic(Canonical.TRANSITIVE_OBJECT_PROPERTY,
                    ((OWLTransitiveObjectPropertyAxiom) value).getProperty(), ann);
        }
        if (value instanceof OWLSubDataPropertyOfAxiom) {
            OWLSubDataPropertyOfAxiom axiom = (OWLSubDataPropertyOfAxiom) value;
            return Canonical.node(Canonical.SUB_DATA_PROPERTY_OF,
                    Canonical.nodeField(dataProperty(axiom.getSubProperty())),
                    Canonical.nodeField(dataProperty(axiom.getSuperProperty())), ann);
        }
        if (value instanceof OWLEquivalentDataPropertiesAxiom) {
            return naryDataProperty(Canonical.EQUIVALENT_DATA_PROPERTIES,
                    (OWLNaryPropertyAxiom<OWLDataPropertyExpression>) value, ann);
        }
        if (value instanceof OWLDisjointDataPropertiesAxiom) {
            return naryDataProperty(Canonical.DISJOINT_DATA_PROPERTIES,
                    (OWLNaryPropertyAxiom<OWLDataPropertyExpression>) value, ann);
        }
        if (value instanceof OWLDataPropertyDomainAxiom) {
            OWLDataPropertyDomainAxiom axiom = (OWLDataPropertyDomainAxiom) value;
            return Canonical.node(Canonical.DATA_PROPERTY_DOMAIN,
                    Canonical.nodeField(dataProperty(axiom.getProperty())),
                    Canonical.nodeField(classExpression(axiom.getDomain())), ann);
        }
        if (value instanceof OWLDataPropertyRangeAxiom) {
            OWLDataPropertyRangeAxiom axiom = (OWLDataPropertyRangeAxiom) value;
            return Canonical.node(Canonical.DATA_PROPERTY_RANGE,
                    Canonical.nodeField(dataProperty(axiom.getProperty())),
                    Canonical.nodeField(dataRange(axiom.getRange())), ann);
        }
        if (value instanceof OWLFunctionalDataPropertyAxiom) {
            return Canonical.node(Canonical.FUNCTIONAL_DATA_PROPERTY,
                    Canonical.nodeField(dataProperty(((OWLFunctionalDataPropertyAxiom) value).getProperty())), ann);
        }
        if (value instanceof OWLDatatypeDefinitionAxiom) {
            OWLDatatypeDefinitionAxiom axiom = (OWLDatatypeDefinitionAxiom) value;
            return Canonical.node(Canonical.DATATYPE_DEFINITION,
                    Canonical.nodeField(entity(axiom.getDatatype())),
                    Canonical.nodeField(dataRange(axiom.getDataRange())), ann);
        }
        if (value instanceof OWLHasKeyAxiom) {
            OWLHasKeyAxiom axiom = (OWLHasKeyAxiom) value;
            return Canonical.node(Canonical.HAS_KEY,
                    Canonical.nodeField(classExpression(axiom.getClassExpression())),
                    Canonical.set(map(axiom.objectPropertyExpressions(), this::objectProperty)),
                    Canonical.set(map(axiom.dataPropertyExpressions(), this::dataProperty)), ann);
        }
        if (value instanceof OWLSameIndividualAxiom) {
            return Canonical.node(Canonical.SAME_INDIVIDUAL,
                    Canonical.set(map(((OWLSameIndividualAxiom) value).individuals(), this::individual)), ann);
        }
        if (value instanceof OWLDifferentIndividualsAxiom) {
            return Canonical.node(Canonical.DIFFERENT_INDIVIDUALS,
                    Canonical.set(map(((OWLDifferentIndividualsAxiom) value).individuals(), this::individual)), ann);
        }
        if (value instanceof OWLClassAssertionAxiom) {
            OWLClassAssertionAxiom axiom = (OWLClassAssertionAxiom) value;
            return Canonical.node(Canonical.CLASS_ASSERTION,
                    Canonical.nodeField(classExpression(axiom.getClassExpression())),
                    Canonical.nodeField(individual(axiom.getIndividual())), ann);
        }
        if (value instanceof OWLObjectPropertyAssertionAxiom) {
            return objectAssertion(Canonical.OBJECT_PROPERTY_ASSERTION,
                    (OWLObjectPropertyAssertionAxiom) value, ann);
        }
        if (value instanceof OWLNegativeObjectPropertyAssertionAxiom) {
            return objectAssertion(Canonical.NEGATIVE_OBJECT_PROPERTY_ASSERTION,
                    (OWLNegativeObjectPropertyAssertionAxiom) value, ann);
        }
        if (value instanceof OWLDataPropertyAssertionAxiom) {
            return dataAssertion(Canonical.DATA_PROPERTY_ASSERTION,
                    (OWLDataPropertyAssertionAxiom) value, ann);
        }
        if (value instanceof OWLNegativeDataPropertyAssertionAxiom) {
            return dataAssertion(Canonical.NEGATIVE_DATA_PROPERTY_ASSERTION,
                    (OWLNegativeDataPropertyAssertionAxiom) value, ann);
        }
        if (value instanceof OWLAnnotationAssertionAxiom) {
            OWLAnnotationAssertionAxiom axiom = (OWLAnnotationAssertionAxiom) value;
            return Canonical.node(Canonical.ANNOTATION_ASSERTION,
                    Canonical.nodeField(entity(axiom.getProperty())),
                    Canonical.nodeField(annotationSubject(axiom.getSubject())),
                    Canonical.nodeField(annotationValue(axiom.getValue())), ann);
        }
        if (value instanceof OWLSubAnnotationPropertyOfAxiom) {
            OWLSubAnnotationPropertyOfAxiom axiom = (OWLSubAnnotationPropertyOfAxiom) value;
            return Canonical.node(Canonical.SUB_ANNOTATION_PROPERTY_OF,
                    Canonical.nodeField(entity(axiom.getSubProperty())),
                    Canonical.nodeField(entity(axiom.getSuperProperty())), ann);
        }
        if (value instanceof OWLAnnotationPropertyDomainAxiom) {
            OWLAnnotationPropertyDomainAxiom axiom = (OWLAnnotationPropertyDomainAxiom) value;
            return Canonical.node(Canonical.ANNOTATION_PROPERTY_DOMAIN,
                    Canonical.nodeField(entity(axiom.getProperty())),
                    Canonical.nodeField(Canonical.iri(axiom.getDomain().toString())), ann);
        }
        if (value instanceof OWLAnnotationPropertyRangeAxiom) {
            OWLAnnotationPropertyRangeAxiom axiom = (OWLAnnotationPropertyRangeAxiom) value;
            return Canonical.node(Canonical.ANNOTATION_PROPERTY_RANGE,
                    Canonical.nodeField(entity(axiom.getProperty())),
                    Canonical.nodeField(Canonical.iri(axiom.getRange().toString())), ann);
        }
        throw unsupported(value);
    }

    private byte[] objectCharacteristic(
            long tag, OWLObjectPropertyExpression property, Canonical.Field annotations) {
        return Canonical.node(tag, Canonical.nodeField(objectProperty(property)), annotations);
    }

    private byte[] naryObjectProperty(
            long tag,
            OWLNaryPropertyAxiom<OWLObjectPropertyExpression> axiom,
            Canonical.Field annotations) {
        return Canonical.node(tag,
                Canonical.set(map(axiom.properties(), this::objectProperty)), annotations);
    }

    private byte[] naryDataProperty(
            long tag,
            OWLNaryPropertyAxiom<OWLDataPropertyExpression> axiom,
            Canonical.Field annotations) {
        return Canonical.node(tag,
                Canonical.set(map(axiom.properties(), this::dataProperty)), annotations);
    }

    private byte[] objectAssertion(
            long tag, OWLPropertyAssertionAxiom<OWLObjectPropertyExpression, OWLIndividual> axiom,
            Canonical.Field annotations) {
        return Canonical.node(tag,
                Canonical.nodeField(objectProperty(axiom.getProperty())),
                Canonical.nodeField(individual(axiom.getSubject())),
                Canonical.nodeField(individual(axiom.getObject())), annotations);
    }

    private byte[] dataAssertion(
            long tag, OWLPropertyAssertionAxiom<OWLDataPropertyExpression, OWLLiteral> axiom,
            Canonical.Field annotations) {
        return Canonical.node(tag,
                Canonical.nodeField(dataProperty(axiom.getProperty())),
                Canonical.nodeField(individual(axiom.getSubject())),
                Canonical.nodeField(literal(axiom.getObject())), annotations);
    }

    private static <T> List<byte[]> map(Stream<T> values, Function<T, byte[]> mapper) {
        return values.map(mapper).collect(Collectors.toCollection(ArrayList::new));
    }

    private static <T> List<byte[]> map(Collection<T> values, Function<T, byte[]> mapper) {
        return values.stream().map(mapper).collect(Collectors.toCollection(ArrayList::new));
    }

    private static IneligibleException unsupported(Object value) {
        return new IneligibleException("OWLAPI object is outside model schema one: "
                + value.getClass().getName());
    }
}
