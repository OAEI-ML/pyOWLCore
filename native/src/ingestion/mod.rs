//! WP16 streaming syntax ingestion, independent of any Python facade version.
//!
//! The syntax and mapping layers return canonical document data.  The V1
//! publication module is deliberately only a first-slice test adapter; a later
//! retained V2 constructor can consume the same `CanonicalDocument` without
//! replacing the parser.

mod rdf_class_expressions;
mod rdf_lists;
mod rdfxml;
#[cfg(any(test, feature = "test-hooks"))]
mod v1_adapter;
mod v2_adapter;

use crate::error::{NativeError, NativeResult};
use crate::hash::sha256;
use crate::limits::LimitKey;
use crate::limits::Limits;
#[cfg(feature = "test-hooks")]
use crate::publication::NativeSnapshotPublicationV1;
use crate::session::Session;
use crate::{cancel::Cancellation, publication::TypedFacadeStorageV2};
use std::time::Instant;

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

pub(crate) struct RetainedRdfXmlOutcomeV2 {
    pub(crate) encoded: Vec<u8>,
    pub(crate) storage: TypedFacadeStorageV2,
    pub(crate) metadata: crate::parse::RetainedParseMetadataV2,
    pub(crate) phases: crate::parse::RetainedParsePhases,
    pub(crate) mapping_ns: u64,
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
    Ok(parse_rdfxml_timed(source, document_iri, session)?.0)
}

fn parse_rdfxml_timed(
    source: &[u8],
    document_iri: Option<&str>,
    session: &mut Session<'_>,
) -> NativeResult<(CanonicalDocument, u64)> {
    check_source(source, session)?;
    if let Some(iri) = document_iri {
        check_iri(
            iri,
            session,
            "native RDF/XML document IRI exceeds max_iri_bytes",
        )?;
    }
    let (mut document, mapping_ns) = rdfxml::parse_and_map_timed(source, document_iri, session)?;
    document.document_iri = document_iri
        .map(|value| owned_text(value, session))
        .transpose()?;
    document.source_sha256 = sha256(source);
    document.byte_length = u64::try_from(source.len())
        .map_err(|_| NativeError::limit("native RDF/XML source length exceeds u64"))?;
    Ok((document, mapping_ns))
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn parse_rdfxml_retained_v2(
    source: &[u8],
    document_iri: Option<&str>,
    session: &mut Session<'_>,
    limits: Limits,
    cancellation: Cancellation,
    interrupt: Option<crate::cancel::InterruptSlot>,
    caller_external_bytes: usize,
    collect_provenance: bool,
    require_empty_imports: bool,
) -> NativeResult<RetainedRdfXmlOutcomeV2> {
    let parse_started = Instant::now();
    let (mut document, mapping_ns) = parse_rdfxml_timed(source, document_iri, session)?;
    let parse_mapping_ns = elapsed_ns(parse_started)?;
    let syntax_parse_ns = parse_mapping_ns.saturating_sub(mapping_ns);
    if require_empty_imports && !document.imports.is_empty() {
        return Err(NativeError::new(
            "NATIVE_RDFXML_RETAINED_UNSUPPORTED",
            "native retained RDF/XML publication cannot bypass resolver-backed imports",
        ));
    }
    let rows = [
        document.ontology_annotations.as_slice(),
        document.axioms.as_slice(),
        document.extensions.as_slice(),
    ];
    let contains_anonymous = crate::parse::retained_rows_contain_anonymous_v2(rows, &limits)?;
    let anonymous_started = Instant::now();
    let mut effective_rows = None;
    let mut effective_occurrence_digests = None;
    if contains_anonymous {
        let scoped = crate::parse::scope_rdfxml_anonymous_rows_v2(
            document.ontology_iri.as_deref(),
            document.version_iri.as_deref(),
            &document.imports,
            rows,
            session,
            &cancellation,
        )?;
        let crate::parse::ScopedAnonymousRowsV2 {
            raw: [annotations, axioms, extensions],
            effective,
            effective_occurrence_digests: digests,
            source_occurrence_digests: _,
        } = scoped;
        document.ontology_annotations = annotations;
        document.axioms = axioms;
        document.extensions = extensions;
        effective_occurrence_digests = Some(digests);
        effective_rows = Some(effective);
    }
    let anonymous_scope_ns = elapsed_ns(anonymous_started)?;
    let rows = [
        document.ontology_annotations.as_slice(),
        document.axioms.as_slice(),
        document.extensions.as_slice(),
    ];
    let encode_started = Instant::now();
    let (encoded, metadata) = crate::parse::build_retained_rdfxml_seed_v2(
        document.ontology_iri.as_deref(),
        document.version_iri.as_deref(),
        &document.imports,
        rows,
        document.decoded_codepoints,
        document.mapping.total_triples,
        collect_provenance,
        effective_occurrence_digests.as_deref(),
    )?;
    let result_encode_ns = elapsed_ns(encode_started)?;
    session.finish()?;
    let published = match effective_rows.as_ref() {
        Some(effective) => v2_adapter::publish_scoped_timed(
            &document,
            effective,
            limits,
            cancellation,
            interrupt,
            caller_external_bytes,
        )?,
        None => v2_adapter::publish_timed(
            std::slice::from_ref(&document),
            &[vec![0]],
            &[0],
            limits,
            cancellation,
            interrupt,
            caller_external_bytes,
        )?,
    };
    Ok(RetainedRdfXmlOutcomeV2 {
        encoded,
        storage: published.storage,
        metadata,
        phases: crate::parse::RetainedParsePhases {
            syntax_parse_ns,
            result_encode_ns,
            arena_construction_ns: published.arena_construction_ns,
            freeze_ns: published.freeze_ns.saturating_add(anonymous_scope_ns),
        },
        mapping_ns,
    })
}

fn elapsed_ns(started: Instant) -> NativeResult<u64> {
    u64::try_from(started.elapsed().as_nanos())
        .map_err(|_| NativeError::limit("native RDF/XML phase time exceeds u64"))
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
    fn production_rdfxml_result_retains_typed_rows_and_bounded_seed() {
        let source = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:owl="http://www.w3.org/2002/07/owl#">
          <owl:Ontology rdf:about="urn:o"/>
          <owl:Class rdf:about="urn:C"/>
        </rdf:RDF>"#;
        let limits = Limits::default();
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, source.len()).expect("session");
        let outcome = parse_rdfxml_retained_v2(
            source,
            Some("urn:document"),
            &mut session,
            limits,
            cancellation,
            None,
            source.len(),
            true,
            false,
        )
        .expect("retained RDF/XML outcome");
        assert_eq!(outcome.encoded.get(..8), Some(b"PYNRRS2\0".as_slice()));
        let counts = outcome.storage.structural_counts().expect("counts");
        assert_eq!(counts.ontology_annotations, 0);
        assert_eq!(counts.stored_axioms, 1);
        assert_eq!(counts.effective_axioms, 1);
        assert_eq!(counts.extensions, 0);
        assert!(outcome.phases.syntax_parse_ns > 0);
        assert!(outcome.mapping_ns > 0);
        assert!(outcome.phases.result_encode_ns > 0);
        assert!(outcome.phases.arena_construction_ns > 0);
        assert!(outcome.phases.freeze_ns > 0);
        let prepared = crate::parse::prepare_retained_publication_v2(
            &outcome.storage,
            &outcome.metadata,
            b"manifest",
            "document-key",
            true,
            false,
            &limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect("prepared RDF/XML publication");
        assert_eq!(prepared.origin_rows.as_ref().map(Vec::len), Some(1));
        assert_eq!(
            prepared
                .origin_rows
                .as_ref()
                .and_then(|rows| rows.first())
                .and_then(|row| row.last()),
            Some(&0),
        );
        let report = prepared.rdf_report.expect("retained RDF report");
        assert!(report.conformant);
        assert_eq!(report.consumed_triples, 2);
        assert_eq!(report.total_triples, 2);
        assert_eq!(report.rows.header.len(), 17);
        assert!(report.rows.unconsumed_triples.is_empty());
        assert!(report.rows.rule_ids.is_empty());
        assert!(report.rows.diagnostics.is_empty());
    }

    #[test]
    fn retained_rdfxml_owns_anonymous_scope_and_rejects_resolver_bypass() {
        let anonymous = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#">
          <rdf:Description rdf:nodeID="anonymous"><rdfs:comment rdf:resource="urn:value"/></rdf:Description>
        </rdf:RDF>"#;
        let imported = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:owl="http://www.w3.org/2002/07/owl#">
          <owl:Ontology rdf:about="urn:o"><owl:imports rdf:resource="urn:i"/></owl:Ontology>
        </rdf:RDF>"#;
        let limits = Limits::default();
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, anonymous.len()).expect("session");
        let outcome = parse_rdfxml_retained_v2(
            anonymous,
            None,
            &mut session,
            limits,
            cancellation,
            None,
            anonymous.len(),
            true,
            false,
        )
        .expect("scoped retained RDF/XML");
        let prepared = crate::parse::prepare_retained_publication_v2(
            &outcome.storage,
            &outcome.metadata,
            b"manifest",
            "document-key",
            true,
            false,
            &limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect("prepared scoped publication");
        assert!(prepared.scoped_roots);
        assert_eq!(prepared.origin_rows.as_ref().map(Vec::len), Some(1));
        assert_eq!(prepared.raw_origin_rows.as_ref().map(Vec::len), Some(1));
        assert_ne!(
            prepared.content.root_table_sha256,
            prepared.content.effective_root_table_sha256,
        );
        assert_ne!(
            prepared.content.provenance_manifest_sha256,
            prepared.content.effective_origin_manifest_sha256,
        );

        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, imported.len()).expect("session");
        let error = parse_rdfxml_retained_v2(
            imported,
            None,
            &mut session,
            limits,
            cancellation,
            None,
            imported.len(),
            false,
            true,
        )
        .err()
        .expect("resolver bypass must fail");
        assert_eq!(error.code, "NATIVE_RDFXML_RETAINED_UNSUPPORTED");
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
