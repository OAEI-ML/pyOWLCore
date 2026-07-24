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
    pub(super) occurrence_count: u64,
    pub(super) rule_ids: &'static [&'static str],
    pub(crate) unconsumed: Vec<RdfTripleEvidence>,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct RdfTripleEvidence {
    pub(crate) subject: String,
    pub(crate) predicate: String,
    pub(crate) object: String,
    pub(crate) object_requires_repr: bool,
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
    pub(super) occurrences: Vec<CanonicalOccurrence>,
    pub(super) language_spellings: Vec<String>,
    pub(super) source_blank_labels: Vec<String>,
    pub(super) source_prefixes: Vec<(String, String)>,
    pub(super) source_sha256: [u8; 32],
    pub(super) byte_length: u64,
    pub(super) decoded_codepoints: u64,
    pub(super) mapping: MappingEvidence,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct CanonicalOccurrence {
    pub(crate) collection: u8,
    pub(crate) row: Vec<u8>,
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
    allow_swrl: bool,
    session: &mut Session<'_>,
) -> NativeResult<CanonicalDocument> {
    Ok(parse_rdfxml_timed(
        source,
        document_iri,
        allow_swrl,
        false,
        false,
        false,
        session,
    )?
    .0)
}

fn parse_rdfxml_timed(
    source: &[u8],
    document_iri: Option<&str>,
    allow_swrl: bool,
    allow_partial_rdf_mapping: bool,
    capture_occurrences: bool,
    preserve_source_map: bool,
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
    let (mut document, mapping_ns) = rdfxml::parse_and_map_timed(
        source,
        document_iri,
        allow_swrl,
        allow_partial_rdf_mapping,
        capture_occurrences,
        preserve_source_map,
        session,
    )?;
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
    preserve_source_map: bool,
    allow_swrl: bool,
    require_empty_imports: bool,
) -> NativeResult<RetainedRdfXmlOutcomeV2> {
    parse_rdfxml_retained_v2_with_mapping(
        source,
        document_iri,
        session,
        limits,
        cancellation,
        interrupt,
        caller_external_bytes,
        collect_provenance,
        preserve_source_map,
        false,
        allow_swrl,
        require_empty_imports,
    )
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn parse_rdfxml_retained_v2_with_mapping(
    source: &[u8],
    document_iri: Option<&str>,
    session: &mut Session<'_>,
    limits: Limits,
    cancellation: Cancellation,
    interrupt: Option<crate::cancel::InterruptSlot>,
    caller_external_bytes: usize,
    collect_provenance: bool,
    preserve_source_map: bool,
    allow_partial_rdf_mapping: bool,
    allow_swrl: bool,
    require_empty_imports: bool,
) -> NativeResult<RetainedRdfXmlOutcomeV2> {
    let parse_started = Instant::now();
    let (mut document, mapping_ns) = parse_rdfxml_timed(
        source,
        document_iri,
        allow_swrl,
        allow_partial_rdf_mapping,
        collect_provenance || preserve_source_map,
        preserve_source_map,
        session,
    )?;
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
    let mut scoped_occurrence_digests: Option<Vec<([u8; 32], [u8; 32])>> = None;
    let mut effective_origin_fallbacks = Vec::new();
    if contains_anonymous {
        let scoped_rows = [
            document.ontology_annotations.clone(),
            document.axioms.clone(),
            document.extensions.clone(),
        ];
        let scoped = crate::parse::scope_rdfxml_anonymous_rows_v2(
            document.ontology_iri.as_deref(),
            document.version_iri.as_deref(),
            &document.imports,
            [
                scoped_rows[0].as_slice(),
                scoped_rows[1].as_slice(),
                scoped_rows[2].as_slice(),
            ],
            session,
            &cancellation,
        )?;
        let crate::parse::ScopedAnonymousRowsV2 {
            raw: [annotations, axioms, extensions],
            effective,
            effective_occurrence_digests,
            source_occurrence_digests,
        } = scoped;
        if effective_occurrence_digests.len() != source_occurrence_digests.len() {
            return Err(NativeError::protocol(
                "native RDF/XML scoped occurrence digest tables diverge",
            ));
        }
        document.ontology_annotations = annotations;
        document.axioms = axioms;
        document.extensions = extensions;
        if collect_provenance || preserve_source_map {
            let canonical_count = scoped_rows.iter().try_fold(0_usize, |total, values| {
                total
                    .checked_add(values.len())
                    .ok_or_else(|| NativeError::limit("native RDF/XML root count overflow"))
            })?;
            if source_occurrence_digests.len() != canonical_count {
                return Err(NativeError::protocol(
                    "native RDF/XML scoped root digests diverge from canonical roots",
                ));
            }
            let lookup_bytes = canonical_count
                .checked_mul(std::mem::size_of::<(&[u8], ([u8; 32], [u8; 32]))>())
                .ok_or_else(|| {
                    NativeError::limit("native RDF/XML occurrence lookup size overflow")
                })?;
            session.reserve_bytes(lookup_bytes)?;
            let mut lookup = Vec::new();
            lookup.try_reserve_exact(canonical_count).map_err(|_| {
                NativeError::limit("native RDF/XML occurrence lookup allocation failed")
            })?;
            lookup.extend(
                scoped_rows
                    .iter()
                    .flatten()
                    .map(Vec::as_slice)
                    .zip(source_occurrence_digests.iter().copied()),
            );
            lookup.sort_unstable_by(|left, right| left.0.cmp(right.0));
            if lookup
                .windows(2)
                .any(|pair| pair[0].0 == pair[1].0 && pair[0].1 != pair[1].1)
            {
                return Err(NativeError::protocol(
                    "native RDF/XML equal roots received different anonymous scopes",
                ));
            }
            let mut ordered = Vec::new();
            ordered
                .try_reserve_exact(document.occurrences.len())
                .map_err(|_| NativeError::limit("native RDF/XML occurrence allocation failed"))?;
            for occurrence in &document.occurrences {
                let selected = lookup
                    .binary_search_by(|candidate| candidate.0.cmp(occurrence.row.as_slice()))
                    .map_err(|_| {
                        NativeError::protocol(
                            "native RDF/XML occurrence root is absent from canonical storage",
                        )
                    })?;
                ordered.push(lookup[selected].1);
            }
            if collect_provenance {
                let explicit_bytes = ordered
                    .len()
                    .checked_mul(std::mem::size_of::<[u8; 32]>())
                    .ok_or_else(|| {
                        NativeError::limit("native RDF/XML explicit-origin lookup size overflow")
                    })?;
                session.reserve_bytes(explicit_bytes)?;
                let mut explicit = Vec::new();
                explicit.try_reserve_exact(ordered.len()).map_err(|_| {
                    NativeError::limit("native RDF/XML explicit-origin allocation failed")
                })?;
                explicit.extend(ordered.iter().map(|(raw, _effective)| *raw));
                explicit.sort_unstable();
                explicit.dedup();

                let fallback_bytes = canonical_count
                    .checked_mul(std::mem::size_of::<([u8; 32], u64)>())
                    .ok_or_else(|| {
                        NativeError::limit("native RDF/XML origin fallback size overflow")
                    })?;
                session.reserve_bytes(fallback_bytes)?;
                effective_origin_fallbacks
                    .try_reserve_exact(canonical_count)
                    .map_err(|_| {
                        NativeError::limit("native RDF/XML origin fallback allocation failed")
                    })?;
                let mut fallback = 0_u64;
                for (raw, effective) in &source_occurrence_digests {
                    if explicit.binary_search(raw).is_err() {
                        effective_origin_fallbacks.push((*effective, fallback));
                        fallback = fallback.checked_add(1).ok_or_else(|| {
                            NativeError::limit("native RDF/XML origin fallback occurrence overflow")
                        })?;
                    }
                }
                let effective_origin_count = u64::try_from(ordered.len())
                    .ok()
                    .and_then(|count| count.checked_add(fallback))
                    .ok_or_else(|| {
                        NativeError::limit("native RDF/XML effective origin count overflow")
                    })?;
                if effective_origin_count > limits.max_origin_entries {
                    return Err(NativeError::limit(
                        "native retained publication exceeds max_origin_entries",
                    ));
                }
            }
            scoped_occurrence_digests = Some(ordered);
        }
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
        document.mapping.consumed_triples,
        std::mem::take(&mut document.mapping.unconsumed),
        document.mapping.occurrence_count,
        &document.occurrences,
        std::mem::take(&mut document.language_spellings),
        std::mem::take(&mut document.source_blank_labels),
        std::mem::take(&mut document.source_prefixes),
        collect_provenance || preserve_source_map,
        preserve_source_map,
        contains_anonymous,
        scoped_occurrence_digests.as_deref(),
        effective_origin_fallbacks,
        &limits,
        &cancellation,
    )?;
    document.occurrences = Vec::new();
    let result_encode_ns = elapsed_ns(encode_started)?;
    session.finish()?;
    let published = match effective_rows.as_ref() {
        Some(effective) => v2_adapter::publish_scoped_timed(
            std::slice::from_ref(&document),
            std::slice::from_ref(effective),
            &[vec![0]],
            &[0],
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
    allow_swrl: bool,
    session: &mut Session<'_>,
) -> NativeResult<V1TestAdapterOutcome> {
    let document = parse_rdfxml(source, document_iri, allow_swrl, session)?;
    let observation = v1_adapter::encode_observation(&document, session)?;
    let publication = v1_adapter::publish(&document, session)?;
    Ok(V1TestAdapterOutcome {
        publication,
        observation,
    })
}

#[cfg(feature = "test-hooks")]
pub(super) fn parse_rdfxml_graph_test_adapter(
    source: &[u8],
    document_iri: Option<&str>,
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>> {
    check_source(source, session)?;
    if let Some(iri) = document_iri {
        check_iri(
            iri,
            session,
            "native RDF/XML document IRI exceeds max_iri_bytes",
        )?;
    }
    rdfxml::parse_graph_observation(source, document_iri, session)
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
        let result = parse_rdfxml(source, document_iri, true, &mut session)?;
        session.finish()?;
        Ok(result)
    }

    fn origin_document_key(row: &[u8]) -> String {
        let size = usize::try_from(u32::from_le_bytes(
            row[32..36].try_into().expect("origin key length"),
        ))
        .expect("origin key size");
        std::str::from_utf8(&row[36..36 + size])
            .expect("origin key")
            .to_owned()
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
            true,
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
            true,
            &limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect("prepared RDF/XML publication");
        assert_eq!(prepared.origin_rows.as_ref().map(Vec::len), Some(1));
        assert_eq!(prepared.raw_origin_rows.as_ref().map(Vec::len), Some(1));
        let raw_rows = prepared.raw_origin_rows.as_ref().expect("raw origins");
        let effective_rows = prepared.origin_rows.as_ref().expect("effective origins");
        let document_fingerprint = outcome
            .metadata
            .document_fingerprint
            .digest
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        assert_eq!(origin_document_key(&raw_rows[0]), document_fingerprint);
        assert_eq!(origin_document_key(&effective_rows[0]), "document-key");
        assert_eq!(
            prepared
                .source_map
                .as_ref()
                .map(|source| source.entries.len()),
            Some(1),
        );
        assert_eq!(
            prepared
                .source_map
                .as_ref()
                .map(|source| source.prefixes.len()),
            Some(2),
        );
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
    fn retained_rdfxml_partial_mapping_keeps_exact_lazy_report_rows() {
        let source = br#"<rdf:RDF
            xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:e="urn:example:">
          <rdf:Description rdf:about="urn:s"><e:p>value</e:p></rdf:Description>
        </rdf:RDF>"#;
        let limits = Limits::default();
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, source.len()).expect("session");
        let mut outcome = parse_rdfxml_retained_v2_with_mapping(
            source,
            None,
            &mut session,
            limits,
            cancellation,
            None,
            source.len(),
            false,
            false,
            true,
            false,
            false,
        )
        .expect("explicit partial mapping");
        let unresolved = crate::parse::prepare_retained_publication_v2(
            &outcome.storage,
            &outcome.metadata,
            b"manifest",
            "document-key",
            false,
            false,
            &limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect_err("raw literal evidence must not reach publication");
        assert_eq!(unresolved.code, "NATIVE_PROTOCOL");
        outcome
            .metadata
            .render_rdf_literal_evidence(|lexical| {
                Ok::<String, std::convert::Infallible>(format!("'{lexical}'"))
            })
            .expect("literal evidence rendering");
        let prepared = crate::parse::prepare_retained_publication_v2(
            &outcome.storage,
            &outcome.metadata,
            b"manifest",
            "document-key",
            false,
            false,
            &limits,
            Cancellation::with_duration(None),
            None,
        )
        .expect("prepared partial RDF report");
        let report = prepared.rdf_report.expect("partial RDF report");
        assert!(!report.conformant);
        assert_eq!(report.consumed_triples, 0);
        assert_eq!(report.total_triples, 1);
        assert_eq!(report.rows.header[0], 0);
        assert_eq!(report.rows.unconsumed_triples.len(), 1);
        assert_eq!(report.rows.rule_ids.len(), 1);
        assert!(report.rows.diagnostics.is_empty());

        fn text(value: &str) -> Vec<u8> {
            let mut encoded = Vec::new();
            encoded.extend_from_slice(&(value.len() as u32).to_le_bytes());
            encoded.extend_from_slice(value.as_bytes());
            encoded
        }
        let expected = ["<urn:s>", "urn:example:p", "'value'"]
            .into_iter()
            .flat_map(text)
            .collect::<Vec<_>>();
        assert_eq!(report.rows.unconsumed_triples[0], expected);
        assert_eq!(report.rows.rule_ids[0], text("OWL2-RDF-REVERSE"));
    }

    #[test]
    fn retained_rdfxml_requires_explicit_swrl_enablement() {
        let source = br#"<rdf:RDF
            xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:swrl="http://www.w3.org/2003/11/swrl#">
          <swrl:Imp>
            <swrl:body rdf:resource="http://www.w3.org/1999/02/22-rdf-syntax-ns#nil"/>
            <swrl:head rdf:resource="http://www.w3.org/1999/02/22-rdf-syntax-ns#nil"/>
          </swrl:Imp>
        </rdf:RDF>"#;
        let limits = Limits::default();
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, source.len()).expect("session");
        let error = parse_rdfxml_retained_v2(
            source,
            None,
            &mut session,
            limits,
            cancellation,
            None,
            source.len(),
            false,
            false,
            false,
            false,
        )
        .err()
        .expect("disabled SWRL must fail");
        assert_eq!(error.code, "NATIVE_EXTENSION_DISABLED");

        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(
            cancellation.clone(),
            limits.deadline,
            limits.cancellation_stride,
        );
        let mut session = Session::new(&mut guard, &limits, source.len()).expect("session");
        let outcome = parse_rdfxml_retained_v2(
            source,
            None,
            &mut session,
            limits,
            cancellation,
            None,
            source.len(),
            false,
            false,
            true,
            false,
        )
        .expect("explicitly enabled SWRL");
        assert_eq!(
            outcome
                .storage
                .structural_counts()
                .expect("counts")
                .extensions,
            1
        );
    }

    #[test]
    fn retained_rdfxml_owns_anonymous_scope_and_rejects_resolver_bypass() {
        let anonymous = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:rdfs="http://www.w3.org/2000/01/rdf-schema#"
            xmlns:owl="http://www.w3.org/2002/07/owl#">
          <owl:Ontology rdf:about="urn:o"><rdfs:comment>ontology</rdfs:comment></owl:Ontology>
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
        assert_eq!(prepared.origin_rows.as_ref().map(Vec::len), Some(2));
        assert_eq!(prepared.raw_origin_rows.as_ref().map(Vec::len), Some(1));
        let raw_rows = prepared.raw_origin_rows.as_ref().expect("raw origins");
        let effective_rows = prepared.origin_rows.as_ref().expect("effective origins");
        let document_fingerprint = outcome
            .metadata
            .document_fingerprint
            .digest
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        assert_eq!(origin_document_key(&raw_rows[0]), document_fingerprint);
        assert!(effective_rows
            .iter()
            .all(|row| origin_document_key(row) == "document-key"));
        assert_ne!(
            prepared.content.root_table_sha256,
            prepared.content.effective_root_table_sha256,
        );
        assert_ne!(
            prepared.content.provenance_manifest_sha256,
            prepared.content.effective_origin_manifest_sha256,
        );

        let mut limited = limits;
        limited.max_origin_entries = 1;
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(
            cancellation.clone(),
            limited.deadline,
            limited.cancellation_stride,
        );
        let mut session =
            Session::new(&mut guard, &limited, anonymous.len()).expect("limited session");
        let error = parse_rdfxml_retained_v2(
            anonymous,
            None,
            &mut session,
            limited,
            cancellation,
            None,
            anonymous.len(),
            true,
            false,
            true,
            false,
        )
        .err()
        .expect("effective fallback must consume the origin limit");
        assert_eq!(error.code, "NATIVE_WIRE_LIMIT");
        assert_eq!(
            error.message,
            "native retained publication exceeds max_origin_entries",
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
            false,
            true,
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
        let document = parse_rdfxml(source, None, true, &mut session).expect("mapped document");
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
