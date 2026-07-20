//! WP16 streaming syntax ingestion, independent of any Python facade version.
//!
//! The syntax and mapping layers return canonical document data.  The V1
//! publication module is deliberately only a first-slice test adapter; a later
//! retained V2 constructor can consume the same `CanonicalDocument` without
//! replacing the parser.

#[allow(dead_code)]
mod rdf_lists;
mod rdfxml;
#[cfg(any(test, feature = "test-hooks"))]
mod v1_adapter;
mod v2_adapter;

use crate::error::{NativeError, NativeResult};
use crate::hash::sha256;
use crate::limits::LimitKey;
#[cfg(feature = "test-hooks")]
use crate::publication::NativeSnapshotPublicationV1;
use crate::session::Session;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct MappingEvidence {
    pub(super) total_triples: u64,
    pub(super) consumed_triples: u64,
    pub(super) rule_ids: &'static [&'static str],
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct CanonicalDocument {
    pub(super) document_iri: Option<String>,
    pub(super) ontology_iri: Option<String>,
    pub(super) version_iri: Option<String>,
    pub(super) imports: Vec<String>,
    pub(super) ontology_annotations: Vec<Vec<u8>>,
    pub(super) axioms: Vec<Vec<u8>>,
    pub(super) extensions: Vec<Vec<u8>>,
    pub(super) source_sha256: [u8; 32],
    pub(super) byte_length: u64,
    pub(super) decoded_codepoints: u64,
    pub(super) mapping: MappingEvidence,
}

#[cfg(feature = "test-hooks")]
pub(super) struct V1TestAdapterOutcome {
    pub(super) publication: NativeSnapshotPublicationV1,
    pub(super) observation: Vec<u8>,
}

fn parse_rdfxml(
    source: &[u8],
    document_iri: Option<&str>,
    session: &mut Session<'_>,
) -> NativeResult<CanonicalDocument> {
    check_source(source, session)?;
    if let Some(iri) = document_iri {
        check_iri(
            iri,
            session,
            "native RDF/XML document IRI exceeds max_iri_bytes",
        )?;
    }
    let mut document = rdfxml::parse_and_map(source, document_iri, session)?;
    document.document_iri = document_iri
        .map(|value| owned_text(value, session))
        .transpose()?;
    document.source_sha256 = sha256(source);
    document.byte_length = u64::try_from(source.len())
        .map_err(|_| NativeError::limit("native RDF/XML source length exceeds u64"))?;
    Ok(document)
}

fn owned_text(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    session.reserve_bytes(value.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native RDF/XML document IRI allocation failed"))?;
    output.push_str(value);
    Ok(output)
}

#[cfg(feature = "test-hooks")]
pub(super) fn ingest_rdfxml_v1_test_adapter(
    source: &[u8],
    document_iri: Option<&str>,
    session: &mut Session<'_>,
) -> NativeResult<V1TestAdapterOutcome> {
    let document = parse_rdfxml(source, document_iri, session)?;
    let observation = v1_adapter::encode_observation(&document, session)?;
    let publication = v1_adapter::publish(&document, session)?;
    Ok(V1TestAdapterOutcome {
        publication,
        observation,
    })
}

fn check_source(source: &[u8], session: &Session<'_>) -> NativeResult<()> {
    let size = u64::try_from(source.len())
        .map_err(|_| NativeError::limit("native RDF/XML source length exceeds u64"))?;
    let transient = size
        .checked_mul(3)
        .ok_or_else(|| NativeError::limit("native RDF/XML transient size overflow"))?;
    if size > session.limits().value(LimitKey::MaxSourceBytes)
        || size > session.limits().value(LimitKey::MaxTotalSourceBytes)
        || transient > session.limits().value(LimitKey::MaxTemporaryBytes)
    {
        return Err(NativeError::limit(
            "native RDF/XML source exceeds configured resource limits",
        ));
    }
    Ok(())
}

fn check_iri(value: &str, session: &Session<'_>, limit_message: &'static str) -> NativeResult<()> {
    if u64::try_from(value.len()).map_or(true, |size| {
        size > session.limits().value(LimitKey::MaxIriBytes)
    }) {
        return Err(NativeError::limit(limit_message));
    }
    crate::model::validate_iri(value).map_err(|error| {
        if error.code == "NATIVE_WIRE_CORRUPTION" {
            NativeError::new(
                "NATIVE_RDFXML_SYNTAX",
                "native RDF/XML contains a relative or invalid IRI",
            )
        } else {
            error
        }
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cancel::{Cancellation, Guard};
    use crate::limits::Limits;

    fn parse(source: &[u8], document_iri: Option<&str>) -> NativeResult<CanonicalDocument> {
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, source.len())?;
        let result = parse_rdfxml(source, document_iri, &mut session)?;
        session.finish()?;
        Ok(result)
    }

    #[test]
    fn source_digest_and_document_iri_wrap_the_parser_without_changing_mapping() {
        let source = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:owl="http://www.w3.org/2002/07/owl#">
          <owl:Class rdf:about="urn:C"/>
        </rdf:RDF>"#;
        let document = parse(source, Some("urn:document")).expect("mapped document");
        assert_eq!(document.document_iri.as_deref(), Some("urn:document"));
        assert_eq!(document.source_sha256, sha256(source));
        assert_eq!(document.byte_length, source.len() as u64);
        assert_eq!(document.axioms.len(), 1);
    }

    #[test]
    fn v1_adapter_is_a_real_freeze_seam_not_a_parallel_owner() {
        let source = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:owl="http://www.w3.org/2002/07/owl#">
          <owl:Ontology rdf:about="urn:o"/>
          <owl:Class rdf:about="urn:C"/>
        </rdf:RDF>"#;
        let limits = Limits::default();
        let mut guard = Guard::new(
            Cancellation::with_duration(None),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, source.len()).expect("session");
        let document = parse_rdfxml(source, None, &mut session).expect("mapped document");
        let publication = v1_adapter::publish(&document, &mut session).expect("publication");
        let storage = publication
            .handle()
            .storage()
            .expect("V1 publication storage");
        let attestation = storage.attestation();
        assert_eq!(attestation.stored_axiom_count, 1);
        assert_eq!(attestation.rdf_mapping_report_count, 1);
        assert_eq!(storage.arena().canonical_rows().len(), 1);
        assert_eq!(storage.arena().documents().len(), 1);
    }
}
