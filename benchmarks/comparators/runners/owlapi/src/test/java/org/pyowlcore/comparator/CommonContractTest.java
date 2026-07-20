package org.pyowlcore.comparator;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.io.ByteArrayInputStream;
import java.nio.charset.StandardCharsets;

import org.junit.jupiter.api.Test;
import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.formats.FunctionalSyntaxDocumentFormat;
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
                new ModelMapper().map(ontology),
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
}
