package org.pyowlcore.comparator;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.junit.jupiter.api.Test;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.formats.FunctionalSyntaxDocumentFormat;
import org.semanticweb.owlapi.formats.TurtleDocumentFormat;
import org.semanticweb.owlapi.io.StreamDocumentSource;
import org.semanticweb.owlapi.model.IRI;
import org.semanticweb.owlapi.model.OWLAnnotation;
import org.semanticweb.owlapi.model.OWLClass;
import org.semanticweb.owlapi.model.OWLDataFactory;
import org.semanticweb.owlapi.model.OWLDataProperty;
import org.semanticweb.owlapi.model.OWLNamedIndividual;
import org.semanticweb.owlapi.model.OWLObjectProperty;
import org.semanticweb.owlapi.model.OWLOntology;
import org.semanticweb.owlapi.model.OWLOntologyManager;

final class CommonContractTest {
    private static final byte[] SOURCE = (
            "Ontology(<urn:owlapi-test> Declaration(Class(<urn:A>)) "
            + "Declaration(Class(<urn:B>)) SubClassOf(<urn:A> <urn:B>))")
            .getBytes(StandardCharsets.UTF_8);
    private static final String SOURCE_SHA256 =
            "babba0f2784bed5fcae4716aa26048cd5646903e7daa758691e12b48eea3bece";
    private static final String DOCUMENT_IRI =
            "urn:pyowl-core:comparator-source:sha256:" + SOURCE_SHA256;

    @Test
    void owlapiMappingMatchesIndependentPythonContract() throws Exception {
        OWLOntologyManager manager = OWLManager.createOWLOntologyManager();
        OWLOntology ontology = manager.loadOntologyFromOntologyDocument(
                new StreamDocumentSource(
                        new ByteArrayInputStream(SOURCE),
                        IRI.create(DOCUMENT_IRI),
                        new FunctionalSyntaxDocumentFormat(),
                        null));
        CommonContract.Build built = CommonContract.build(
                new ModelMapper().map(ontology, "functional"),
                new CommonContract.RequestContext(
                        "owlapi-unit",
                        SOURCE,
                        SOURCE_SHA256,
                        DOCUMENT_IRI,
                        "functional",
                        "a68176678f9e39941cd6258b3b7181355afbbf751c89e43cc69e516aed82d24c"));

        assertEquals(
                "c6d135da81058b44cf4f3a550568b4e4ee8ecd35352548bf2ebd629996ab860b",
                built.contract.get("contract_sha256"));
    }

    @Test
    void languageLiteralsUseTheCommonPlainLiteralDatatype() throws Exception {
        byte[] source = ("@prefix : <urn:t#> .\n"
                + "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
                + "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
                + "<urn:t> a owl:Ontology .\n"
                + ":C a owl:Class ; rdfs:label \"Colour\"@EN-gb .\n")
                .getBytes(StandardCharsets.UTF_8);
        String sourceSha256 =
                "4a52fa6d54cce6df114b709224032ed2312b218d6a1afbd072cf95604fdbaf8f";
        String documentIri = "urn:pyowl-core:comparator-source:sha256:" + sourceSha256;
        OWLOntology ontology = OWLManager.createOWLOntologyManager()
                .loadOntologyFromOntologyDocument(new StreamDocumentSource(
                        new ByteArrayInputStream(source), IRI.create(documentIri),
                        new TurtleDocumentFormat(), null));
        CommonContract.Build built = CommonContract.build(
                new ModelMapper().map(ontology, "turtle"),
                new CommonContract.RequestContext(
                        "owlapi-language", source, sourceSha256, documentIri, "turtle",
                        "6ad540e139870561dc6d37919e52c6534a494441e40a80fad8ab0f2e7a0f169b"));

        assertEquals(
                "ac8bf5e2bd26f22ba4989bd376189f3fcfca44e03d99bfe8321c967bc02f56e7",
                built.contract.get("contract_sha256"));
    }

    @Test
    void rdfProvenanceUsesCanonicalDigestOrdinals() throws Exception {
        byte[] source = ("@prefix : <https://example.org/pyowl-core/benchmark#> .\n"
                + "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
                + "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
                + "<https://example.org/pyowl-core/benchmark> a owl:Ontology .\n"
                + ":C000000 a owl:Class ; rdfs:label \"Class 000000\"@en .\n"
                + ":C000001 a owl:Class ; rdfs:label \"Class 000001\"@en .\n"
                + ":C000002 a owl:Class ; rdfs:label \"Class 000002\"@en .\n"
                + ":Union a owl:Class ; owl:equivalentClass [ a owl:Class ; "
                + "owl:unionOf (:C000000 :C000001 :C000002) ] .\n")
                .getBytes(StandardCharsets.UTF_8);
        String sourceSha256 =
                "53b6279f1dc204308f1a71f7ac3b3cb6de6e5e51c00916338b71b2763bd317a5";
        String documentIri = "urn:pyowl-core:comparator-source:sha256:" + sourceSha256;
        OWLOntology ontology = OWLManager.createOWLOntologyManager()
                .loadOntologyFromOntologyDocument(new StreamDocumentSource(
                        new ByteArrayInputStream(source), IRI.create(documentIri),
                        new TurtleDocumentFormat(), null));
        CommonContract.Build built = CommonContract.build(
                new ModelMapper().map(ontology, "turtle"),
                new CommonContract.RequestContext(
                        "owlapi-annotation-list", source, sourceSha256, documentIri, "turtle",
                        "6ad540e139870561dc6d37919e52c6534a494441e40a80fad8ab0f2e7a0f169b"));

        assertEquals(
                "3373ab19c1a1ad7f231f7560937c8e1581dde37f491f7359508cfc54afa49489",
                built.contract.get("contract_sha256"));
    }

    @Test
    void rdfEquivalenceComponentsAreTransitivelyCoalescedWithAnnotations() throws Exception {
        OWLOntologyManager manager = OWLManager.createOWLOntologyManager();
        OWLDataFactory dataFactory = manager.getOWLDataFactory();
        OWLAnnotation first = dataFactory.getOWLAnnotation(
                dataFactory.getRDFSLabel(), dataFactory.getOWLLiteral("first"));
        OWLAnnotation second = dataFactory.getOWLAnnotation(
                dataFactory.getRDFSComment(), dataFactory.getOWLLiteral("second"));
        OWLClass classA = dataFactory.getOWLClass(IRI.create("urn:class-a"));
        OWLClass classB = dataFactory.getOWLClass(IRI.create("urn:class-b"));
        OWLClass classC = dataFactory.getOWLClass(IRI.create("urn:class-c"));
        OWLObjectProperty objectA =
                dataFactory.getOWLObjectProperty(IRI.create("urn:object-a"));
        OWLObjectProperty objectB =
                dataFactory.getOWLObjectProperty(IRI.create("urn:object-b"));
        OWLObjectProperty objectC =
                dataFactory.getOWLObjectProperty(IRI.create("urn:object-c"));
        OWLDataProperty dataA = dataFactory.getOWLDataProperty(IRI.create("urn:data-a"));
        OWLDataProperty dataB = dataFactory.getOWLDataProperty(IRI.create("urn:data-b"));
        OWLDataProperty dataC = dataFactory.getOWLDataProperty(IRI.create("urn:data-c"));
        OWLNamedIndividual individualA =
                dataFactory.getOWLNamedIndividual(IRI.create("urn:individual-a"));
        OWLNamedIndividual individualB =
                dataFactory.getOWLNamedIndividual(IRI.create("urn:individual-b"));
        OWLNamedIndividual individualC =
                dataFactory.getOWLNamedIndividual(IRI.create("urn:individual-c"));

        OWLOntology graph = manager.createOntology();
        manager.addAxiom(graph, dataFactory.getOWLEquivalentClassesAxiom(
                Set.of(classA, classB), Set.of(first)));
        manager.addAxiom(graph, dataFactory.getOWLEquivalentClassesAxiom(
                Set.of(classB, classC), Set.of(second)));
        manager.addAxiom(graph, dataFactory.getOWLEquivalentObjectPropertiesAxiom(
                Set.of(objectA, objectB), Set.of(first)));
        manager.addAxiom(graph, dataFactory.getOWLEquivalentObjectPropertiesAxiom(
                Set.of(objectB, objectC), Set.of(second)));
        manager.addAxiom(graph, dataFactory.getOWLEquivalentDataPropertiesAxiom(
                Set.of(dataA, dataB), Set.of(first)));
        manager.addAxiom(graph, dataFactory.getOWLEquivalentDataPropertiesAxiom(
                Set.of(dataB, dataC), Set.of(second)));
        manager.addAxiom(graph, dataFactory.getOWLSameIndividualAxiom(
                Set.of(individualA, individualB), Set.of(first)));
        manager.addAxiom(graph, dataFactory.getOWLSameIndividualAxiom(
                Set.of(individualB, individualC), Set.of(second)));

        OWLOntology expected = manager.createOntology();
        manager.addAxiom(expected, dataFactory.getOWLEquivalentClassesAxiom(
                Set.of(classA, classB, classC), Set.of(first, second)));
        manager.addAxiom(expected, dataFactory.getOWLEquivalentObjectPropertiesAxiom(
                Set.of(objectA, objectB, objectC), Set.of(first, second)));
        manager.addAxiom(expected, dataFactory.getOWLEquivalentDataPropertiesAxiom(
                Set.of(dataA, dataB, dataC), Set.of(first, second)));
        manager.addAxiom(expected, dataFactory.getOWLSameIndividualAxiom(
                Set.of(individualA, individualB, individualC), Set.of(first, second)));

        ModelMapper.MappedDocument actualMapped = new ModelMapper().map(graph, "rdfxml");
        ModelMapper.MappedDocument expectedMapped =
                new ModelMapper().map(expected, "functional");
        List<byte[]> actualRoots = new ArrayList<>(actualMapped.provenanceRoots);
        actualRoots.sort(Arrays::compareUnsigned);

        assertEquals(4, actualMapped.axioms.size());
        assertEquals(4, actualRoots.size());
        for (int index = 0; index < expectedMapped.axioms.size(); index++) {
            assertArrayEquals(
                    expectedMapped.axioms.get(index).value,
                    actualMapped.axioms.get(index).value);
            assertArrayEquals(actualMapped.axioms.get(index).value, actualRoots.get(index));
        }
    }

    @Test
    void reorderedRdfRootsConvergeOnCanonicalProvenance() throws Exception {
        String prefix = "@prefix : <urn:ambiguous#> .\n"
                + "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
                + "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
                + "<urn:ambiguous> a owl:Ontology .\n"
                + ":A a owl:Class . :B a owl:Class . :C a owl:Class . :D a owl:Class .\n"
                + ":p a owl:ObjectProperty .\n";
        String first = ":A rdfs:subClassOf [ a owl:Restriction ; owl:onProperty :p ; "
                + "owl:someValuesFrom :B ] .\n";
        String second = ":C rdfs:subClassOf [ a owl:Restriction ; owl:onProperty :p ; "
                + "owl:someValuesFrom :D ] .\n";

        Map<String, Object> forward = buildTurtleContract(
                (prefix + first + second).getBytes(StandardCharsets.UTF_8));
        Map<String, Object> reverse = buildTurtleContract(
                (prefix + second + first).getBytes(StandardCharsets.UTF_8));

        assertEquals(forward.get("root_document_key"), reverse.get("root_document_key"));
        assertEquals(forward.get("provenance"), reverse.get("provenance"));
    }

    @Test
    void canonicalOriginOrdinalsRetainAllDistinguishingEvidence() {
        byte[] alpha = Canonical.iri("urn:alpha");
        byte[] beta = Canonical.iri("urn:beta");
        byte[] gamma = Canonical.iri("urn:gamma");
        List<Object> baseline =
                CommonContract.originRows(List.of(alpha, beta, alpha), "document-a", true);

        assertEquals(
                baseline,
                CommonContract.originRows(List.of(alpha, alpha, beta), "document-a", true));
        assertEquals(List.of(0, 1, 2), occurrenceOrdinals(baseline));
        assertNotEquals(
                baseline,
                CommonContract.originRows(List.of(alpha, beta, alpha), "document-b", true));
        assertNotEquals(
                baseline,
                CommonContract.originRows(List.of(alpha, gamma, alpha), "document-a", true));
        assertNotEquals(
                baseline,
                CommonContract.originRows(List.of(alpha, beta), "document-a", true));
        assertNotEquals(
                CommonContract.originRows(List.of(alpha, beta), "document-a", false),
                CommonContract.originRows(List.of(beta, alpha), "document-a", false));
    }

    @Test
    void anonymousIndividualIdentityFailsClosed() throws Exception {
        byte[] source = ("@prefix : <urn:anonymous#> .\n"
                + "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
                + ":C a owl:Class . [] a :C .\n")
                .getBytes(StandardCharsets.UTF_8);
        OWLOntology ontology = OWLManager.createOWLOntologyManager()
                .loadOntologyFromOntologyDocument(new StreamDocumentSource(
                        new ByteArrayInputStream(source), IRI.create("urn:anonymous"),
                        new TurtleDocumentFormat(), null));

        assertThrows(ModelMapper.IneligibleException.class,
                () -> new ModelMapper().map(ontology, "turtle"));
    }

    @SuppressWarnings("unchecked")
    private static List<Integer> occurrenceOrdinals(List<Object> rows) {
        List<Integer> output = new ArrayList<>();
        for (Object row : rows) {
            for (Object occurrence : (List<Object>) ((Map<String, Object>) row).get("occurrences")) {
                output.add((Integer) ((Map<String, Object>) occurrence).get("occurrence"));
            }
        }
        return output;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> buildTurtleContract(byte[] source) throws Exception {
        String sourceSha256 = Canonical.hex(Canonical.sha256(source));
        String documentIri = "urn:pyowl-core:comparator-source:sha256:" + sourceSha256;
        OWLOntology ontology = OWLManager.createOWLOntologyManager()
                .loadOntologyFromOntologyDocument(new StreamDocumentSource(
                        new ByteArrayInputStream(source), IRI.create(documentIri),
                        new TurtleDocumentFormat(), null));
        return CommonContract.build(
                new ModelMapper().map(ontology, "turtle"),
                new CommonContract.RequestContext(
                        "owlapi-reordered", source, sourceSha256, documentIri, "turtle",
                        "6ad540e139870561dc6d37919e52c6534a494441e40a80fad8ab0f2e7a0f169b"))
                .contract;
    }
}
