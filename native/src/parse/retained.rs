//! Compact owner-first publication preparation for retained Functional loads.
//!
//! The optimized path never exports canonical ontology rows or fingerprint
//! preimages to Python.  It returns bounded metadata, then streams canonical
//! temporaries from the retained component arena into native digest state.

use std::collections::{BTreeMap, VecDeque};
use std::mem::size_of;

use crate::cancel::{Cancellation, InterruptSlot};
use crate::canonical::{iri, Node, LEXICAL_KEY, PROVISIONAL_SCOPE};
use crate::error::{NativeError, NativeResult};
use crate::hash::Sha256;
use crate::limits::{LimitKey, Limits};
use crate::model::{
    canonical_contains_tag, canonical_field_count, structural_digest_v1, ScanBudget,
};
use crate::publication::{
    TypedFacadeCollectionV2, TypedFacadeScopeV2, TypedFacadeStorageV2, TypedRdfReportRowsV2,
    TypedSourceMapRowsV2, AUXILIARY_CODEC_SCHEMA_SHA256_V2,
};

use super::{ParsedDocument, Span, SpannedNode};

pub(crate) const RETAINED_SEED_MAGIC_V2: &[u8; 8] = b"PYNFRS2\0";
pub(crate) const RETAINED_RDFXML_SEED_MAGIC_V2: &[u8; 8] = b"PYNRRS2\0";
pub(crate) const RETAINED_PREPARED_MAGIC_V2: &[u8; 8] = b"PYNFPP2\0";
const RETAINED_SEED_SCHEMA_V2: u16 = 1;
const RETAINED_PREPARED_SCHEMA_V2: u16 = 3;

const DOCUMENT_FINGERPRINT_DOMAIN_V1: &[u8] = b"pyowl-core:document-fingerprint:v1\0";
const STRUCTURAL_FINGERPRINT_DOMAIN_V1: &[u8] = b"pyowl-core:snapshot-structural:v1\0";
const LOGICAL_FINGERPRINT_DOMAIN_V1: &[u8] = b"pyowl-core:snapshot-logical:v1\0";
const LOGICAL_POLICY_V1: &[u8] = b"datatype-policy:owl2-v1\0";
const SIGNATURE_FINGERPRINT_DOMAIN_V1: &[u8] = b"pyowl-core:snapshot-signature:v1\0";
const RECORD_INVENTORY_DOMAIN_V1: &[u8] = b"pyowl-core:comparator-record-inventory:v1\0";

const ROOT_TABLE_MANIFEST_DOMAIN_V2: &[u8] = b"pyowl-core:native-root-table-manifest:v2";
const DOCUMENT_ROOT_TABLE_DOMAIN_V2: &[u8] = b"pyowl-core:native-document-root-table:v2";
const EFFECTIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2: &[u8] =
    b"pyowl-core:native-effective-root-table-manifest:v2";
const EFFECTIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2: &[u8] =
    b"pyowl-core:native-effective-document-root-table:v2";
const FINGERPRINT_INPUTS_MANIFEST_DOMAIN_V2: &[u8] =
    b"pyowl-core:native-fingerprint-inputs-manifest:v2";
const SOURCE_MANIFEST_DOMAIN_V2: &[u8] = b"pyowl-core:native-source-manifest:v2";
const DOCUMENT_SOURCE_TABLE_DOMAIN_V2: &[u8] = b"pyowl-core:native-document-source-table:v2";
const PROVENANCE_MANIFEST_DOMAIN_V2: &[u8] = b"pyowl-core:native-provenance-manifest:v2";
const DOCUMENT_ORIGIN_TABLE_DOMAIN_V2: &[u8] = b"pyowl-core:native-document-origin-table:v2";
const EFFECTIVE_ORIGIN_MANIFEST_DOMAIN_V2: &[u8] =
    b"pyowl-core:native-effective-origin-manifest:v2";
const EFFECTIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2: &[u8] =
    b"pyowl-core:native-effective-document-origin-table:v2";
const EFFECTIVE_CLOSURE_ORIGIN_TABLE_DOMAIN_V2: &[u8] =
    b"pyowl-core:native-effective-closure-origin-table:v2";
const RDF_MAPPING_REPORT_DOMAIN_V2: &[u8] = b"pyowl-core:native-rdf-mapping-report:v2";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct FingerprintEvidenceV2 {
    pub(crate) preimage_bytes: u64,
    pub(crate) digest: [u8; 32],
}

#[derive(Clone, Debug)]
pub(crate) struct RetainedOccurrenceV2 {
    digest: [u8; 32],
    effective_digest: [u8; 32],
    span: Option<Span>,
    source_order: u64,
    language_details: Vec<RetainedLanguageDetailV2>,
    source_blank_labels: Vec<String>,
}

#[derive(Clone, Debug)]
struct RetainedLanguageDetailV2 {
    digest: [u8; 32],
    spelling: String,
}

#[derive(Debug)]
pub(crate) struct RetainedParseMetadataV2 {
    pub(crate) document_fingerprint: FingerprintEvidenceV2,
    pub(crate) occurrence_count: u64,
    pub(crate) root_counts: [u64; 3],
    occurrences: Vec<RetainedOccurrenceV2>,
    effective_origin_fallbacks: Vec<([u8; 32], u64)>,
    source_prefixes: Option<Vec<(String, String)>>,
    rdf_mapping: Option<RetainedRdfMappingEvidenceV2>,
    scoped_roots: bool,
}

#[derive(Debug)]
struct RetainedRdfMappingEvidenceV2 {
    consumed_triples: u64,
    total_triples: u64,
    unconsumed: Vec<crate::bindings::ingestion::engine::RdfTripleEvidence>,
}

type StructuralRowsV2 = [Vec<Vec<u8>>; 3];
type MaterializedStructuralRowsV2 = (StructuralRowsV2, Option<StructuralRowsV2>);
type RetainedSeedV2 = (
    Vec<u8>,
    RetainedParseMetadataV2,
    StructuralRowsV2,
    Option<StructuralRowsV2>,
);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct RetainedContentDigestsV2 {
    pub(crate) root_table_sha256: [u8; 32],
    pub(crate) effective_root_table_sha256: [u8; 32],
    pub(crate) fingerprint_inputs_sha256: [u8; 32],
    pub(crate) source_manifest_sha256: [u8; 32],
    pub(crate) provenance_manifest_sha256: [u8; 32],
    pub(crate) effective_origin_manifest_sha256: [u8; 32],
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct RecordInventoryEvidenceV1 {
    pub(crate) count: u64,
    pub(crate) canonical_bytes: u64,
    pub(crate) transcript_bytes: u64,
    pub(crate) digest: [u8; 32],
}

#[derive(Debug)]
pub(crate) struct PreparedRetainedPublicationV2 {
    pub(crate) document_fingerprint: FingerprintEvidenceV2,
    pub(crate) structural_fingerprint: FingerprintEvidenceV2,
    pub(crate) logical_fingerprint: FingerprintEvidenceV2,
    pub(crate) signature_fingerprint: FingerprintEvidenceV2,
    pub(crate) content: RetainedContentDigestsV2,
    pub(crate) record_inventories: [RecordInventoryEvidenceV1; 4],
    pub(crate) root_count: u64,
    pub(crate) node_count: u64,
    pub(crate) source_map: Option<TypedSourceMapRowsV2>,
    pub(crate) origin_rows: Option<Vec<Vec<u8>>>,
    pub(crate) raw_origin_rows: Option<Vec<Vec<u8>>>,
    pub(crate) rdf_report: Option<PreparedRetainedRdfReportV2>,
    pub(crate) max_facade_row_bytes: u64,
    pub(crate) canonical_rows_encoded: u64,
    pub(crate) canonical_bytes_encoded: u64,
    pub(crate) fingerprint_temporary_bytes: u64,
    pub(crate) origin_bytes_retained: u64,
    pub(crate) document_key: Box<str>,
    pub(crate) scoped_roots: bool,
}

#[derive(Debug)]
pub(crate) struct PreparedRetainedRdfReportV2 {
    pub(crate) rows: TypedRdfReportRowsV2,
    pub(crate) conformant: bool,
    pub(crate) consumed_triples: u64,
    pub(crate) total_triples: u64,
    pub(crate) digest: [u8; 32],
    pub(crate) retained_bytes: u64,
}

impl PreparedRetainedPublicationV2 {
    pub(crate) fn encode_summary(&self, prepare_ns: u64) -> NativeResult<Vec<u8>> {
        let mut output = Vec::new();
        output
            .try_reserve_exact(640)
            .map_err(|_| NativeError::limit("native retained summary allocation failed"))?;
        append(&mut output, RETAINED_PREPARED_MAGIC_V2)?;
        append(&mut output, &RETAINED_PREPARED_SCHEMA_V2.to_le_bytes())?;
        let flags = u16::from(self.rdf_report.is_some()) | (u16::from(self.scoped_roots) << 1);
        append(&mut output, &flags.to_le_bytes())?;
        for evidence in [
            self.document_fingerprint,
            self.structural_fingerprint,
            self.logical_fingerprint,
            self.signature_fingerprint,
        ] {
            append_u64(&mut output, evidence.preimage_bytes)?;
            append(&mut output, &evidence.digest)?;
        }
        for digest in [
            self.content.root_table_sha256,
            self.content.effective_root_table_sha256,
            self.content.fingerprint_inputs_sha256,
            self.content.source_manifest_sha256,
            self.content.provenance_manifest_sha256,
            self.content.effective_origin_manifest_sha256,
        ] {
            append(&mut output, &digest)?;
        }
        for inventory in self.record_inventories {
            append_u64(&mut output, inventory.count)?;
            append_u64(&mut output, inventory.canonical_bytes)?;
            append_u64(&mut output, inventory.transcript_bytes)?;
            append(&mut output, &inventory.digest)?;
        }
        append_u64(&mut output, self.root_count)?;
        append_u64(&mut output, self.node_count)?;
        let origin_rows = self.origin_rows.as_ref().map_or(Ok(0_u64), |rows| {
            u64::try_from(rows.len())
                .map_err(|_| NativeError::limit("native retained origin count exceeds u64"))
        })?;
        let source_map_entries = self.source_map.as_ref().map_or(Ok(0_u64), |source| {
            u64::try_from(source.entries.len())
                .map_err(|_| NativeError::limit("native retained source-map count exceeds u64"))
        })?;
        let source_prefixes = self.source_map.as_ref().map_or(Ok(0_u64), |source| {
            u64::try_from(source.prefixes.len())
                .map_err(|_| NativeError::limit("native retained source-prefix count exceeds u64"))
        })?;
        append_u64(&mut output, source_map_entries)?;
        append_u64(&mut output, source_prefixes)?;
        append_u64(&mut output, origin_rows)?;
        append_u64(&mut output, self.max_facade_row_bytes)?;
        append_u64(&mut output, self.canonical_rows_encoded)?;
        append_u64(&mut output, self.canonical_bytes_encoded)?;
        append_u64(&mut output, self.fingerprint_temporary_bytes)?;
        append_u64(&mut output, self.origin_bytes_retained)?;
        append_u64(&mut output, prepare_ns)?;
        if let Some(report) = &self.rdf_report {
            append(&mut output, &[u8::from(report.conformant)])?;
            append_u64(&mut output, report.consumed_triples)?;
            append_u64(&mut output, report.total_triples)?;
            append_u64(
                &mut output,
                u64::try_from(report.rows.unconsumed_triples.len()).map_err(|_| {
                    NativeError::limit("native RDF unconsumed row count exceeds u64")
                })?,
            )?;
            append_u64(
                &mut output,
                u64::try_from(report.rows.rule_ids.len())
                    .map_err(|_| NativeError::limit("native RDF rule count exceeds u64"))?,
            )?;
            append_u64(
                &mut output,
                u64::try_from(report.rows.diagnostics.len())
                    .map_err(|_| NativeError::limit("native RDF diagnostic count exceeds u64"))?,
            )?;
            append(&mut output, &report.digest)?;
            append_u64(&mut output, report.retained_bytes)?;
        }
        Ok(output)
    }
}

impl RetainedParseMetadataV2 {
    pub(crate) fn render_rdf_literal_evidence<E>(
        &mut self,
        mut render: impl FnMut(&str) -> Result<String, E>,
    ) -> Result<(), E> {
        let Some(mapping) = &mut self.rdf_mapping else {
            return Ok(());
        };
        for triple in &mut mapping.unconsumed {
            if triple.object_requires_repr {
                triple.object = render(&triple.object)?;
                triple.object_requires_repr = false;
            }
        }
        Ok(())
    }

    pub(crate) fn retained_bytes(&self) -> NativeResult<usize> {
        let mut retained = self
            .occurrences
            .capacity()
            .checked_mul(size_of::<RetainedOccurrenceV2>())
            .ok_or_else(|| NativeError::limit("native retained parser metadata overflow"))?;
        for occurrence in &self.occurrences {
            retained = retained
                .checked_add(
                    occurrence
                        .language_details
                        .capacity()
                        .checked_mul(size_of::<RetainedLanguageDetailV2>())
                        .ok_or_else(|| {
                            NativeError::limit("native retained language metadata overflow")
                        })?,
                )
                .ok_or_else(|| NativeError::limit("native retained language metadata overflow"))?;
            for detail in &occurrence.language_details {
                retained = retained
                    .checked_add(detail.spelling.capacity())
                    .ok_or_else(|| {
                        NativeError::limit("native retained language spelling overflow")
                    })?;
            }
            retained = retained
                .checked_add(
                    occurrence
                        .source_blank_labels
                        .capacity()
                        .checked_mul(size_of::<String>())
                        .ok_or_else(|| {
                            NativeError::limit("native retained blank-label metadata overflow")
                        })?,
                )
                .ok_or_else(|| {
                    NativeError::limit("native retained blank-label metadata overflow")
                })?;
            for label in &occurrence.source_blank_labels {
                retained = retained.checked_add(label.capacity()).ok_or_else(|| {
                    NativeError::limit("native retained blank-label spelling overflow")
                })?;
            }
        }
        retained = retained
            .checked_add(
                self.effective_origin_fallbacks
                    .capacity()
                    .checked_mul(size_of::<([u8; 32], u64)>())
                    .ok_or_else(|| {
                        NativeError::limit("native retained origin fallback metadata overflow")
                    })?,
            )
            .ok_or_else(|| {
                NativeError::limit("native retained origin fallback metadata overflow")
            })?;
        if let Some(rows) = &self.source_prefixes {
            retained = rows.iter().try_fold(
                retained
                    .checked_add(
                        rows.capacity()
                            .checked_mul(size_of::<(String, String)>())
                            .ok_or_else(|| {
                                NativeError::limit(
                                    "native retained source-prefix metadata overflow",
                                )
                            })?,
                    )
                    .ok_or_else(|| {
                        NativeError::limit("native retained source-prefix metadata overflow")
                    })?,
                |total, (prefix, iri)| {
                    total
                        .checked_add(prefix.capacity())
                        .and_then(|value| value.checked_add(iri.capacity()))
                        .ok_or_else(|| {
                            NativeError::limit("native retained source-prefix metadata overflow")
                        })
                },
            )?;
        }
        if let Some(mapping) = &self.rdf_mapping {
            retained = retained
                .checked_add(
                    mapping
                        .unconsumed
                        .capacity()
                        .checked_mul(size_of::<
                            crate::bindings::ingestion::engine::RdfTripleEvidence,
                        >())
                        .ok_or_else(|| {
                            NativeError::limit("native retained RDF metadata overflow")
                        })?,
                )
                .ok_or_else(|| NativeError::limit("native retained RDF metadata overflow"))?;
            for triple in &mapping.unconsumed {
                retained = retained
                    .checked_add(triple.subject.capacity())
                    .and_then(|value| value.checked_add(triple.predicate.capacity()))
                    .and_then(|value| value.checked_add(triple.object.capacity()))
                    .ok_or_else(|| NativeError::limit("native retained RDF metadata overflow"))?;
            }
        }
        Ok(retained)
    }
}

/// Build bounded publication evidence from canonical rows produced by a
/// non-Functional native mapper. Structural rows stay in Rust and only this
/// compact seed crosses the Python boundary.
#[allow(clippy::too_many_arguments)]
pub(crate) fn build_rdfxml_seed(
    ontology_iri: Option<&str>,
    version_iri: Option<&str>,
    imports: &[String],
    rows: [&[Vec<u8>]; 3],
    decoded_codepoints: u64,
    total_triples: u64,
    consumed_triples: u64,
    unconsumed: Vec<crate::bindings::ingestion::engine::RdfTripleEvidence>,
    occurrence_count: u64,
    occurrence_rows: &[crate::bindings::ingestion::engine::CanonicalOccurrence],
    language_spellings: Vec<String>,
    source_blank_labels: Vec<String>,
    source_prefixes: Vec<(String, String)>,
    collect_occurrences: bool,
    preserve_source_map: bool,
    scoped_roots: bool,
    scoped_occurrence_digests: Option<&[([u8; 32], [u8; 32])]>,
    effective_origin_fallbacks: Vec<([u8; 32], u64)>,
    limits: &Limits,
    cancellation: &Cancellation,
) -> NativeResult<(Vec<u8>, RetainedParseMetadataV2)> {
    let unconsumed_count = total_triples
        .checked_sub(consumed_triples)
        .ok_or_else(|| NativeError::protocol("native RDF consumed count exceeds total"))?;
    if (unconsumed_count == 0) != unconsumed.is_empty()
        || u64::try_from(unconsumed.len()).map_or(true, |count| count > unconsumed_count)
    {
        return Err(NativeError::protocol(
            "native RDF partial-mapping evidence diverges from its counts",
        ));
    }
    if scoped_occurrence_digests.is_some() && !scoped_roots {
        return Err(NativeError::protocol(
            "native RDF/XML occurrence scopes require scoped root ownership",
        ));
    }
    if !scoped_roots && !effective_origin_fallbacks.is_empty() {
        return Err(NativeError::protocol(
            "native RDF/XML origin fallbacks require scoped root ownership",
        ));
    }
    if !collect_occurrences && !effective_origin_fallbacks.is_empty() {
        return Err(NativeError::protocol(
            "native RDF/XML origin fallbacks were captured while disabled",
        ));
    }
    let ontology_node = ontology_iri
        .map(|value| iri(value.to_owned()))
        .transpose()?;
    let version_node = version_iri.map(|value| iri(value.to_owned())).transpose()?;
    let mut import_nodes = imports
        .iter()
        .map(|value| iri(value.clone()))
        .collect::<NativeResult<Vec<_>>>()?;
    import_nodes.sort_unstable_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
    import_nodes.dedup_by(|left, right| left.as_bytes() == right.as_bytes());
    let document_fingerprint =
        document_fingerprint_slices(&ontology_node, &version_node, &import_nodes, rows)?;
    let root_counts = [
        u64::try_from(rows[0].len())
            .map_err(|_| NativeError::limit("native RDF/XML annotation count exceeds u64"))?,
        u64::try_from(rows[1].len())
            .map_err(|_| NativeError::limit("native RDF/XML axiom count exceeds u64"))?,
        u64::try_from(rows[2].len())
            .map_err(|_| NativeError::limit("native RDF/XML extension count exceeds u64"))?,
    ];
    let metadata_iri_objects = u64::from(ontology_iri.is_some())
        .checked_add(u64::from(version_iri.is_some()))
        .and_then(|value| value.checked_add(u64::try_from(import_nodes.len()).ok()?))
        .ok_or_else(|| NativeError::limit("native RDF/XML metadata IRI count overflow"))?;
    let canonical_rows_scanned = occurrence_count
        .checked_add(metadata_iri_objects)
        .ok_or_else(|| NativeError::limit("native RDF/XML canonical row count overflow"))?;
    let occurrences = rdfxml_retained_occurrences(
        occurrence_rows,
        RdfXmlOccurrenceCaptureV2 {
            count: occurrence_count,
            collect: collect_occurrences,
            scoped_digests: scoped_occurrence_digests,
            language_spellings,
            source_blank_labels,
            preserve_source_map,
            limits,
            cancellation,
        },
    )?;

    let mut encoded = Vec::new();
    append(&mut encoded, RETAINED_RDFXML_SEED_MAGIC_V2)?;
    append(&mut encoded, &RETAINED_SEED_SCHEMA_V2.to_le_bytes())?;
    append(&mut encoded, &0_u16.to_le_bytes())?;
    for value in [
        decoded_codepoints,
        canonical_rows_scanned,
        occurrence_count,
        root_counts[0],
        root_counts[1],
        root_counts[2],
        metadata_iri_objects,
        document_fingerprint.preimage_bytes,
    ] {
        append_u64(&mut encoded, value)?;
    }
    append(&mut encoded, &document_fingerprint.digest)?;
    append_optional_text(&mut encoded, ontology_iri)?;
    append_optional_text(&mut encoded, version_iri)?;
    append_u64(
        &mut encoded,
        u64::try_from(import_nodes.len())
            .map_err(|_| NativeError::limit("native RDF/XML import count exceeds u64"))?,
    )?;
    for value in &import_nodes {
        append_text64(&mut encoded, iri_text(value.as_bytes())?)?;
    }
    append_u64(&mut encoded, total_triples)?;
    Ok((
        encoded,
        RetainedParseMetadataV2 {
            document_fingerprint,
            occurrence_count,
            root_counts,
            occurrences,
            effective_origin_fallbacks,
            source_prefixes: preserve_source_map.then_some(source_prefixes),
            rdf_mapping: Some(RetainedRdfMappingEvidenceV2 {
                consumed_triples,
                total_triples,
                unconsumed,
            }),
            scoped_roots,
        },
    ))
}

pub(crate) fn contains_anonymous_rows(
    rows: [&[Vec<u8>]; 3],
    limits: &Limits,
) -> NativeResult<bool> {
    let mut budget = ScanBudget::from_limits(limits);
    for row in rows.into_iter().flatten() {
        if canonical_contains_tag(row, &mut budget, 3)? {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(crate) fn contains_anonymous(parsed: &ParsedDocument, limits: &Limits) -> NativeResult<bool> {
    let mut budget = ScanBudget::from_limits(limits);
    for value in parsed
        .annotations
        .iter()
        .chain(&parsed.axioms)
        .chain(&parsed.extensions)
    {
        if canonical_contains_tag(value.node.as_bytes(), &mut budget, 3)? {
            return Ok(true);
        }
    }
    Ok(false)
}

pub(crate) fn materialized_structural_rows(
    mut parsed: ParsedDocument,
    scope_anonymous: bool,
    cancellation: &Cancellation,
    session: &mut crate::session::Session<'_>,
) -> NativeResult<MaterializedStructuralRowsV2> {
    let occurrence_rows = [
        occurrence_root_rows(parsed.annotations),
        occurrence_root_rows(parsed.axioms),
        occurrence_root_rows(parsed.extensions),
    ];
    if !scope_anonymous {
        return Ok((occurrence_rows.map(canonicalize_root_rows), None));
    }
    parsed
        .imports
        .sort_unstable_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
    parsed
        .imports
        .dedup_by(|left, right| left.as_bytes() == right.as_bytes());
    let scoped = super::anonymous::scope_functional_anonymous_rows_v2(
        &parsed.ontology_iri,
        &parsed.version_iri,
        &parsed.imports,
        [
            occurrence_rows[0].as_slice(),
            occurrence_rows[1].as_slice(),
            occurrence_rows[2].as_slice(),
        ],
        session,
        cancellation,
    )?;
    Ok((scoped.raw, Some(scoped.effective)))
}

pub(crate) fn build_seed(
    mut parsed: ParsedDocument,
    collect_provenance: bool,
    preserve_source_map: bool,
    limits: &Limits,
    cancellation: &Cancellation,
    session: &mut crate::session::Session<'_>,
    scope_anonymous: bool,
) -> NativeResult<RetainedSeedV2> {
    let occurrence_count = total_occurrences(&parsed)?;
    let language_spellings = std::mem::take(&mut parsed.language_spellings);
    let mut occurrences = retained_occurrences(
        &parsed,
        occurrence_count,
        collect_provenance || preserve_source_map,
        preserve_source_map,
        language_spellings,
        limits,
        cancellation,
    )?;
    let ParsedDocument {
        ontology_iri,
        version_iri,
        mut imports,
        annotations,
        axioms,
        extensions,
        prefixes,
        decoded_codepoints,
        language_spellings: _,
    } = parsed;
    let raw_import_count = imports.len();
    imports.sort_unstable_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
    imports.dedup_by(|left, right| left.as_bytes() == right.as_bytes());
    let occurrence_rows = [
        occurrence_root_rows(annotations),
        occurrence_root_rows(axioms),
        occurrence_root_rows(extensions),
    ];
    let (rows, effective_rows, scoped_roots) = if scope_anonymous {
        let scoped = super::anonymous::scope_functional_anonymous_rows_v2(
            &ontology_iri,
            &version_iri,
            &imports,
            [
                occurrence_rows[0].as_slice(),
                occurrence_rows[1].as_slice(),
                occurrence_rows[2].as_slice(),
            ],
            session,
            cancellation,
        )?;
        apply_scoped_occurrence_digests(
            &mut occurrences,
            occurrence_count,
            &scoped.source_occurrence_digests,
        )?;
        (scoped.raw, Some(scoped.effective), true)
    } else {
        (occurrence_rows.map(canonicalize_root_rows), None, false)
    };
    let ontology = ontology_iri
        .as_ref()
        .map(|value| iri_text(value.as_bytes()))
        .transpose()?;
    let version = version_iri
        .as_ref()
        .map(|value| iri_text(value.as_bytes()))
        .transpose()?;
    let document_fingerprint = document_fingerprint(&ontology_iri, &version_iri, &imports, &rows)?;
    let root_counts = [
        u64::try_from(rows[0].len())
            .map_err(|_| NativeError::limit("native annotation count exceeds u64"))?,
        u64::try_from(rows[1].len())
            .map_err(|_| NativeError::limit("native axiom count exceeds u64"))?,
        u64::try_from(rows[2].len())
            .map_err(|_| NativeError::limit("native extension count exceeds u64"))?,
    ];
    let metadata_rows = u64::from(ontology_iri.is_some())
        .checked_add(u64::from(version_iri.is_some()))
        .and_then(|value| value.checked_add(u64::try_from(raw_import_count).ok()?))
        .ok_or_else(|| NativeError::limit("native metadata row count overflow"))?;
    let canonical_rows_scanned = metadata_rows
        .checked_add(occurrence_count)
        .ok_or_else(|| NativeError::limit("native canonical row count overflow"))?;
    let metadata_iri_objects = u64::from(ontology.is_some())
        .checked_add(u64::from(version.is_some()))
        .and_then(|value| value.checked_add(u64::try_from(imports.len()).ok()?))
        .ok_or_else(|| NativeError::limit("native metadata IRI count overflow"))?;
    let mut encoded = Vec::new();
    append(&mut encoded, RETAINED_SEED_MAGIC_V2)?;
    append(&mut encoded, &RETAINED_SEED_SCHEMA_V2.to_le_bytes())?;
    append(&mut encoded, &0_u16.to_le_bytes())?;
    for value in [
        decoded_codepoints,
        canonical_rows_scanned,
        occurrence_count,
        root_counts[0],
        root_counts[1],
        root_counts[2],
        metadata_iri_objects,
        document_fingerprint.preimage_bytes,
    ] {
        append_u64(&mut encoded, value)?;
    }
    append(&mut encoded, &document_fingerprint.digest)?;
    append_optional_text(&mut encoded, ontology)?;
    append_optional_text(&mut encoded, version)?;
    append_u64(
        &mut encoded,
        u64::try_from(imports.len())
            .map_err(|_| NativeError::limit("native retained import count exceeds u64"))?,
    )?;
    for value in &imports {
        append_text64(&mut encoded, iri_text(value.as_bytes())?)?;
    }
    Ok((
        encoded,
        RetainedParseMetadataV2 {
            document_fingerprint,
            occurrence_count,
            root_counts,
            occurrences,
            effective_origin_fallbacks: Vec::new(),
            source_prefixes: preserve_source_map.then_some(prefixes),
            rdf_mapping: None,
            scoped_roots,
        },
        rows,
        effective_rows,
    ))
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn prepare_publication(
    storage: &TypedFacadeStorageV2,
    metadata: &RetainedParseMetadataV2,
    manifest: &[u8],
    document_key: &str,
    collect_provenance: bool,
    preserve_source_map: bool,
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
) -> NativeResult<PreparedRetainedPublicationV2> {
    if document_key.is_empty() || !document_key.is_ascii() {
        return Err(NativeError::protocol(
            "native retained publication received an invalid document key",
        ));
    }
    let observed = storage.structural_counts()?;
    if observed.ontology_annotations != metadata.root_counts[0]
        || observed.stored_axioms != metadata.root_counts[1]
        || observed.effective_axioms != metadata.root_counts[1]
        || observed.extensions != metadata.root_counts[2]
    {
        return Err(NativeError::protocol(
            "native retained publication metadata diverges from its arena",
        ));
    }
    let captures_occurrences = collect_provenance || preserve_source_map;
    if captures_occurrences {
        if u64::try_from(metadata.occurrences.len()).ok() != Some(metadata.occurrence_count) {
            return Err(NativeError::protocol(
                "native retained auxiliary occurrences are incomplete",
            ));
        }
        if collect_provenance {
            let effective_origin_count = metadata
                .occurrence_count
                .checked_add(
                    u64::try_from(metadata.effective_origin_fallbacks.len()).map_err(|_| {
                        NativeError::limit("native retained origin fallback count exceeds u64")
                    })?,
                )
                .ok_or_else(|| NativeError::limit("native retained origin count overflow"))?;
            if effective_origin_count > limits.max_origin_entries {
                return Err(NativeError::limit(
                    "native retained publication exceeds max_origin_entries",
                ));
            }
        }
        if preserve_source_map && source_map_row_count(metadata)? > limits.max_source_map_entries {
            return Err(NativeError::limit(
                "native retained publication exceeds max_source_map_entries",
            ));
        }
    } else if !metadata.occurrences.is_empty() {
        return Err(NativeError::protocol(
            "native retained auxiliary occurrences were prepared while disabled",
        ));
    }
    if metadata.source_prefixes.is_some() != preserve_source_map {
        return Err(NativeError::protocol(
            "native retained source-map metadata diverges from publication options",
        ));
    }
    let storage_counters = storage.counters()?;
    let node_count = storage_counters.component.unique_nodes;
    let root_count = metadata
        .root_counts
        .into_iter()
        .try_fold(0_u64, |total, count| {
            checked_add(
                total,
                count,
                "native retained root inventory count overflow",
            )
        })?;

    let mut raw_document = MeasuredSha256::domain(DOCUMENT_ROOT_TABLE_DOMAIN_V2)?;
    let mut effective_document = MeasuredSha256::domain(EFFECTIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2)?;
    raw_document.text64(document_key)?;
    effective_document.text64(document_key)?;
    let mut structural = MeasuredSha256::new();
    structural.update(STRUCTURAL_FINGERPRINT_DOMAIN_V1)?;
    structural.frame_varint(manifest)?;
    structural.frame_varint(document_key.as_bytes())?;
    let mut logical_axioms = Vec::new();
    logical_axioms
        .try_reserve_exact(
            usize::try_from(metadata.root_counts[1])
                .map_err(|_| NativeError::limit("native retained axiom count exceeds usize"))?,
        )
        .map_err(|_| NativeError::limit("native logical axiom workspace allocation failed"))?;
    let mut logical_extensions = Vec::new();
    logical_extensions
        .try_reserve_exact(
            usize::try_from(metadata.root_counts[2])
                .map_err(|_| NativeError::limit("native retained extension count exceeds usize"))?,
        )
        .map_err(|_| NativeError::limit("native logical extension workspace allocation failed"))?;
    let mut canonical_rows_encoded = 0_u64;
    let mut canonical_bytes_encoded = 0_u64;
    let mut record_inventories = [RecordInventoryEvidenceV1::default(); 4];

    for (tag, collection, expected) in [
        (
            1_u8,
            TypedFacadeCollectionV2::OntologyAnnotations,
            metadata.root_counts[0],
        ),
        (
            2_u8,
            TypedFacadeCollectionV2::Axioms,
            metadata.root_counts[1],
        ),
        (
            3_u8,
            TypedFacadeCollectionV2::Extensions,
            metadata.root_counts[2],
        ),
    ] {
        raw_document.update(&[tag])?;
        raw_document.u64_le(expected)?;
        effective_document.update(&[tag])?;
        effective_document.u64_le(expected)?;
        structural.varint(expected)?;
        let mut inventory = MeasuredSha256::new();
        inventory.update(RECORD_INVENTORY_DOMAIN_V1)?;
        inventory.varint(expected)?;
        let mut inventory_canonical_bytes = 0_u64;
        let mut emitted = 0_u64;
        if metadata.scoped_roots {
            let mut raw_emitted = 0_u64;
            storage.visit_canonical_roots(
                collection,
                TypedFacadeScopeV2::Document,
                Some(0),
                true,
                cancellation.clone(),
                interrupt.clone(),
                |row| {
                    raw_emitted =
                        checked_add(raw_emitted, 1, "native retained raw root count overflow")?;
                    canonical_rows_encoded = checked_add(
                        canonical_rows_encoded,
                        1,
                        "native retained canonical row count overflow",
                    )?;
                    let row_bytes = u64::try_from(row.len()).map_err(|_| {
                        NativeError::limit("native retained canonical row exceeds u64")
                    })?;
                    canonical_bytes_encoded = checked_add(
                        canonical_bytes_encoded,
                        row_bytes,
                        "native retained canonical byte count overflow",
                    )?;
                    raw_document.frame64(row)
                },
            )?;
            if raw_emitted != expected {
                return Err(NativeError::protocol(
                    "native retained raw traversal diverges from its count",
                ));
            }
            storage.visit_canonical_roots(
                collection,
                TypedFacadeScopeV2::Document,
                Some(0),
                false,
                cancellation.clone(),
                interrupt.clone(),
                |row| {
                    emitted = checked_add(emitted, 1, "native retained root count overflow")?;
                    canonical_rows_encoded = checked_add(
                        canonical_rows_encoded,
                        1,
                        "native retained canonical row count overflow",
                    )?;
                    let row_bytes = u64::try_from(row.len()).map_err(|_| {
                        NativeError::limit("native retained canonical row exceeds u64")
                    })?;
                    canonical_bytes_encoded = checked_add(
                        canonical_bytes_encoded,
                        row_bytes,
                        "native retained canonical byte count overflow",
                    )?;
                    inventory_canonical_bytes = checked_add(
                        inventory_canonical_bytes,
                        row_bytes,
                        "native retained inventory canonical byte count overflow",
                    )?;
                    effective_document.frame64(row)?;
                    structural.frame_varint(row)?;
                    inventory.frame_varint(row)?;
                    if collection == TypedFacadeCollectionV2::Axioms
                        && is_logical_axiom(row_tag(row)?)
                    {
                        logical_axioms.push(without_annotations(row)?);
                    } else if collection == TypedFacadeCollectionV2::Extensions {
                        logical_extensions.push(without_annotations(row)?);
                    }
                    Ok(())
                },
            )?;
        } else {
            storage.visit_canonical_roots(
                collection,
                TypedFacadeScopeV2::Document,
                Some(0),
                true,
                cancellation.clone(),
                interrupt.clone(),
                |row| {
                    emitted = checked_add(emitted, 1, "native retained root count overflow")?;
                    canonical_rows_encoded = checked_add(
                        canonical_rows_encoded,
                        1,
                        "native retained canonical row count overflow",
                    )?;
                    let row_bytes = u64::try_from(row.len()).map_err(|_| {
                        NativeError::limit("native retained canonical row exceeds u64")
                    })?;
                    canonical_bytes_encoded = checked_add(
                        canonical_bytes_encoded,
                        row_bytes,
                        "native retained canonical byte count overflow",
                    )?;
                    inventory_canonical_bytes = checked_add(
                        inventory_canonical_bytes,
                        row_bytes,
                        "native retained inventory canonical byte count overflow",
                    )?;
                    raw_document.frame64(row)?;
                    effective_document.frame64(row)?;
                    structural.frame_varint(row)?;
                    inventory.frame_varint(row)?;
                    if collection == TypedFacadeCollectionV2::Axioms
                        && is_logical_axiom(row_tag(row)?)
                    {
                        logical_axioms.push(without_annotations(row)?);
                    } else if collection == TypedFacadeCollectionV2::Extensions {
                        logical_extensions.push(without_annotations(row)?);
                    }
                    Ok(())
                },
            )?;
        }
        if emitted != expected {
            return Err(NativeError::protocol(
                "native retained root traversal diverges from its count",
            ));
        }
        let inventory_evidence = inventory.finish();
        record_inventories[usize::from(tag - 1)] = RecordInventoryEvidenceV1 {
            count: emitted,
            canonical_bytes: inventory_canonical_bytes,
            transcript_bytes: inventory_evidence.preimage_bytes,
            digest: inventory_evidence.digest,
        };
    }

    let raw_document_digest = raw_document.finish().digest;
    let effective_document_digest = effective_document.finish().digest;
    let root_table_sha256 = root_manifest_digest(
        ROOT_TABLE_MANIFEST_DOMAIN_V2,
        document_key,
        metadata.root_counts,
        raw_document_digest,
    )?;
    let effective_root_table_sha256 = root_manifest_digest(
        EFFECTIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2,
        document_key,
        metadata.root_counts,
        effective_document_digest,
    )?;
    let structural_fingerprint = structural.finish();

    let fingerprint_temporary_bytes = logical_workspace_bytes(
        logical_axioms.capacity(),
        logical_extensions.capacity(),
        logical_axioms.iter().chain(&logical_extensions),
    )?;
    let encoded_row_workspace = storage.maximum_row_bytes();
    let temporary_workspace = fingerprint_temporary_bytes
        .checked_add(encoded_row_workspace)
        .ok_or_else(|| NativeError::limit("native retained fingerprint workspace overflow"))?;
    if temporary_workspace > limits.value(LimitKey::MaxTemporaryBytes) {
        return Err(NativeError::limit(
            "native retained fingerprint workspace exceeds max_temporary_bytes",
        ));
    }
    let retained_owner_bytes = storage_counters.retained_owner_bytes;
    let peak_live_bytes = retained_owner_bytes
        .checked_add(temporary_workspace)
        .ok_or_else(|| NativeError::limit("native retained publication memory overflow"))?;
    if limits
        .max_memory_bytes
        .is_some_and(|maximum| peak_live_bytes > maximum)
    {
        return Err(NativeError::limit(
            "native retained publication exceeds max_memory_bytes",
        ));
    }

    cancellation.checkpoint()?;
    logical_axioms.sort_unstable();
    logical_axioms.dedup();
    logical_extensions.sort_unstable();
    logical_extensions.dedup();
    cancellation.checkpoint()?;

    let mut logical = MeasuredSha256::new();
    logical.update(LOGICAL_FINGERPRINT_DOMAIN_V1)?;
    logical.update(LOGICAL_POLICY_V1)?;
    logical
        .varint(u64::try_from(logical_axioms.len()).map_err(|_| {
            NativeError::limit("native retained logical axiom count exceeds u64")
        })?)?;
    for row in &logical_axioms {
        cancellation.checkpoint()?;
        logical.frame_varint(row)?;
    }
    logical.varint(u64::try_from(logical_extensions.len()).map_err(|_| {
        NativeError::limit("native retained logical extension count exceeds u64")
    })?)?;
    for row in &logical_extensions {
        cancellation.checkpoint()?;
        logical.update(b"E")?;
        logical.frame_varint(row)?;
    }
    let logical_fingerprint = logical.finish();
    drop(logical_axioms);
    drop(logical_extensions);

    let signature_count = storage.canonical_root_count(
        TypedFacadeCollectionV2::Signature,
        TypedFacadeScopeV2::Closure,
        None,
        false,
    )?;
    let mut signature = MeasuredSha256::new();
    signature.update(SIGNATURE_FINGERPRINT_DOMAIN_V1)?;
    signature.update(&[1])?;
    signature.varint(signature_count)?;
    let mut signature_inventory = MeasuredSha256::new();
    signature_inventory.update(RECORD_INVENTORY_DOMAIN_V1)?;
    signature_inventory.varint(signature_count)?;
    let mut signature_canonical_bytes = 0_u64;
    let mut emitted_signature = 0_u64;
    storage.visit_canonical_roots(
        TypedFacadeCollectionV2::Signature,
        TypedFacadeScopeV2::Closure,
        None,
        false,
        cancellation.clone(),
        interrupt.clone(),
        |row| {
            emitted_signature = checked_add(
                emitted_signature,
                1,
                "native retained signature count overflow",
            )?;
            canonical_rows_encoded = checked_add(
                canonical_rows_encoded,
                1,
                "native retained canonical row count overflow",
            )?;
            let row_bytes = u64::try_from(row.len())
                .map_err(|_| NativeError::limit("native signature row exceeds u64"))?;
            canonical_bytes_encoded = checked_add(
                canonical_bytes_encoded,
                row_bytes,
                "native retained canonical byte count overflow",
            )?;
            signature_canonical_bytes = checked_add(
                signature_canonical_bytes,
                row_bytes,
                "native retained signature inventory byte count overflow",
            )?;
            signature.frame_varint(row)?;
            signature_inventory.frame_varint(row)
        },
    )?;
    if emitted_signature != signature_count {
        return Err(NativeError::protocol(
            "native retained signature traversal diverges from its count",
        ));
    }
    let signature_fingerprint = signature.finish();
    let signature_inventory_evidence = signature_inventory.finish();
    record_inventories[3] = RecordInventoryEvidenceV1 {
        count: emitted_signature,
        canonical_bytes: signature_canonical_bytes,
        transcript_bytes: signature_inventory_evidence.preimage_bytes,
        digest: signature_inventory_evidence.digest,
    };

    let source_map = if preserve_source_map {
        Some(encode_source_map_rows(metadata, limits, &cancellation)?)
    } else {
        None
    };
    let (origin_rows, raw_origin_rows, origin_bytes_retained) = if collect_provenance {
        let effective = encode_origin_rows(metadata, document_key, false, limits, &cancellation)?;
        let raw = metadata
            .scoped_roots
            .then(|| encode_origin_rows(metadata, document_key, true, limits, &cancellation))
            .transpose()?;
        let bytes = effective
            .iter()
            .chain(raw.as_deref().unwrap_or_default())
            .try_fold(0_u64, |total, row| {
                total.checked_add(u64::try_from(row.len()).ok()?)
            })
            .ok_or_else(|| NativeError::limit("native retained origin byte count overflow"))?;
        (Some(effective), raw, bytes)
    } else {
        (None, None, 0)
    };
    let selected_origins = origin_rows.as_deref().unwrap_or_default();
    let selected_raw_origins = raw_origin_rows.as_deref().unwrap_or(selected_origins);
    let rdf_report = metadata
        .rdf_mapping
        .as_ref()
        .map(|mapping| prepare_rdf_report(document_key, mapping, limits))
        .transpose()?;
    let source_manifest_sha256 = source_manifest_digest(document_key, source_map.as_ref())?;
    let provenance_manifest_sha256 = provenance_manifest_digest(
        document_key,
        origin_rows.is_some(),
        selected_raw_origins,
        rdf_report.as_ref(),
    )?;
    let effective_origin_manifest_sha256 =
        effective_origin_manifest_digest(document_key, selected_origins)?;
    let fingerprint_inputs_sha256 = fingerprint_inputs_digest(
        document_key,
        metadata.document_fingerprint,
        structural_fingerprint,
        logical_fingerprint,
        signature_fingerprint,
    )?;
    let origin_max =
        selected_origins
            .iter()
            .chain(selected_raw_origins)
            .try_fold(1_u64, |maximum, row| {
                Ok::<u64, NativeError>(maximum.max(
                    u64::try_from(row.len()).map_err(|_| {
                        NativeError::limit("native retained origin row exceeds u64")
                    })?,
                ))
            })?;
    let rdf_max = rdf_report.as_ref().map_or(1_u64, |report| {
        report
            .rows
            .unconsumed_triples
            .iter()
            .chain(&report.rows.rule_ids)
            .chain(&report.rows.diagnostics)
            .fold(report.rows.header.len() as u64, |maximum, row| {
                maximum.max(row.len() as u64)
            })
    });
    let source_max = source_map.as_ref().map_or(1_u64, |source| {
        source
            .entries
            .iter()
            .chain(&source.prefixes)
            .map(Vec::len)
            .max()
            .unwrap_or(1) as u64
    });
    cancellation.checkpoint()?;
    Ok(PreparedRetainedPublicationV2 {
        document_fingerprint: metadata.document_fingerprint,
        structural_fingerprint,
        logical_fingerprint,
        signature_fingerprint,
        content: RetainedContentDigestsV2 {
            root_table_sha256,
            effective_root_table_sha256,
            fingerprint_inputs_sha256,
            source_manifest_sha256,
            provenance_manifest_sha256,
            effective_origin_manifest_sha256,
        },
        record_inventories,
        root_count,
        node_count,
        source_map,
        origin_rows,
        raw_origin_rows,
        rdf_report,
        max_facade_row_bytes: storage
            .maximum_row_bytes()
            .max(source_max)
            .max(origin_max)
            .max(rdf_max),
        canonical_rows_encoded,
        canonical_bytes_encoded,
        fingerprint_temporary_bytes,
        origin_bytes_retained,
        document_key: document_key.into(),
        scoped_roots: metadata.scoped_roots,
    })
}

fn document_fingerprint(
    ontology_iri: &Option<Node>,
    version_iri: &Option<Node>,
    imports: &[Node],
    rows: &[Vec<Vec<u8>>; 3],
) -> NativeResult<FingerprintEvidenceV2> {
    document_fingerprint_slices(
        ontology_iri,
        version_iri,
        imports,
        [&rows[0], &rows[1], &rows[2]],
    )
}

fn document_fingerprint_slices(
    ontology_iri: &Option<Node>,
    version_iri: &Option<Node>,
    imports: &[Node],
    rows: [&[Vec<u8>]; 3],
) -> NativeResult<FingerprintEvidenceV2> {
    let mut hasher = MeasuredSha256::new();
    hasher.update(DOCUMENT_FINGERPRINT_DOMAIN_V1)?;
    for value in [ontology_iri.as_ref(), version_iri.as_ref()] {
        match value {
            Some(node) => {
                hasher.update(b"1")?;
                hasher.frame_varint(node.as_bytes())?;
            }
            None => hasher.update(b"0")?,
        }
    }
    hasher.varint(
        u64::try_from(imports.len())
            .map_err(|_| NativeError::limit("native import count exceeds u64"))?,
    )?;
    for value in imports {
        hasher.frame_varint(value.as_bytes())?;
    }
    for collection in rows {
        hasher.varint(
            u64::try_from(collection.len())
                .map_err(|_| NativeError::limit("native root count exceeds u64"))?,
        )?;
        for row in collection {
            hasher.frame_varint(row)?;
        }
    }
    Ok(hasher.finish())
}

pub(super) fn functional_document_fingerprint(
    ontology_iri: &Option<Node>,
    version_iri: &Option<Node>,
    imports: &[Node],
    rows: [&[Vec<u8>]; 3],
) -> NativeResult<FingerprintEvidenceV2> {
    document_fingerprint_slices(ontology_iri, version_iri, imports, rows)
}

pub(super) fn rdfxml_document_fingerprint(
    ontology_iri: Option<&str>,
    version_iri: Option<&str>,
    imports: &[String],
    rows: [&[Vec<u8>]; 3],
) -> NativeResult<FingerprintEvidenceV2> {
    let ontology_node = ontology_iri
        .map(|value| iri(value.to_owned()))
        .transpose()?;
    let version_node = version_iri.map(|value| iri(value.to_owned())).transpose()?;
    let mut import_nodes = imports
        .iter()
        .map(|value| iri(value.clone()))
        .collect::<NativeResult<Vec<_>>>()?;
    import_nodes.sort_unstable_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
    import_nodes.dedup_by(|left, right| left.as_bytes() == right.as_bytes());
    document_fingerprint_slices(&ontology_node, &version_node, &import_nodes, rows)
}

fn retained_occurrences(
    parsed: &ParsedDocument,
    count: u64,
    collect: bool,
    preserve_source_map: bool,
    language_spellings: Vec<String>,
    limits: &Limits,
    cancellation: &Cancellation,
) -> NativeResult<Vec<RetainedOccurrenceV2>> {
    if !collect {
        return Ok(Vec::new());
    }
    let capacity = usize::try_from(count)
        .map_err(|_| NativeError::limit("native occurrence count exceeds usize"))?;
    let mut result = Vec::new();
    result
        .try_reserve_exact(capacity)
        .map_err(|_| NativeError::limit("native occurrence allocation failed"))?;
    for (source_order, value) in parsed
        .annotations
        .iter()
        .chain(&parsed.axioms)
        .chain(&parsed.extensions)
        .enumerate()
    {
        result.push(RetainedOccurrenceV2 {
            digest: structural_digest_v1(value.node.as_bytes()),
            effective_digest: structural_digest_v1(value.node.as_bytes()),
            span: Some(value.span),
            source_order: u64::try_from(source_order)
                .map_err(|_| NativeError::limit("native occurrence ordinal exceeds u64"))?,
            language_details: Vec::new(),
            source_blank_labels: Vec::new(),
        });
    }
    result.sort_unstable_by_key(|value| {
        value
            .span
            .map_or((u64::MAX, u64::MAX, value.source_order), |span| {
                (span.byte_start, span.byte_end, value.source_order)
            })
    });
    if preserve_source_map {
        attach_language_details(
            parsed,
            &mut result,
            language_spellings,
            limits,
            cancellation,
        )?;
    } else if !language_spellings.is_empty() {
        return Err(NativeError::protocol(
            "native retained language spellings were captured while disabled",
        ));
    }
    Ok(result)
}

fn attach_language_details(
    parsed: &ParsedDocument,
    occurrences: &mut [RetainedOccurrenceV2],
    language_spellings: Vec<String>,
    limits: &Limits,
    cancellation: &Cancellation,
) -> NativeResult<()> {
    let mut by_language: BTreeMap<String, VecDeque<String>> = BTreeMap::new();
    for spelling in language_spellings {
        by_language
            .entry(spelling.to_ascii_lowercase())
            .or_default()
            .push_back(spelling);
    }
    let values: Vec<&SpannedNode> = parsed
        .annotations
        .iter()
        .chain(&parsed.axioms)
        .chain(&parsed.extensions)
        .collect();
    if values.len() != occurrences.len() {
        return Err(NativeError::protocol(
            "native retained occurrence roots diverge from source metadata",
        ));
    }
    let mut terms = 0_u64;
    let mut source_rows = u64::try_from(occurrences.len())
        .map_err(|_| NativeError::limit("native source-map count exceeds u64"))?;
    if source_rows > limits.max_source_map_entries {
        return Err(NativeError::limit(
            "native retained publication exceeds max_source_map_entries",
        ));
    }
    for occurrence in occurrences {
        cancellation.checkpoint()?;
        let source_order = usize::try_from(occurrence.source_order)
            .map_err(|_| NativeError::limit("native source order exceeds usize"))?;
        let row = values.get(source_order).ok_or_else(|| {
            NativeError::protocol("native retained source order is out of bounds")
        })?;
        let literals =
            canonical_language_literals(row.node.as_bytes(), limits, cancellation, &mut terms)?;
        occurrence
            .language_details
            .try_reserve_exact(literals.len())
            .map_err(|_| NativeError::limit("native language detail allocation failed"))?;
        for (digest, language) in literals {
            let Some(spelling) = by_language.get_mut(language).and_then(VecDeque::pop_front) else {
                continue;
            };
            source_rows = source_rows
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native source-map count overflow"))?;
            if source_rows > limits.max_source_map_entries {
                return Err(NativeError::limit(
                    "native retained publication exceeds max_source_map_entries",
                ));
            }
            occurrence
                .language_details
                .push(RetainedLanguageDetailV2 { digest, spelling });
        }
    }
    Ok(())
}

fn canonical_language_literals<'a>(
    row: &'a [u8],
    limits: &Limits,
    cancellation: &Cancellation,
    terms: &mut u64,
) -> NativeResult<Vec<([u8; 32], &'a str)>> {
    canonical_lexical_nodes(row, limits, cancellation, terms, false)
        .map(|details| details.language_literals)
}

struct CanonicalLexicalNodes<'a> {
    language_literals: Vec<([u8; 32], &'a str)>,
    blank_labels: Vec<&'a str>,
}

fn canonical_lexical_nodes<'a>(
    row: &'a [u8],
    limits: &Limits,
    cancellation: &Cancellation,
    terms: &mut u64,
    collect_blank_labels: bool,
) -> NativeResult<CanonicalLexicalNodes<'a>> {
    if u64::try_from(row.len()).map_or(true, |size| size > limits.max_canonical_work) {
        return Err(NativeError::limit(
            "native source-map scan exceeds max_canonical_work",
        ));
    }
    let mut language_literals = Vec::new();
    let mut blank_labels = Vec::new();
    let end = scan_lexical_node(
        row,
        0,
        row.len(),
        0,
        limits,
        cancellation,
        terms,
        collect_blank_labels,
        &mut language_literals,
        &mut blank_labels,
    )?;
    if end != row.len() {
        return Err(NativeError::protocol(
            "native source-map canonical row has trailing bytes",
        ));
    }
    Ok(CanonicalLexicalNodes {
        language_literals,
        blank_labels,
    })
}

#[allow(clippy::too_many_arguments)]
fn scan_lexical_node<'a>(
    data: &'a [u8],
    start: usize,
    bound: usize,
    depth: u32,
    limits: &Limits,
    cancellation: &Cancellation,
    terms: &mut u64,
    collect_blank_labels: bool,
    language_literals: &mut Vec<([u8; 32], &'a str)>,
    blank_labels: &mut Vec<&'a str>,
) -> NativeResult<usize> {
    cancellation.checkpoint()?;
    *terms = terms
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("native source-map term count overflow"))?;
    if depth > limits.max_nesting_depth.min(1024) || *terms > limits.max_terms {
        return Err(NativeError::limit(
            "native source-map canonical scan exceeds model limits",
        ));
    }
    let (tag, mut offset) = read_varint(data, start)?;
    let field_count = canonical_field_count(
        u16::try_from(tag).map_err(|_| NativeError::protocol("canonical tag exceeds u16"))?,
    )
    .ok_or_else(|| NativeError::protocol("canonical source-map tag is unknown"))?;
    let mut language = None;
    let mut anonymous_scope = None;
    let mut anonymous_key = None;
    for field in 0..field_count {
        let marker = *data
            .get(offset)
            .filter(|_| offset < bound)
            .ok_or_else(|| NativeError::protocol("canonical source-map component is truncated"))?;
        offset = offset
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("canonical source-map offset overflow"))?;
        match marker {
            0 => {}
            1 => {
                let (length, child_start) = read_varint(data, offset)?;
                let child_end = bounded_end(child_start, length, bound)?;
                let observed = scan_lexical_node(
                    data,
                    child_start,
                    child_end,
                    depth.saturating_add(1),
                    limits,
                    cancellation,
                    terms,
                    collect_blank_labels,
                    language_literals,
                    blank_labels,
                )?;
                if observed != child_end {
                    return Err(NativeError::protocol(
                        "canonical source-map child frame is invalid",
                    ));
                }
                offset = child_end;
            }
            2 | 3 | 5 => {
                let (length, value_start) = read_varint(data, offset)?;
                let value_end = bounded_end(value_start, length, bound)?;
                if tag == 4 && field == 2 {
                    if marker != 2 {
                        return Err(NativeError::protocol(
                            "canonical literal language has the wrong marker",
                        ));
                    }
                    language = Some(std::str::from_utf8(&data[value_start..value_end]).map_err(
                        |_| NativeError::protocol("canonical literal language is not UTF-8"),
                    )?);
                }
                if collect_blank_labels && tag == 3 && field < 2 {
                    if marker != 3 {
                        return Err(NativeError::protocol(
                            "canonical anonymous identity has the wrong marker",
                        ));
                    }
                    if field == 0 {
                        anonymous_scope = Some(&data[value_start..value_end]);
                    } else {
                        anonymous_key = Some(&data[value_start..value_end]);
                    }
                }
                offset = value_end;
            }
            4 => {
                offset = read_varint(data, offset)?.1;
            }
            6 => {
                let (count, after_count) = read_varint(data, offset)?;
                if count > limits.max_sequence_arity {
                    return Err(NativeError::limit(
                        "canonical source-map set exceeds max_sequence_arity",
                    ));
                }
                offset = after_count;
                for _ in 0..count {
                    let (length, child_start) = read_varint(data, offset)?;
                    let child_end = bounded_end(child_start, length, bound)?;
                    let observed = scan_lexical_node(
                        data,
                        child_start,
                        child_end,
                        depth.saturating_add(1),
                        limits,
                        cancellation,
                        terms,
                        collect_blank_labels,
                        language_literals,
                        blank_labels,
                    )?;
                    if observed != child_end {
                        return Err(NativeError::protocol(
                            "canonical source-map set frame is invalid",
                        ));
                    }
                    offset = child_end;
                }
            }
            7 => {
                let (count, after_count) = read_varint(data, offset)?;
                if count > limits.max_sequence_arity {
                    return Err(NativeError::limit(
                        "canonical source-map sequence exceeds max_sequence_arity",
                    ));
                }
                offset = after_count;
                for _ in 0..count {
                    if data.get(offset) != Some(&1) {
                        return Err(NativeError::protocol(
                            "canonical source-map sequence item is not a node",
                        ));
                    }
                    offset = offset.checked_add(1).ok_or_else(|| {
                        NativeError::limit("canonical source-map offset overflow")
                    })?;
                    let (length, child_start) = read_varint(data, offset)?;
                    let child_end = bounded_end(child_start, length, bound)?;
                    let observed = scan_lexical_node(
                        data,
                        child_start,
                        child_end,
                        depth.saturating_add(1),
                        limits,
                        cancellation,
                        terms,
                        collect_blank_labels,
                        language_literals,
                        blank_labels,
                    )?;
                    if observed != child_end {
                        return Err(NativeError::protocol(
                            "canonical source-map sequence frame is invalid",
                        ));
                    }
                    offset = child_end;
                }
            }
            _ => {
                return Err(NativeError::protocol(
                    "canonical source-map component marker is unknown",
                ));
            }
        }
    }
    if offset != bound {
        return Err(NativeError::protocol(
            "canonical source-map node frame is invalid",
        ));
    }
    if let Some(language) = language {
        language_literals
            .try_reserve(1)
            .map_err(|_| NativeError::limit("native language literal allocation failed"))?;
        language_literals.push((structural_digest_v1(&data[start..offset]), language));
    }
    if collect_blank_labels && tag == 3 {
        let scope = anonymous_scope.ok_or_else(|| {
            NativeError::protocol("canonical anonymous identity is missing its scope")
        })?;
        let key = anonymous_key.ok_or_else(|| {
            NativeError::protocol("canonical anonymous identity is missing its local key")
        })?;
        if let Some(label) = provisional_source_blank_label(scope, key)? {
            blank_labels
                .try_reserve(1)
                .map_err(|_| NativeError::limit("native blank-label allocation failed"))?;
            blank_labels.push(label);
        }
    }
    Ok(offset)
}

fn provisional_source_blank_label<'a>(
    scope: &[u8],
    local_key: &'a [u8],
) -> NativeResult<Option<&'a str>> {
    if scope != PROVISIONAL_SCOPE || !local_key.starts_with(LEXICAL_KEY) {
        return Ok(None);
    }
    let payload = &local_key[LEXICAL_KEY.len()..];
    let (length, start) = read_varint(payload, 0)?;
    let end = bounded_end(start, length, payload.len())?;
    if end != payload.len() {
        return Err(NativeError::protocol(
            "canonical blank-label frame has trailing bytes",
        ));
    }
    std::str::from_utf8(&payload[start..end])
        .map(Some)
        .map_err(|_| NativeError::protocol("canonical blank label is not UTF-8"))
}

fn bounded_end(start: usize, length: u64, bound: usize) -> NativeResult<usize> {
    start
        .checked_add(
            usize::try_from(length)
                .map_err(|_| NativeError::limit("canonical source-map frame exceeds usize"))?,
        )
        .filter(|end| *end <= bound)
        .ok_or_else(|| NativeError::protocol("canonical source-map frame is truncated"))
}

struct RdfXmlOccurrenceCaptureV2<'a> {
    count: u64,
    collect: bool,
    scoped_digests: Option<&'a [([u8; 32], [u8; 32])]>,
    language_spellings: Vec<String>,
    source_blank_labels: Vec<String>,
    preserve_source_map: bool,
    limits: &'a Limits,
    cancellation: &'a Cancellation,
}

fn rdfxml_retained_occurrences(
    rows: &[crate::bindings::ingestion::engine::CanonicalOccurrence],
    capture: RdfXmlOccurrenceCaptureV2<'_>,
) -> NativeResult<Vec<RetainedOccurrenceV2>> {
    let RdfXmlOccurrenceCaptureV2 {
        count,
        collect,
        scoped_digests,
        language_spellings,
        source_blank_labels,
        preserve_source_map,
        limits,
        cancellation,
    } = capture;
    if !collect {
        if !rows.is_empty()
            || !language_spellings.is_empty()
            || !source_blank_labels.is_empty()
            || scoped_digests.is_some()
        {
            return Err(NativeError::protocol(
                "native RDF/XML occurrence metadata was captured while disabled",
            ));
        }
        return Ok(Vec::new());
    }
    let capacity = usize::try_from(count)
        .map_err(|_| NativeError::limit("native RDF/XML occurrence count exceeds usize"))?;
    let mut result = Vec::new();
    result
        .try_reserve_exact(capacity)
        .map_err(|_| NativeError::limit("native RDF/XML occurrence allocation failed"))?;
    if u64::try_from(rows.len()).ok() != Some(count) {
        return Err(NativeError::protocol(
            "native RDF/XML occurrence rows are incomplete",
        ));
    }
    if let Some(digests) = scoped_digests {
        if u64::try_from(digests.len()).ok() != Some(count) {
            return Err(NativeError::protocol(
                "native RDF/XML scoped occurrence digests are incomplete",
            ));
        }
    }
    for (source_order, row) in rows.iter().enumerate() {
        let provisional = structural_digest_v1(&row.row);
        let (digest, effective_digest) = scoped_digests
            .map(|values| values[source_order])
            .unwrap_or((provisional, provisional));
        result.push(RetainedOccurrenceV2 {
            digest,
            effective_digest,
            span: None,
            source_order: u64::try_from(source_order)
                .map_err(|_| NativeError::limit("native RDF/XML occurrence ordinal exceeds u64"))?,
            language_details: Vec::new(),
            source_blank_labels: Vec::new(),
        });
    }
    if preserve_source_map {
        attach_rdfxml_lexical_details(
            rows,
            &mut result,
            language_spellings,
            source_blank_labels,
            limits,
            cancellation,
        )?;
    } else if !language_spellings.is_empty() || !source_blank_labels.is_empty() {
        return Err(NativeError::protocol(
            "native RDF/XML source spellings were captured while disabled",
        ));
    }
    Ok(result)
}

fn attach_rdfxml_lexical_details(
    rows: &[crate::bindings::ingestion::engine::CanonicalOccurrence],
    occurrences: &mut [RetainedOccurrenceV2],
    language_spellings: Vec<String>,
    source_blank_labels: Vec<String>,
    limits: &Limits,
    cancellation: &Cancellation,
) -> NativeResult<()> {
    if rows.len() != occurrences.len() {
        return Err(NativeError::protocol(
            "native RDF/XML source details diverge from occurrence roots",
        ));
    }
    let mut by_language: BTreeMap<String, VecDeque<String>> = BTreeMap::new();
    for spelling in language_spellings {
        by_language
            .entry(spelling.to_ascii_lowercase())
            .or_default()
            .push_back(spelling);
    }
    if source_blank_labels
        .windows(2)
        .any(|pair| pair[0].as_bytes() >= pair[1].as_bytes())
    {
        return Err(NativeError::protocol(
            "native RDF/XML source blank labels are not canonical",
        ));
    }
    let collect_blank_labels = !source_blank_labels.is_empty();
    let mut terms = 0_u64;
    for (row, occurrence) in rows.iter().zip(occurrences) {
        cancellation.checkpoint()?;
        let CanonicalLexicalNodes {
            language_literals: literals,
            mut blank_labels,
        } = canonical_lexical_nodes(
            &row.row,
            limits,
            cancellation,
            &mut terms,
            collect_blank_labels,
        )?;
        occurrence
            .language_details
            .try_reserve_exact(literals.len())
            .map_err(|_| NativeError::limit("native RDF/XML language detail allocation failed"))?;
        for (digest, language) in literals {
            let Some(spelling) = by_language.get_mut(language).and_then(VecDeque::pop_front) else {
                continue;
            };
            occurrence
                .language_details
                .push(RetainedLanguageDetailV2 { digest, spelling });
        }
        blank_labels.sort_unstable_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
        blank_labels.dedup();
        blank_labels.retain(|label| {
            source_blank_labels
                .binary_search_by(|candidate| candidate.as_bytes().cmp(label.as_bytes()))
                .is_ok()
        });
        occurrence
            .source_blank_labels
            .try_reserve_exact(blank_labels.len())
            .map_err(|_| NativeError::limit("native RDF/XML blank-label allocation failed"))?;
        for label in blank_labels {
            let mut retained = String::new();
            retained
                .try_reserve_exact(label.len())
                .map_err(|_| NativeError::limit("native RDF/XML blank-label allocation failed"))?;
            retained.push_str(label);
            occurrence.source_blank_labels.push(retained);
        }
    }
    Ok(())
}

fn total_occurrences(parsed: &ParsedDocument) -> NativeResult<u64> {
    [&parsed.annotations, &parsed.axioms, &parsed.extensions]
        .into_iter()
        .try_fold(0_u64, |total, values| {
            total.checked_add(u64::try_from(values.len()).ok()?)
        })
        .ok_or_else(|| NativeError::limit("native occurrence count overflow"))
}

fn occurrence_root_rows(values: Vec<SpannedNode>) -> Vec<Vec<u8>> {
    values
        .into_iter()
        .map(|value| value.node.into_bytes())
        .collect()
}

fn canonicalize_root_rows(mut rows: Vec<Vec<u8>>) -> Vec<Vec<u8>> {
    rows.sort_unstable();
    rows.dedup();
    rows
}

fn apply_scoped_occurrence_digests(
    occurrences: &mut [RetainedOccurrenceV2],
    occurrence_count: u64,
    digests: &[([u8; 32], [u8; 32])],
) -> NativeResult<()> {
    if u64::try_from(digests.len()).ok() != Some(occurrence_count) {
        return Err(NativeError::protocol(
            "native Functional anonymous occurrence digests are incomplete",
        ));
    }
    for occurrence in occurrences {
        let source_order = usize::try_from(occurrence.source_order)
            .map_err(|_| NativeError::limit("native occurrence ordinal exceeds usize"))?;
        let (raw, effective) = digests.get(source_order).ok_or_else(|| {
            NativeError::protocol("native Functional anonymous occurrence order is invalid")
        })?;
        occurrence.digest = *raw;
        occurrence.effective_digest = *effective;
    }
    Ok(())
}

fn encode_origin_rows(
    metadata: &RetainedParseMetadataV2,
    document_key: &str,
    raw_owner_role: bool,
    limits: &Limits,
    cancellation: &Cancellation,
) -> NativeResult<Vec<Vec<u8>>> {
    let raw_document_key = raw_owner_role
        .then(|| digest_hex(metadata.document_fingerprint.digest))
        .transpose()?;
    let selected_document_key = raw_document_key.as_deref().unwrap_or(document_key);
    let fallback_count = if raw_owner_role {
        0
    } else {
        metadata.effective_origin_fallbacks.len()
    };
    let mut keyed = Vec::new();
    keyed
        .try_reserve_exact(
            metadata
                .occurrences
                .len()
                .checked_add(fallback_count)
                .ok_or_else(|| NativeError::limit("native origin table size overflow"))?,
        )
        .map_err(|_| NativeError::limit("native origin table allocation failed"))?;
    for (occurrence, value) in metadata.occurrences.iter().enumerate() {
        cancellation.checkpoint()?;
        let occurrence = u64::try_from(occurrence)
            .map_err(|_| NativeError::limit("native origin occurrence exceeds u64"))?;
        let digest = if raw_owner_role {
            value.digest
        } else {
            value.effective_digest
        };
        let row = encode_origin_row(digest, selected_document_key, occurrence, value.span)?;
        if u64::try_from(row.len()).map_or(true, |size| size > limits.max_wire_bytes) {
            return Err(NativeError::limit(
                "native retained origin row exceeds max_wire_bytes",
            ));
        }
        keyed.push((digest, occurrence, row));
    }
    if !raw_owner_role {
        for (digest, occurrence) in &metadata.effective_origin_fallbacks {
            cancellation.checkpoint()?;
            let row = encode_origin_row(*digest, document_key, *occurrence, None)?;
            if u64::try_from(row.len()).map_or(true, |size| size > limits.max_wire_bytes) {
                return Err(NativeError::limit(
                    "native retained origin row exceeds max_wire_bytes",
                ));
            }
            keyed.push((*digest, *occurrence, row));
        }
    }
    keyed.sort_unstable_by(|left, right| {
        left.0
            .cmp(&right.0)
            .then_with(|| left.1.cmp(&right.1))
            .then_with(|| left.2.cmp(&right.2))
    });
    let mut rows = Vec::new();
    rows.try_reserve_exact(keyed.len())
        .map_err(|_| NativeError::limit("native origin row allocation failed"))?;
    rows.extend(keyed.into_iter().map(|(_digest, _occurrence, row)| row));
    Ok(rows)
}

fn digest_hex(digest: [u8; 32]) -> NativeResult<String> {
    const LOWER_HEX: &[u8; 16] = b"0123456789abcdef";

    let mut encoded = String::new();
    encoded
        .try_reserve_exact(digest.len() * 2)
        .map_err(|_| NativeError::limit("native origin document fingerprint allocation failed"))?;
    for byte in digest {
        encoded.push(char::from(LOWER_HEX[usize::from(byte >> 4)]));
        encoded.push(char::from(LOWER_HEX[usize::from(byte & 0x0f)]));
    }
    Ok(encoded)
}

fn encode_source_map_rows(
    metadata: &RetainedParseMetadataV2,
    limits: &Limits,
    cancellation: &Cancellation,
) -> NativeResult<TypedSourceMapRowsV2> {
    let row_count = source_map_row_count(metadata)?;
    let mut keyed = Vec::new();
    keyed
        .try_reserve_exact(
            usize::try_from(row_count)
                .map_err(|_| NativeError::limit("native source-map count exceeds usize"))?,
        )
        .map_err(|_| NativeError::limit("native source-map table allocation failed"))?;
    let mut producer_order = 0_u64;
    for (occurrence, value) in metadata.occurrences.iter().enumerate() {
        cancellation.checkpoint()?;
        let occurrence = u64::try_from(occurrence)
            .map_err(|_| NativeError::limit("native source-map occurrence exceeds u64"))?;
        let mut root_lexical = Vec::new();
        root_lexical
            .try_reserve_exact(
                value
                    .language_details
                    .len()
                    .saturating_add(value.source_blank_labels.len()),
            )
            .map_err(|_| NativeError::limit("native source-map lexical allocation failed"))?;
        for (index, detail) in value.language_details.iter().enumerate() {
            let key = if index == 0 {
                "language-tag".to_owned()
            } else {
                format!("language-tag:{}", index + 1)
            };
            root_lexical.push((key, detail.spelling.as_str()));
        }
        for (index, label) in value.source_blank_labels.iter().enumerate() {
            let key = if index == 0 {
                "blank-label".to_owned()
            } else {
                format!("blank-label:{}", index + 1)
            };
            root_lexical.push((key, label.as_str()));
        }
        root_lexical.sort_unstable_by(|left, right| left.0.as_bytes().cmp(right.0.as_bytes()));
        let row = encode_source_map_row(value.digest, occurrence, value.span, &root_lexical)?;
        if u64::try_from(row.len()).map_or(true, |size| size > limits.max_wire_bytes) {
            return Err(NativeError::limit(
                "native retained source-map row exceeds max_wire_bytes",
            ));
        }
        keyed.push((value.digest, producer_order, row));
        producer_order = producer_order
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("native source-map producer order overflow"))?;
        for detail in &value.language_details {
            let lexical = [("language-tag".to_owned(), detail.spelling.as_str())];
            let row = encode_source_map_row(detail.digest, occurrence, value.span, &lexical)?;
            if u64::try_from(row.len()).map_or(true, |size| size > limits.max_wire_bytes) {
                return Err(NativeError::limit(
                    "native retained source-map row exceeds max_wire_bytes",
                ));
            }
            keyed.push((detail.digest, producer_order, row));
            producer_order = producer_order
                .checked_add(1)
                .ok_or_else(|| NativeError::limit("native source-map producer order overflow"))?;
        }
    }
    keyed.sort_unstable_by(|left, right| left.0.cmp(&right.0).then_with(|| left.1.cmp(&right.1)));
    let mut entries = Vec::new();
    entries
        .try_reserve_exact(keyed.len())
        .map_err(|_| NativeError::limit("native source-map row allocation failed"))?;
    entries.extend(keyed.into_iter().map(|(_digest, _producer_order, row)| row));

    let selected_prefixes = metadata.source_prefixes.as_deref().ok_or_else(|| {
        NativeError::protocol("native retained source-map prefixes are unavailable")
    })?;
    let mut prefixes = Vec::new();
    prefixes
        .try_reserve_exact(selected_prefixes.len())
        .map_err(|_| NativeError::limit("native source-prefix row allocation failed"))?;
    let mut previous: Option<&[u8]> = None;
    for (prefix, iri) in selected_prefixes {
        cancellation.checkpoint()?;
        if previous.is_some_and(|value| value >= prefix.as_bytes()) {
            return Err(NativeError::protocol(
                "native retained source prefixes are not canonical",
            ));
        }
        previous = Some(prefix.as_bytes());
        let row = encode_source_prefix_row(prefix, iri)?;
        if u64::try_from(row.len()).map_or(true, |size| size > limits.max_wire_bytes) {
            return Err(NativeError::limit(
                "native retained source-prefix row exceeds max_wire_bytes",
            ));
        }
        prefixes.push(row);
    }
    Ok(TypedSourceMapRowsV2 { entries, prefixes })
}

fn encode_source_map_row(
    digest: [u8; 32],
    occurrence: u64,
    span: Option<Span>,
    lexical: &[(String, &str)],
) -> NativeResult<Vec<u8>> {
    let lexical_count = u16::try_from(lexical.len())
        .map_err(|_| NativeError::limit("native source-map lexical count exceeds u16"))?;
    let mut previous: Option<&[u8]> = None;
    let lexical_size = lexical.iter().try_fold(0_usize, |total, (key, value)| {
        if key.is_empty() || previous.is_some_and(|selected| selected >= key.as_bytes()) {
            return Err(NativeError::protocol(
                "native source-map lexical keys are not canonical",
            ));
        }
        previous = Some(key.as_bytes());
        u32::try_from(key.len())
            .map_err(|_| NativeError::limit("native source-map lexical key exceeds u32"))?;
        u32::try_from(value.len())
            .map_err(|_| NativeError::limit("native source-map lexical value exceeds u32"))?;
        total
            .checked_add(8)
            .and_then(|selected| selected.checked_add(key.len()))
            .and_then(|selected| selected.checked_add(value.len()))
            .ok_or_else(|| NativeError::limit("native source-map lexical size overflow"))
    })?;
    let size = 32_usize
        .checked_add(8 + 1 + usize::from(span.is_some()) * 4 * 8 + 2)
        .and_then(|value| value.checked_add(lexical_size))
        .ok_or_else(|| NativeError::limit("native source-map row size overflow"))?;
    let mut row = Vec::new();
    row.try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native source-map row allocation failed"))?;
    row.extend_from_slice(&digest);
    row.extend_from_slice(&occurrence.to_le_bytes());
    encode_source_span(span, &mut row);
    row.extend_from_slice(&lexical_count.to_le_bytes());
    for (key, value) in lexical {
        encode_source_text(key, &mut row)?;
        encode_source_text(value, &mut row)?;
    }
    Ok(row)
}

fn source_map_row_count(metadata: &RetainedParseMetadataV2) -> NativeResult<u64> {
    metadata
        .occurrences
        .iter()
        .try_fold(0_u64, |total, occurrence| {
            total
                .checked_add(1)
                .and_then(|selected| {
                    u64::try_from(occurrence.language_details.len())
                        .ok()
                        .and_then(|count| selected.checked_add(count))
                })
                .ok_or_else(|| NativeError::limit("native source-map count overflow"))
        })
}

fn encode_source_text(value: &str, row: &mut Vec<u8>) -> NativeResult<()> {
    let length = u32::try_from(value.len())
        .map_err(|_| NativeError::limit("native source-map text exceeds u32"))?;
    row.extend_from_slice(&length.to_le_bytes());
    row.extend_from_slice(value.as_bytes());
    Ok(())
}

fn encode_source_prefix_row(prefix: &str, iri: &str) -> NativeResult<Vec<u8>> {
    let prefix_len = u32::try_from(prefix.len())
        .map_err(|_| NativeError::limit("native source prefix exceeds u32"))?;
    let iri_len = u32::try_from(iri.len())
        .map_err(|_| NativeError::limit("native source prefix IRI exceeds u32"))?;
    let size = 8_usize
        .checked_add(prefix.len())
        .and_then(|value| value.checked_add(iri.len()))
        .ok_or_else(|| NativeError::limit("native source-prefix row size overflow"))?;
    let mut row = Vec::new();
    row.try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native source-prefix row allocation failed"))?;
    row.extend_from_slice(&prefix_len.to_le_bytes());
    row.extend_from_slice(prefix.as_bytes());
    row.extend_from_slice(&iri_len.to_le_bytes());
    row.extend_from_slice(iri.as_bytes());
    Ok(row)
}

fn encode_source_span(span: Option<Span>, row: &mut Vec<u8>) {
    match span {
        Some(span) => {
            row.push(0x8f);
            for coordinate in [span.byte_start, span.byte_end, span.line, span.column] {
                row.extend_from_slice(&coordinate.to_le_bytes());
            }
        }
        None => row.push(0),
    }
}

fn encode_origin_row(
    digest: [u8; 32],
    document_key: &str,
    occurrence: u64,
    span: Option<Span>,
) -> NativeResult<Vec<u8>> {
    let key = document_key.as_bytes();
    let key_len = u32::try_from(key.len())
        .map_err(|_| NativeError::limit("native document key exceeds u32"))?;
    let size = 32_usize
        .checked_add(4)
        .and_then(|value| value.checked_add(key.len()))
        .and_then(|value| value.checked_add(8 + 1 + usize::from(span.is_some()) * 4 * 8))
        .ok_or_else(|| NativeError::limit("native origin row size overflow"))?;
    let mut row = Vec::new();
    row.try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native origin row allocation failed"))?;
    row.extend_from_slice(&digest);
    row.extend_from_slice(&key_len.to_le_bytes());
    row.extend_from_slice(key);
    row.extend_from_slice(&occurrence.to_le_bytes());
    match span {
        Some(span) => {
            row.push(0x8f);
            for coordinate in [span.byte_start, span.byte_end, span.line, span.column] {
                row.extend_from_slice(&coordinate.to_le_bytes());
            }
        }
        None => row.push(0),
    }
    Ok(row)
}

fn root_manifest_digest(
    domain: &[u8],
    document_key: &str,
    counts: [u64; 3],
    document_digest: [u8; 32],
) -> NativeResult<[u8; 32]> {
    let mut hasher = MeasuredSha256::domain(domain)?;
    hasher.u32_le(1)?;
    hasher.u64_le(1)?;
    hasher.text64(document_key)?;
    for count in counts {
        hasher.u64_le(count)?;
    }
    hasher.update(&document_digest)?;
    Ok(hasher.finish().digest)
}

fn fingerprint_inputs_digest(
    document_key: &str,
    document: FingerprintEvidenceV2,
    structural: FingerprintEvidenceV2,
    logical: FingerprintEvidenceV2,
    signature: FingerprintEvidenceV2,
) -> NativeResult<[u8; 32]> {
    let mut hasher = MeasuredSha256::domain(FINGERPRINT_INPUTS_MANIFEST_DOMAIN_V2)?;
    hasher.u32_le(1)?;
    hasher.text64(document_key)?;
    hasher.u64_le(1)?;
    for (tag, key, evidence) in [
        (1_u8, Some(document_key), document),
        (2_u8, None, structural),
        (3_u8, None, logical),
        (4_u8, None, signature),
    ] {
        hasher.update(&[tag])?;
        if let Some(value) = key {
            hasher.text64(value)?;
        }
        hasher.u64_le(evidence.preimage_bytes)?;
        hasher.u32_le(1)?;
        hasher.update(&evidence.digest)?;
    }
    Ok(hasher.finish().digest)
}

fn source_manifest_digest(
    document_key: &str,
    source_map: Option<&TypedSourceMapRowsV2>,
) -> NativeResult<[u8; 32]> {
    let mut hasher = MeasuredSha256::domain(SOURCE_MANIFEST_DOMAIN_V2)?;
    hasher.update(&AUXILIARY_CODEC_SCHEMA_SHA256_V2)?;
    hasher.u64_le(1)?;
    hasher.text64(document_key)?;
    let Some(source_map) = source_map else {
        hasher.update(&[0])?;
        return Ok(hasher.finish().digest);
    };
    let entry_count = u64::try_from(source_map.entries.len())
        .map_err(|_| NativeError::limit("native source-map count exceeds u64"))?;
    let prefix_count = u64::try_from(source_map.prefixes.len())
        .map_err(|_| NativeError::limit("native source-prefix count exceeds u64"))?;
    let mut document = MeasuredSha256::domain(DOCUMENT_SOURCE_TABLE_DOMAIN_V2)?;
    document.text64(document_key)?;
    document.u64_le(entry_count)?;
    for row in &source_map.entries {
        document.frame64(row)?;
    }
    document.u64_le(prefix_count)?;
    for row in &source_map.prefixes {
        document.frame64(row)?;
    }
    hasher.update(&[1])?;
    hasher.u64_le(entry_count)?;
    hasher.u64_le(prefix_count)?;
    hasher.update(&document.finish().digest)?;
    Ok(hasher.finish().digest)
}

fn provenance_manifest_digest(
    document_key: &str,
    present: bool,
    origins: &[Vec<u8>],
    rdf_report: Option<&PreparedRetainedRdfReportV2>,
) -> NativeResult<[u8; 32]> {
    let mut hasher = MeasuredSha256::domain(PROVENANCE_MANIFEST_DOMAIN_V2)?;
    hasher.update(&AUXILIARY_CODEC_SCHEMA_SHA256_V2)?;
    hasher.u64_le(1)?;
    hasher.text64(document_key)?;
    if present {
        let count = u64::try_from(origins.len())
            .map_err(|_| NativeError::limit("native origin count exceeds u64"))?;
        let mut document = MeasuredSha256::domain(DOCUMENT_ORIGIN_TABLE_DOMAIN_V2)?;
        document.text64(document_key)?;
        document.u64_le(count)?;
        for row in origins {
            document.frame64(row)?;
        }
        hasher.update(&[1])?;
        hasher.u64_le(count)?;
        hasher.update(&document.finish().digest)?;
    } else {
        hasher.update(&[0])?;
    }
    if let Some(report) = rdf_report {
        hasher.update(&[1])?;
        hasher.u64_le(
            u64::try_from(report.rows.unconsumed_triples.len())
                .map_err(|_| NativeError::limit("native RDF unconsumed count exceeds u64"))?,
        )?;
        hasher.u64_le(
            u64::try_from(report.rows.rule_ids.len())
                .map_err(|_| NativeError::limit("native RDF rule count exceeds u64"))?,
        )?;
        hasher.u64_le(
            u64::try_from(report.rows.diagnostics.len())
                .map_err(|_| NativeError::limit("native RDF diagnostic count exceeds u64"))?,
        )?;
        hasher.update(&report.digest)?;
    } else {
        hasher.update(&[0])?;
    }
    Ok(hasher.finish().digest)
}

fn prepare_rdf_report(
    document_key: &str,
    mapping: &RetainedRdfMappingEvidenceV2,
    limits: &Limits,
) -> NativeResult<PreparedRetainedRdfReportV2> {
    let remaining = mapping
        .total_triples
        .checked_sub(mapping.consumed_triples)
        .ok_or_else(|| NativeError::protocol("native RDF consumed count exceeds total"))?;
    let evidence_count = u64::try_from(mapping.unconsumed.len())
        .map_err(|_| NativeError::limit("native RDF evidence count exceeds u64"))?;
    if (remaining == 0) != mapping.unconsumed.is_empty()
        || evidence_count > remaining
        || evidence_count > limits.value(crate::limits::LimitKey::MaxDiagnostics)
    {
        return Err(NativeError::protocol(
            "native RDF report evidence diverges from mapping counts",
        ));
    }
    let conformant = remaining == 0;
    let mut header = Vec::new();
    header
        .try_reserve_exact(17)
        .map_err(|_| NativeError::limit("native RDF report header allocation failed"))?;
    header.push(u8::from(conformant));
    header.extend_from_slice(&mapping.consumed_triples.to_le_bytes());
    header.extend_from_slice(&mapping.total_triples.to_le_bytes());

    let mut unconsumed_triples = Vec::new();
    unconsumed_triples
        .try_reserve_exact(mapping.unconsumed.len())
        .map_err(|_| NativeError::limit("native RDF evidence row allocation failed"))?;
    for triple in &mapping.unconsumed {
        let row = encode_rdf_triple_evidence(triple, limits)?;
        unconsumed_triples.push(row);
    }
    let mut rule_ids = Vec::new();
    if !conformant {
        rule_ids
            .try_reserve_exact(1)
            .map_err(|_| NativeError::limit("native RDF rule row allocation failed"))?;
        rule_ids.push(encode_rdf_rule_id("OWL2-RDF-REVERSE", limits)?);
    }

    let mut report = MeasuredSha256::domain(RDF_MAPPING_REPORT_DOMAIN_V2)?;
    report.text64(document_key)?;
    report.frame64(&header)?;
    report.u64_le(evidence_count)?;
    for row in &unconsumed_triples {
        report.frame64(row)?;
    }
    report.u64_le(u64::from(!conformant))?;
    for row in &rule_ids {
        report.frame64(row)?;
    }
    report.u64_le(0)?;
    let digest = report.finish().digest;
    let retained_bytes = unconsumed_triples
        .iter()
        .chain(&rule_ids)
        .try_fold(header.capacity(), |total, row| {
            total
                .checked_add(row.capacity())
                .ok_or_else(|| NativeError::limit("native RDF report size overflow"))
        })
        .and_then(|size| {
            u64::try_from(size)
                .map_err(|_| NativeError::limit("native RDF report size exceeds u64"))
        })?;
    Ok(PreparedRetainedRdfReportV2 {
        rows: TypedRdfReportRowsV2 {
            header,
            unconsumed_triples,
            rule_ids,
            diagnostics: Vec::new(),
        },
        conformant,
        consumed_triples: mapping.consumed_triples,
        total_triples: mapping.total_triples,
        digest,
        retained_bytes,
    })
}

fn encode_rdf_triple_evidence(
    triple: &crate::bindings::ingestion::engine::RdfTripleEvidence,
    limits: &Limits,
) -> NativeResult<Vec<u8>> {
    if triple.object_requires_repr {
        return Err(NativeError::protocol(
            "native RDF literal evidence was not rendered by the binding",
        ));
    }
    if triple.subject.is_empty() || triple.predicate.is_empty() || triple.object.is_empty() {
        return Err(NativeError::protocol(
            "native RDF evidence contains empty text",
        ));
    }
    let size = [&triple.subject, &triple.predicate, &triple.object]
        .into_iter()
        .try_fold(0_usize, |total, value| {
            u32::try_from(value.len())
                .map_err(|_| NativeError::limit("native RDF evidence text exceeds u32"))?;
            total
                .checked_add(4)
                .and_then(|selected| selected.checked_add(value.len()))
                .ok_or_else(|| NativeError::limit("native RDF evidence row size overflow"))
        })?;
    ensure_rdf_auxiliary_row_size(size, limits)?;
    let mut row = Vec::new();
    row.try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native RDF evidence row allocation failed"))?;
    for value in [&triple.subject, &triple.predicate, &triple.object] {
        encode_source_text(value, &mut row)?;
    }
    Ok(row)
}

fn encode_rdf_rule_id(value: &str, limits: &Limits) -> NativeResult<Vec<u8>> {
    if value.is_empty() {
        return Err(NativeError::protocol("native RDF rule id is empty"));
    }
    u32::try_from(value.len()).map_err(|_| NativeError::limit("native RDF rule id exceeds u32"))?;
    let size = value
        .len()
        .checked_add(4)
        .ok_or_else(|| NativeError::limit("native RDF rule row size overflow"))?;
    ensure_rdf_auxiliary_row_size(size, limits)?;
    let mut row = Vec::new();
    row.try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native RDF rule row allocation failed"))?;
    encode_source_text(value, &mut row)?;
    Ok(row)
}

fn ensure_rdf_auxiliary_row_size(size: usize, limits: &Limits) -> NativeResult<()> {
    if u64::try_from(size).map_or(true, |size| size > limits.max_wire_bytes) {
        return Err(NativeError::limit(
            "native RDF report row exceeds max_wire_bytes",
        ));
    }
    Ok(())
}

fn effective_origin_manifest_digest(
    document_key: &str,
    origins: &[Vec<u8>],
) -> NativeResult<[u8; 32]> {
    let count = u64::try_from(origins.len())
        .map_err(|_| NativeError::limit("native effective origin count exceeds u64"))?;
    let mut document = MeasuredSha256::domain(EFFECTIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2)?;
    document.text64(document_key)?;
    document.u64_le(count)?;
    document.u64_le(count)?;
    for row in origins {
        document.frame64(row)?;
    }
    let document_digest = document.finish().digest;
    let mut closure = MeasuredSha256::domain(EFFECTIVE_CLOSURE_ORIGIN_TABLE_DOMAIN_V2)?;
    closure.u64_le(count)?;
    for row in origins {
        closure.frame64(row)?;
    }
    let closure_digest = closure.finish().digest;
    let mut manifest = MeasuredSha256::domain(EFFECTIVE_ORIGIN_MANIFEST_DOMAIN_V2)?;
    manifest.update(&AUXILIARY_CODEC_SCHEMA_SHA256_V2)?;
    manifest.u64_le(1)?;
    manifest.text64(document_key)?;
    manifest.u64_le(count)?;
    manifest.update(&document_digest)?;
    manifest.u64_le(count)?;
    manifest.update(&closure_digest)?;
    Ok(manifest.finish().digest)
}

fn without_annotations(row: &[u8]) -> NativeResult<Vec<u8>> {
    let (tag, mut offset) = read_varint(row, 0)?;
    let fields = canonical_field_count(
        u16::try_from(tag).map_err(|_| NativeError::protocol("canonical tag exceeds u16"))?,
    )
    .ok_or_else(|| NativeError::protocol("canonical field ledger is incomplete"))?;
    let mut last = offset;
    for _ in 0..fields {
        last = offset;
        offset = skip_component(row, offset)?;
    }
    if offset != row.len() || last >= row.len() || row[last] != 6 {
        return Err(NativeError::protocol(
            "logical root has invalid annotation framing",
        ));
    }
    let mut result = Vec::new();
    let size = last
        .checked_add(2)
        .ok_or_else(|| NativeError::limit("logical row size overflow"))?;
    result
        .try_reserve_exact(size)
        .map_err(|_| NativeError::limit("logical row allocation failed"))?;
    result.extend_from_slice(&row[..last]);
    result.extend_from_slice(&[6, 0]);
    Ok(result)
}

fn skip_component(data: &[u8], offset: usize) -> NativeResult<usize> {
    let marker = *data
        .get(offset)
        .ok_or_else(|| NativeError::protocol("canonical component is truncated"))?;
    let mut following = offset
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("canonical component offset overflow"))?;
    match marker {
        0 => Ok(following),
        1 | 2 | 3 | 5 => {
            let (length, after) = read_varint(data, following)?;
            following = after;
            following
                .checked_add(
                    usize::try_from(length).map_err(|_| {
                        NativeError::limit("canonical component length exceeds usize")
                    })?,
                )
                .filter(|end| *end <= data.len())
                .ok_or_else(|| NativeError::protocol("canonical component frame is truncated"))
        }
        4 => read_varint(data, following).map(|(_value, after)| after),
        6 => {
            let (count, mut after) = read_varint(data, following)?;
            for _ in 0..count {
                let (length, framed) = read_varint(data, after)?;
                after = framed
                    .checked_add(
                        usize::try_from(length)
                            .map_err(|_| NativeError::limit("canonical set frame exceeds usize"))?,
                    )
                    .filter(|end| *end <= data.len())
                    .ok_or_else(|| NativeError::protocol("canonical set frame is truncated"))?;
            }
            Ok(after)
        }
        7 => {
            let (count, mut after) = read_varint(data, following)?;
            for _ in 0..count {
                after = skip_component(data, after)?;
            }
            Ok(after)
        }
        _ => Err(NativeError::protocol(
            "canonical component marker is unknown",
        )),
    }
}

fn row_tag(row: &[u8]) -> NativeResult<u64> {
    read_varint(row, 0).map(|(tag, _offset)| tag)
}

const fn is_logical_axiom(tag: u64) -> bool {
    matches!(
        tag,
        61..=64 | 70..=82 | 90..=95 | 100..=101 | 110..=116
    )
}

fn iri_text(data: &[u8]) -> NativeResult<&str> {
    let (tag, offset) = read_varint(data, 0)?;
    if tag != 1 || data.get(offset) != Some(&2) {
        return Err(NativeError::protocol(
            "native retained metadata is not an IRI",
        ));
    }
    let (length, start) = read_varint(data, offset + 1)?;
    let end = start
        .checked_add(
            usize::try_from(length)
                .map_err(|_| NativeError::limit("native IRI length exceeds usize"))?,
        )
        .filter(|end| *end == data.len())
        .ok_or_else(|| NativeError::protocol("native IRI frame is invalid"))?;
    std::str::from_utf8(&data[start..end])
        .map_err(|_| NativeError::protocol("native IRI is not UTF-8"))
}

fn read_varint(data: &[u8], mut offset: usize) -> NativeResult<(u64, usize)> {
    let mut value = 0_u64;
    let mut shift = 0_u32;
    loop {
        let byte = *data
            .get(offset)
            .ok_or_else(|| NativeError::protocol("canonical varint is truncated"))?;
        offset = offset
            .checked_add(1)
            .ok_or_else(|| NativeError::limit("canonical varint offset overflow"))?;
        let payload = u64::from(byte & 0x7f);
        if shift >= 64 && payload != 0 {
            return Err(NativeError::limit("canonical varint exceeds u64"));
        }
        value |= payload
            .checked_shl(shift)
            .ok_or_else(|| NativeError::limit("canonical varint shift overflow"))?;
        if byte & 0x80 == 0 {
            return Ok((value, offset));
        }
        shift = shift
            .checked_add(7)
            .ok_or_else(|| NativeError::limit("canonical varint shift overflow"))?;
        if shift > 63 {
            return Err(NativeError::limit("canonical varint exceeds u64"));
        }
    }
}

fn logical_workspace_bytes<'a>(
    axiom_capacity: usize,
    extension_capacity: usize,
    mut rows: impl Iterator<Item = &'a Vec<u8>>,
) -> NativeResult<u64> {
    let outer_slots = axiom_capacity
        .checked_add(extension_capacity)
        .and_then(|count| count.checked_mul(size_of::<Vec<u8>>()))
        .ok_or_else(|| NativeError::limit("native logical workspace allocation overflow"))?;
    rows.try_fold(
        u64::try_from(outer_slots)
            .map_err(|_| NativeError::limit("native logical workspace exceeds u64"))?,
        |total, row| {
            total
                .checked_add(
                    u64::try_from(row.capacity())
                        .map_err(|_| NativeError::limit("native logical row exceeds u64"))?,
                )
                .ok_or_else(|| NativeError::limit("native logical workspace byte overflow"))
        },
    )
}

#[derive(Debug)]
struct MeasuredSha256 {
    hasher: Sha256,
    bytes: u64,
}

impl MeasuredSha256 {
    fn new() -> Self {
        Self {
            hasher: Sha256::new(),
            bytes: 0,
        }
    }

    fn domain(domain: &[u8]) -> NativeResult<Self> {
        let mut result = Self::new();
        result.update(domain)?;
        result.update(&[0])?;
        Ok(result)
    }

    fn update(&mut self, value: &[u8]) -> NativeResult<()> {
        self.bytes = self
            .bytes
            .checked_add(
                u64::try_from(value.len())
                    .map_err(|_| NativeError::limit("native digest input exceeds u64"))?,
            )
            .ok_or_else(|| NativeError::limit("native digest input size overflow"))?;
        self.hasher.update(value);
        Ok(())
    }

    fn u32_le(&mut self, value: u32) -> NativeResult<()> {
        self.update(&value.to_le_bytes())
    }

    fn u64_le(&mut self, value: u64) -> NativeResult<()> {
        self.update(&value.to_le_bytes())
    }

    fn text64(&mut self, value: &str) -> NativeResult<()> {
        self.frame64(value.as_bytes())
    }

    fn frame64(&mut self, value: &[u8]) -> NativeResult<()> {
        self.u64_le(
            u64::try_from(value.len())
                .map_err(|_| NativeError::limit("native framed value exceeds u64"))?,
        )?;
        self.update(value)
    }

    fn varint(&mut self, value: u64) -> NativeResult<()> {
        let (encoded, length) = encode_varint(value);
        self.update(&encoded[..length])
    }

    fn frame_varint(&mut self, value: &[u8]) -> NativeResult<()> {
        self.varint(
            u64::try_from(value.len())
                .map_err(|_| NativeError::limit("native canonical frame exceeds u64"))?,
        )?;
        self.update(value)
    }

    fn finish(self) -> FingerprintEvidenceV2 {
        FingerprintEvidenceV2 {
            preimage_bytes: self.bytes,
            digest: self.hasher.finish(),
        }
    }
}

fn encode_varint(mut value: u64) -> ([u8; 10], usize) {
    let mut output = [0_u8; 10];
    let mut length = 0;
    loop {
        let byte = (value & 0x7f) as u8;
        value >>= 7;
        output[length] = byte | if value == 0 { 0 } else { 0x80 };
        length += 1;
        if value == 0 {
            return (output, length);
        }
    }
}

fn append(output: &mut Vec<u8>, value: &[u8]) -> NativeResult<()> {
    output
        .try_reserve(value.len())
        .map_err(|_| NativeError::limit("native retained framing allocation failed"))?;
    output.extend_from_slice(value);
    Ok(())
}

fn append_u64(output: &mut Vec<u8>, value: u64) -> NativeResult<()> {
    append(output, &value.to_le_bytes())
}

fn append_optional_text(output: &mut Vec<u8>, value: Option<&str>) -> NativeResult<()> {
    match value {
        Some(selected) => {
            append(output, &[1])?;
            append_text64(output, selected)
        }
        None => append(output, &[0]),
    }
}

fn append_text64(output: &mut Vec<u8>, value: &str) -> NativeResult<()> {
    append_u64(
        output,
        u64::try_from(value.len())
            .map_err(|_| NativeError::limit("native retained text exceeds u64"))?,
    )?;
    append(output, value.as_bytes())
}

fn checked_add(left: u64, right: u64, message: &'static str) -> NativeResult<u64> {
    left.checked_add(right)
        .ok_or_else(|| NativeError::limit(message))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::canonical::{canonical_set, entity, literal, Field};

    #[test]
    fn language_details_follow_canonical_walk_and_source_spelling_queues() {
        let datatype = entity(
            "datatype",
            iri("http://www.w3.org/1999/02/22-rdf-syntax-ns#PlainLiteral".into())
                .expect("plain literal IRI"),
        )
        .expect("datatype");
        let values = canonical_set(
            vec![
                literal("z".into(), datatype.clone(), Some("en".into())).expect("literal"),
                literal("a".into(), datatype, Some("en".into())).expect("literal"),
            ],
            1,
            None,
        )
        .expect("canonical values");
        let expected_digests = values
            .iter()
            .map(|value| structural_digest_v1(value.as_bytes()))
            .collect::<Vec<_>>();
        let root = Node::build(24, vec![Field::Set(values)]).expect("data enumeration");
        let span = Span {
            byte_start: 1,
            byte_end: 2,
            line: 1,
            column: 1,
        };
        let parsed = ParsedDocument {
            ontology_iri: None,
            version_iri: None,
            imports: Vec::new(),
            annotations: Vec::new(),
            axioms: vec![SpannedNode {
                node: root.clone(),
                span,
            }],
            extensions: Vec::new(),
            prefixes: Vec::new(),
            decoded_codepoints: 0,
            language_spellings: Vec::new(),
        };
        let mut occurrences = vec![RetainedOccurrenceV2 {
            digest: structural_digest_v1(root.as_bytes()),
            effective_digest: structural_digest_v1(root.as_bytes()),
            span: Some(span),
            source_order: 0,
            language_details: Vec::new(),
            source_blank_labels: Vec::new(),
        }];

        attach_language_details(
            &parsed,
            &mut occurrences,
            vec!["EN".into(), "eN".into()],
            &Limits::default(),
            &Cancellation::with_duration(None),
        )
        .expect("language details");

        assert_eq!(
            occurrences[0]
                .language_details
                .iter()
                .map(|detail| detail.digest)
                .collect::<Vec<_>>(),
            expected_digests
        );
        assert_eq!(
            occurrences[0]
                .language_details
                .iter()
                .map(|detail| detail.spelling.as_str())
                .collect::<Vec<_>>(),
            ["EN", "eN"]
        );
    }
}
