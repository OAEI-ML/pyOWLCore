package org.pyowlcore.comparator;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;

import org.junit.jupiter.api.Test;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.formats.FunctionalSyntaxDocumentFormat;
import org.semanticweb.owlapi.formats.TurtleDocumentFormat;
import org.semanticweb.owlapi.io.StreamDocumentSource;
import org.semanticweb.owlapi.model.IRI;
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
                "d0e4440ae74587a017bb9ef56de41dce71c2ca5fd527687235cb4524dde9f278",
                built.contract.get("contract_sha256"));
    }

    @Test
    void rdfProvenanceFollowsTheNormativeMappingPhases() throws Exception {
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
                "d2b73f23f2e1543bef4dfe141a61623694ef753a858c4cfa3f4f928d27bf5634",
                built.contract.get("contract_sha256"));
    }

    @Test
    void ambiguousRdfOccurrenceOrderFailsClosed() throws Exception {
        byte[] source = ("@prefix : <urn:ambiguous#> .\n"
                + "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
                + "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
                + ":A a owl:Class . :B a owl:Class . :C a owl:Class . :D a owl:Class .\n"
                + ":p a owl:ObjectProperty .\n"
                + ":A rdfs:subClassOf [ a owl:Restriction ; owl:onProperty :p ; "
                + "owl:someValuesFrom :B ] .\n"
                + ":C rdfs:subClassOf [ a owl:Restriction ; owl:onProperty :p ; "
                + "owl:someValuesFrom :D ] .\n")
                .getBytes(StandardCharsets.UTF_8);
        OWLOntology ontology = OWLManager.createOWLOntologyManager()
                .loadOntologyFromOntologyDocument(new StreamDocumentSource(
                        new ByteArrayInputStream(source), IRI.create("urn:ambiguous"),
                        new TurtleDocumentFormat(), null));

        assertThrows(ModelMapper.IneligibleException.class,
                () -> new ModelMapper().map(ontology, "turtle"));
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
}
