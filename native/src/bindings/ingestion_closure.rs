//! Bounded owner-first preparation for parser-built import closures.
//!
//! Python supplies only document-aligned metadata and the canonical import
//! manifest. Structural, source, origin, RDF-report, and fingerprint rows stay
//! in Rust. The resulting summary is O(documents); the prepared composite owns
//! the proportional tables until attested finalization.

use std::mem::size_of;
use std::sync::Arc;

use crate::cancel::{Cancellation, Guard, InterruptSlot};
use crate::error::{NativeError, NativeResult};
use crate::hash::Sha256;
use crate::limits::{LimitKey, Limits};
use crate::model::{canonical_field_count, scan_canonical, structural_digest_v2, ScanBudget};
use crate::parse::RetainedParseMetadataV2;
use crate::publication::{
    PreparedTypedAuxiliaryV2, TypedFacadeBuilderV2, TypedFacadeCollectionV2, TypedFacadeScopeV2,
    TypedFacadeStorageV2, TypedRdfReportRowsV2, TypedSourceMapRowsV2,
    AUXILIARY_CODEC_SCHEMA_SHA256_V2,
};
use crate::session::Session;

pub(super) const RETAINED_CLOSURE_PREPARED_MAGIC_V2: &[u8; 8] = b"PYNFCP2\0";
const RETAINED_CLOSURE_PREPARED_SCHEMA_V2: u16 = 1;

const DOCUMENT_FINGERPRINT_DOMAIN_V2: &[u8] = b"pyowl-core:document-fingerprint:v2\0";
const STRUCTURAL_FINGERPRINT_DOMAIN_V2: &[u8] = b"pyowl-core:snapshot-structural:v2\0";
const LOGICAL_FINGERPRINT_DOMAIN_V2: &[u8] = b"pyowl-core:snapshot-logical:v2\0";
const LOGICAL_POLICY_V1: &[u8] = b"datatype-policy:owl2-v1\0";
const SIGNATURE_FINGERPRINT_DOMAIN_V2: &[u8] = b"pyowl-core:snapshot-signature:v2\0";

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
const RETAINED_CLOSURE_SUMMARY_BASE_BYTES_V2: usize = 444;
const RETAINED_CLOSURE_SUMMARY_DOCUMENT_BYTES_V2: usize = 121;
const RETAINED_CLOSURE_SUMMARY_RDF_REPORT_BYTES_V2: usize = 81;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct FingerprintEvidenceV2 {
    pub(super) preimage_bytes: u64,
    pub(super) digest: [u8; 32],
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) struct ContentDigestsV2 {
    pub(super) root_table_sha256: [u8; 32],
    pub(super) effective_root_table_sha256: [u8; 32],
    pub(super) fingerprint_inputs_sha256: [u8; 32],
    pub(super) source_manifest_sha256: [u8; 32],
    pub(super) provenance_manifest_sha256: [u8; 32],
    pub(super) effective_origin_manifest_sha256: [u8; 32],
}

#[derive(Debug)]
pub(super) struct PreparedClosureRdfReportV2 {
    pub(super) rows: TypedRdfReportRowsV2,
    pub(super) row_counts: [u64; 3],
    pub(super) conformant: bool,
    pub(super) consumed_triples: u64,
    pub(super) total_triples: u64,
    pub(super) digest: [u8; 32],
    pub(super) retained_bytes: u64,
}

#[derive(Debug)]
pub(super) struct PreparedClosureDocumentV2 {
    pub(super) document_key: Box<str>,
    pub(super) document_fingerprint: FingerprintEvidenceV2,
    pub(super) raw_counts: [u64; 3],
    pub(super) effective_counts: [u64; 3],
    pub(super) source_map: Option<TypedSourceMapRowsV2>,
    pub(super) source_counts: [u64; 2],
    pub(super) origin_rows: Option<Vec<Vec<u8>>>,
    pub(super) raw_origin_rows: Option<Vec<Vec<u8>>>,
    pub(super) origin_counts: [u64; 2],
    pub(super) rdf_report: Option<PreparedClosureRdfReportV2>,
}

#[derive(Debug)]
pub(super) struct PreparedClosurePublicationV2 {
    pub(super) documents: Vec<PreparedClosureDocumentV2>,
    pub(super) structural_fingerprint: FingerprintEvidenceV2,
    pub(super) logical_fingerprint: FingerprintEvidenceV2,
    pub(super) signature_fingerprint: FingerprintEvidenceV2,
    pub(super) content: ContentDigestsV2,
    pub(super) closure_counts: [u64; 3],
    pub(super) closure_origin_count: u64,
    pub(super) auxiliary: PreparedTypedAuxiliaryV2,
    pub(super) max_facade_row_bytes: u64,
    pub(super) parser_summary_bytes_materialized: u64,
    pub(super) canonical_rows_scanned: u64,
    pub(super) structural_occurrence_rows_scanned: u64,
    pub(super) metadata_iri_objects_materialized: u64,
    pub(super) canonical_rows_encoded: u64,
    pub(super) canonical_bytes_encoded: u64,
    pub(super) fingerprint_temporary_bytes: u64,
    pub(super) origin_bytes_retained: u64,
    evidence_limits: Limits,
    evidence_base_live_bytes: usize,
    evidence_retained_bytes: usize,
}

impl PreparedClosurePublicationV2 {
    pub(super) fn encode_summary(&self, prepare_ns: u64) -> NativeResult<Vec<u8>> {
        let mut output = Vec::new();
        let predicted = self.documents.iter().try_fold(
            RETAINED_CLOSURE_SUMMARY_BASE_BYTES_V2,
            |total, document| {
                let rdf_report_bytes = if document.rdf_report.is_some() {
                    RETAINED_CLOSURE_SUMMARY_RDF_REPORT_BYTES_V2
                } else {
                    0
                };
                total
                    .checked_add(RETAINED_CLOSURE_SUMMARY_DOCUMENT_BYTES_V2)
                    .and_then(|value| value.checked_add(rdf_report_bytes))
                    .ok_or_else(|| NativeError::limit("native closure summary size overflow"))
            },
        )?;
        self.ensure_additional_peak(predicted)?;
        output
            .try_reserve_exact(predicted)
            .map_err(|_| NativeError::limit("native closure summary allocation failed"))?;
        self.ensure_additional_peak(output.capacity())?;
        append(&mut output, RETAINED_CLOSURE_PREPARED_MAGIC_V2)?;
        append(
            &mut output,
            &RETAINED_CLOSURE_PREPARED_SCHEMA_V2.to_le_bytes(),
        )?;
        let flags = u16::from(
            self.documents
                .iter()
                .any(|document| document.source_map.is_some()),
        ) | (u16::from(
            self.documents
                .iter()
                .any(|document| document.origin_rows.is_some()),
        ) << 1)
            | (u16::from(
                self.documents
                    .iter()
                    .any(|document| document.rdf_report.is_some()),
            ) << 2);
        append(&mut output, &flags.to_le_bytes())?;
        append_u64(
            &mut output,
            u64::try_from(self.documents.len())
                .map_err(|_| NativeError::limit("native closure document count exceeds u64"))?,
        )?;
        for evidence in [
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
        for count in self.closure_counts {
            append_u64(&mut output, count)?;
        }
        append_u64(&mut output, self.closure_origin_count)?;
        for value in [
            self.max_facade_row_bytes,
            self.parser_summary_bytes_materialized,
            self.canonical_rows_scanned,
            self.structural_occurrence_rows_scanned,
            self.metadata_iri_objects_materialized,
            self.canonical_rows_encoded,
            self.canonical_bytes_encoded,
            self.fingerprint_temporary_bytes,
            self.origin_bytes_retained,
            prepare_ns,
        ] {
            append_u64(&mut output, value)?;
        }
        for document in &self.documents {
            append_u64(&mut output, document.document_fingerprint.preimage_bytes)?;
            append(&mut output, &document.document_fingerprint.digest)?;
            for count in document.raw_counts {
                append_u64(&mut output, count)?;
            }
            for count in document.effective_counts {
                append_u64(&mut output, count)?;
            }
            for value in [
                document.source_counts[0],
                document.source_counts[1],
                document.origin_counts[1],
                document.origin_counts[0],
            ] {
                append_u64(&mut output, value)?;
            }
            match &document.rdf_report {
                None => append(&mut output, &[0])?,
                Some(report) => {
                    append(&mut output, &[1, u8::from(report.conformant)])?;
                    for value in [
                        report.consumed_triples,
                        report.total_triples,
                        report.row_counts[0],
                        report.row_counts[1],
                        report.row_counts[2],
                    ] {
                        append_u64(&mut output, value)?;
                    }
                    append(&mut output, &report.digest)?;
                    append_u64(&mut output, report.retained_bytes)?;
                }
            }
        }
        debug_assert_eq!(output.len(), predicted);
        Ok(output)
    }

    pub(super) fn ensure_summary_copy_peak(
        &self,
        summary_capacity: usize,
        copied_bytes: usize,
    ) -> NativeResult<()> {
        self.ensure_additional_peak(
            summary_capacity
                .checked_add(copied_bytes)
                .ok_or_else(|| NativeError::limit("native closure summary copy size overflow"))?,
        )
    }

    fn ensure_additional_peak(&self, additional_bytes: usize) -> NativeResult<()> {
        enforce_evidence_peak(
            &self.evidence_limits,
            self.evidence_base_live_bytes,
            self.evidence_retained_bytes,
            additional_bytes,
        )
    }
}

#[allow(clippy::too_many_arguments)]
pub(super) fn prepare_closure(
    sources: Vec<TypedFacadeStorageV2>,
    metadata: &[Arc<RetainedParseMetadataV2>],
    manifest: &[u8],
    root_document_key: &str,
    document_keys: &[String],
    collect_provenance: bool,
    preserve_source_map: bool,
    effective_documents: &[Vec<u64>],
    closure_documents: &[u64],
    mut anonymous_scope_targets: Vec<Option<[u8; 32]>>,
    limits: Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    base_external_bytes: usize,
    fork_owner_bytes: usize,
    force_auxiliary_plan_failure: bool,
) -> NativeResult<(TypedFacadeStorageV2, PreparedClosurePublicationV2)> {
    if sources.len() != metadata.len()
        || sources.len() != document_keys.len()
        || sources.len() != anonymous_scope_targets.len()
    {
        return Err(NativeError::protocol(
            "native closure preparation inputs are not document-aligned",
        ));
    }
    for (target, retained) in anonymous_scope_targets.iter_mut().zip(metadata) {
        if target.is_some() && !retained.closure_has_scoped_roots() {
            *target = None;
        }
    }
    let scope_base_live_bytes = base_external_bytes
        .checked_add(fork_owner_bytes)
        .ok_or_else(|| NativeError::limit("native closure scope live-byte count overflow"))?;
    let (scope_maps, scope_map_bytes) = prepare_scope_digest_maps(
        &sources,
        metadata,
        &anonymous_scope_targets,
        &limits,
        cancellation.clone(),
        interrupt.clone(),
        scope_base_live_bytes,
    )?;
    let composite_external_bytes = base_external_bytes
        .checked_add(scope_map_bytes)
        .ok_or_else(|| NativeError::limit("native closure external-byte count overflow"))?;
    let storage = TypedFacadeBuilderV2::compose_native_documents(
        sources,
        effective_documents,
        closure_documents,
        &anonymous_scope_targets,
        limits,
        cancellation.clone(),
        interrupt.clone(),
        composite_external_bytes,
        fork_owner_bytes,
    )?;
    let prepared = prepare_composite_evidence(
        &storage,
        metadata,
        manifest,
        root_document_key,
        document_keys,
        collect_provenance,
        preserve_source_map,
        &scope_maps,
        &limits,
        cancellation,
        interrupt,
        composite_external_bytes,
        force_auxiliary_plan_failure,
    )?;
    Ok((storage, prepared))
}

type ScopeDigestMapV2 = Vec<([u8; 32], [u8; 32])>;

fn prepare_scope_digest_maps(
    sources: &[TypedFacadeStorageV2],
    metadata: &[Arc<RetainedParseMetadataV2>],
    targets: &[Option<[u8; 32]>],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    base_live_bytes: usize,
) -> NativeResult<(Vec<Option<ScopeDigestMapV2>>, usize)> {
    let mut output = Vec::new();
    let outer_bytes = sources
        .len()
        .checked_mul(size_of::<Option<ScopeDigestMapV2>>())
        .ok_or_else(|| NativeError::limit("native closure scope-map size overflow"))?;
    enforce_scope_workspace(limits, base_live_bytes, outer_bytes)?;
    cancellation.checkpoint()?;
    output
        .try_reserve_exact(sources.len())
        .map_err(|_| NativeError::limit("native closure scope-map allocation failed"))?;
    let retained_outer_bytes = scope_digest_map_bytes(&output, output.capacity())?;
    enforce_scope_workspace(limits, base_live_bytes, retained_outer_bytes)?;
    for ((source, metadata), target) in sources.iter().zip(metadata).zip(targets) {
        cancellation.checkpoint()?;
        let Some(target) = target else {
            output.push(None);
            continue;
        };
        if !metadata.closure_has_scoped_roots() {
            return Err(NativeError::protocol(
                "native closure scope target requires scoped parser roots",
            ));
        }
        let retained_map_bytes = scope_digest_map_bytes(&output, output.capacity())?;
        let canonical_row_temporary_bytes =
            usize::try_from(source.maximum_row_bytes()).map_err(|_| {
                NativeError::limit("native closure canonical row temporary exceeds usize")
            })?;
        preflight_scope_growth(
            limits,
            base_live_bytes,
            retained_map_bytes,
            canonical_row_temporary_bytes,
        )?;
        let raw_preflight =
            measure_document_rows(source, 0, true, cancellation.clone(), interrupt.clone())?;
        let prior_preflight =
            measure_document_rows(source, 0, false, cancellation.clone(), interrupt.clone())?;
        let preflight_workspace = retained_map_bytes
            .checked_add(raw_preflight)
            .and_then(|value| value.checked_add(prior_preflight))
            .and_then(|value| value.checked_add(canonical_row_temporary_bytes))
            .ok_or_else(|| NativeError::limit("native closure scope workspace overflow"))?;
        enforce_scope_workspace(limits, base_live_bytes, preflight_workspace)?;
        let (raw, raw_bytes) = collect_document_rows(
            source,
            0,
            true,
            ScopeRowBudget {
                limits,
                base_live_bytes,
                existing_workspace_bytes: retained_map_bytes,
                canonical_row_temporary_bytes,
            },
            cancellation.clone(),
            interrupt.clone(),
        )?;
        let prior_live_bytes = retained_map_bytes
            .checked_add(raw_bytes)
            .ok_or_else(|| NativeError::limit("native closure scope workspace overflow"))?;
        let (prior, prior_bytes) = collect_document_rows(
            source,
            0,
            false,
            ScopeRowBudget {
                limits,
                base_live_bytes,
                existing_workspace_bytes: prior_live_bytes,
                canonical_row_temporary_bytes,
            },
            cancellation.clone(),
            interrupt.clone(),
        )?;
        let row_bytes = raw_bytes
            .checked_add(prior_bytes)
            .ok_or_else(|| NativeError::limit("native closure scope workspace overflow"))?;
        let scope_workspace = retained_map_bytes
            .checked_add(row_bytes)
            .ok_or_else(|| NativeError::limit("native closure scope workspace overflow"))?;
        enforce_scope_workspace(limits, base_live_bytes, scope_workspace)?;
        let predicted_transform_peak = scope_workspace
            .checked_add(canonical_row_storage_bytes(&raw)?)
            .ok_or_else(|| NativeError::limit("native closure scope workspace overflow"))?;
        enforce_scope_workspace(limits, base_live_bytes, predicted_transform_peak)?;
        let mut guard = match interrupt.as_ref() {
            Some(slot) => Guard::with_interrupt(
                cancellation.clone(),
                limits.deadline,
                limits.cancellation_stride,
                slot.clone(),
            ),
            None => Guard::new(
                cancellation.clone(),
                limits.deadline,
                limits.cancellation_stride,
            ),
        };
        let mut session =
            scope_rescope_session(&mut guard, limits, base_live_bytes, scope_workspace)?;
        let transformed = crate::parse::rescope_anonymous_rows_v2(
            [raw[0].as_slice(), raw[1].as_slice(), raw[2].as_slice()],
            *target,
            &mut session,
            &cancellation,
        )?;
        session.finish()?;
        let transformed_bytes = canonical_row_storage_bytes(&transformed)?;
        let row_count = raw.iter().zip(&prior).zip(&transformed).try_fold(
            0_usize,
            |total, ((raw_rows, prior_rows), transformed_rows)| {
                if raw_rows.len() != prior_rows.len() || raw_rows.len() != transformed_rows.len() {
                    return Err(NativeError::protocol(
                        "native closure anonymous re-scope changed root cardinality",
                    ));
                }
                total
                    .checked_add(raw_rows.len())
                    .ok_or_else(|| NativeError::limit("native closure scope-map count overflow"))
            },
        )?;
        let pair_count = row_count
            .checked_mul(2)
            .ok_or_else(|| NativeError::limit("native closure scope-map count overflow"))?;
        let predicted_mapping_bytes = pair_count
            .checked_mul(size_of::<([u8; 32], [u8; 32])>())
            .ok_or_else(|| NativeError::limit("native closure scope-map size overflow"))?;
        let peak_workspace = scope_workspace
            .checked_add(transformed_bytes)
            .and_then(|value| value.checked_add(predicted_mapping_bytes))
            .ok_or_else(|| NativeError::limit("native closure scope workspace overflow"))?;
        enforce_scope_workspace(limits, base_live_bytes, peak_workspace)?;
        cancellation.checkpoint()?;
        let mut mapping = Vec::new();
        mapping
            .try_reserve_exact(pair_count)
            .map_err(|_| NativeError::limit("native closure scope-map allocation failed"))?;
        let mapping_bytes = mapping
            .capacity()
            .checked_mul(size_of::<([u8; 32], [u8; 32])>())
            .ok_or_else(|| NativeError::limit("native closure scope-map size overflow"))?;
        let actual_peak_workspace = scope_workspace
            .checked_add(transformed_bytes)
            .and_then(|value| value.checked_add(mapping_bytes))
            .ok_or_else(|| NativeError::limit("native closure scope workspace overflow"))?;
        enforce_scope_workspace(limits, base_live_bytes, actual_peak_workspace)?;
        for ((raw_rows, prior_rows), transformed_rows) in raw.iter().zip(&prior).zip(&transformed) {
            for ((raw_row, prior_row), transformed_row) in
                raw_rows.iter().zip(prior_rows).zip(transformed_rows)
            {
                cancellation.checkpoint()?;
                let selected = structural_digest_v2(transformed_row);
                mapping.push((structural_digest_v2(raw_row), selected));
                mapping.push((structural_digest_v2(prior_row), selected));
            }
        }
        cancellation.checkpoint()?;
        mapping.sort_unstable();
        cancellation.checkpoint()?;
        if mapping
            .windows(2)
            .any(|pair| pair[0].0 == pair[1].0 && pair[0].1 != pair[1].1)
        {
            return Err(NativeError::protocol(
                "native closure scope mapping is not functional",
            ));
        }
        mapping.dedup_by_key(|(source, _target)| *source);
        output.push(Some(mapping));
    }
    let retained_bytes = scope_digest_map_bytes(&output, output.capacity())?;
    enforce_scope_workspace(limits, base_live_bytes, retained_bytes)?;
    Ok((output, retained_bytes))
}

fn scope_rescope_session<'a>(
    guard: &'a mut Guard,
    limits: &'a Limits,
    base_live_bytes: usize,
    scope_workspace: usize,
) -> NativeResult<Session<'a>> {
    // The copied raw/prior rows and any retained maps remain live while the
    // anonymous re-scope session builds its parsed and encoded temporaries.
    // Seed both budgets with that workspace so max_temporary_bytes, like
    // max_memory_bytes, applies to their aggregate rather than to two
    // independently admitted allocation domains.
    let mut session = Session::new(guard, limits, base_live_bytes)?;
    session.reserve_temporary_bytes(scope_workspace)?;
    Ok(session)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rescope_session_aggregates_live_scope_workspace_at_the_exact_temporary_boundary() {
        let limits = Limits::default();
        let Ok(maximum) = usize::try_from(limits.value(LimitKey::MaxTemporaryBytes)) else {
            return;
        };
        let cancellation = Cancellation::with_duration(None);
        let mut guard = Guard::new(cancellation, limits.deadline, limits.cancellation_stride);
        let mut session =
            scope_rescope_session(&mut guard, &limits, 0, maximum - 1).expect("scope session");
        session
            .reserve_temporary_bytes(1)
            .expect("exact aggregate temporary boundary");
        let error = session
            .reserve_temporary_bytes(1)
            .expect_err("aggregate temporary boundary must reject one extra byte");
        assert_eq!(error.code, "NATIVE_WIRE_LIMIT");
        assert!(error.message.contains("max_temporary_bytes"));
    }
}

fn scope_digest_map_bytes(
    maps: &[Option<ScopeDigestMapV2>],
    outer_capacity: usize,
) -> NativeResult<usize> {
    outer_capacity
        .checked_mul(size_of::<Option<ScopeDigestMapV2>>())
        .and_then(|outer| {
            maps.iter().try_fold(outer, |total, mapping| {
                mapping.as_ref().map_or(Some(total), |mapping| {
                    mapping
                        .capacity()
                        .checked_mul(size_of::<([u8; 32], [u8; 32])>())
                        .and_then(|bytes| total.checked_add(bytes))
                })
            })
        })
        .ok_or_else(|| NativeError::limit("native closure scope-map size overflow"))
}

fn enforce_scope_workspace(
    limits: &Limits,
    base_live_bytes: usize,
    workspace_bytes: usize,
) -> NativeResult<()> {
    let workspace = u64::try_from(workspace_bytes)
        .map_err(|_| NativeError::limit("native closure scope workspace exceeds u64"))?;
    if workspace > limits.value(LimitKey::MaxTemporaryBytes) {
        return Err(limits.resource_limit(
            LimitKey::MaxTemporaryBytes,
            workspace,
            "native closure scope workspace exceeds max_temporary_bytes",
        ));
    }
    let live = base_live_bytes
        .checked_add(workspace_bytes)
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(|| NativeError::limit("native closure scope live-byte count overflow"))?;
    if let Some(maximum) = limits.max_memory_bytes.filter(|maximum| live > *maximum) {
        return Err(NativeError::resource_limit(
            "max_memory_bytes",
            live,
            maximum,
            "native closure scope workspace exceeds max_memory_bytes",
        ));
    }
    Ok(())
}

struct ClosureEvidenceBudget<'a> {
    limits: &'a Limits,
    base_live_bytes: usize,
    retained_bytes: usize,
}

impl<'a> ClosureEvidenceBudget<'a> {
    fn new(limits: &'a Limits, base_live_bytes: usize) -> NativeResult<Self> {
        let budget = Self {
            limits,
            base_live_bytes,
            retained_bytes: 0,
        };
        budget.ensure_temporary(0)?;
        Ok(budget)
    }

    fn ensure_temporary(&self, temporary_bytes: usize) -> NativeResult<()> {
        enforce_evidence_peak(
            self.limits,
            self.base_live_bytes,
            self.retained_bytes,
            temporary_bytes,
        )
    }

    fn retain(&mut self, bytes: usize) -> NativeResult<()> {
        self.ensure_temporary(bytes)?;
        self.retained_bytes = self
            .retained_bytes
            .checked_add(bytes)
            .ok_or_else(|| NativeError::limit("native closure retained evidence overflow"))?;
        Ok(())
    }

    fn release(&mut self, bytes: usize) -> NativeResult<()> {
        self.retained_bytes = self
            .retained_bytes
            .checked_sub(bytes)
            .ok_or_else(|| NativeError::protocol("native closure retained evidence underflow"))?;
        Ok(())
    }
}

fn enforce_evidence_peak(
    limits: &Limits,
    base_live_bytes: usize,
    retained_bytes: usize,
    temporary_bytes: usize,
) -> NativeResult<()> {
    let workspace = retained_bytes
        .checked_add(temporary_bytes)
        .ok_or_else(|| NativeError::limit("native closure evidence workspace overflow"))?;
    let workspace_u64 = u64::try_from(workspace)
        .map_err(|_| NativeError::limit("native closure evidence workspace exceeds u64"))?;
    if workspace_u64 > limits.value(LimitKey::MaxTemporaryBytes) {
        return Err(limits.resource_limit(
            LimitKey::MaxTemporaryBytes,
            workspace_u64,
            "native closure evidence exceeds max_temporary_bytes",
        ));
    }
    let live = base_live_bytes
        .checked_add(workspace)
        .and_then(|value| u64::try_from(value).ok())
        .ok_or_else(|| NativeError::limit("native closure evidence live-byte overflow"))?;
    if let Some(maximum) = limits.max_memory_bytes.filter(|maximum| live > *maximum) {
        return Err(NativeError::resource_limit(
            "max_memory_bytes",
            live,
            maximum,
            "native closure evidence exceeds max_memory_bytes",
        ));
    }
    Ok(())
}

#[derive(Clone, Copy)]
struct ScopeRowBudget<'a> {
    limits: &'a Limits,
    base_live_bytes: usize,
    existing_workspace_bytes: usize,
    canonical_row_temporary_bytes: usize,
}

fn collect_document_rows(
    storage: &TypedFacadeStorageV2,
    document_ordinal: u64,
    raw_owner_role: bool,
    budget: ScopeRowBudget<'_>,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
) -> NativeResult<([Vec<Vec<u8>>; 3], usize)> {
    let mut output: [Vec<Vec<u8>>; 3] = Default::default();
    let workspace_floor = budget
        .existing_workspace_bytes
        .checked_add(budget.canonical_row_temporary_bytes)
        .ok_or_else(|| NativeError::limit("native closure scope workspace overflow"))?;
    enforce_scope_workspace(budget.limits, budget.base_live_bytes, workspace_floor)?;
    let mut workspace_bytes = workspace_floor;
    for (target, collection) in output.iter_mut().zip(structural_collections()) {
        let count = storage.canonical_root_count(
            collection,
            TypedFacadeScopeV2::Document,
            Some(document_ordinal),
            raw_owner_role,
        )?;
        let expected_count = usize::try_from(count)
            .map_err(|_| NativeError::limit("native closure root count exceeds usize"))?;
        let requested_outer_bytes = expected_count
            .checked_mul(size_of::<Vec<u8>>())
            .ok_or_else(|| NativeError::limit("native closure root row size overflow"))?;
        preflight_scope_growth(
            budget.limits,
            budget.base_live_bytes,
            workspace_bytes,
            requested_outer_bytes,
        )?;
        target
            .try_reserve_exact(expected_count)
            .map_err(|_| NativeError::limit("native closure root row allocation failed"))?;
        let outer_bytes = target
            .capacity()
            .checked_mul(size_of::<Vec<u8>>())
            .ok_or_else(|| NativeError::limit("native closure root row size overflow"))?;
        workspace_bytes = preflight_scope_growth(
            budget.limits,
            budget.base_live_bytes,
            workspace_bytes,
            outer_bytes,
        )?;
        storage.visit_canonical_roots(
            collection,
            TypedFacadeScopeV2::Document,
            Some(document_ordinal),
            raw_owner_role,
            cancellation.clone(),
            interrupt.clone(),
            |row| {
                if target.len() >= expected_count {
                    return Err(NativeError::protocol(
                        "native closure root traversal exceeds its count",
                    ));
                }
                preflight_scope_growth(
                    budget.limits,
                    budget.base_live_bytes,
                    workspace_bytes,
                    row.len(),
                )?;
                let mut copied = Vec::new();
                copied.try_reserve_exact(row.len()).map_err(|_| {
                    NativeError::limit("native closure root row payload allocation failed")
                })?;
                workspace_bytes = preflight_scope_growth(
                    budget.limits,
                    budget.base_live_bytes,
                    workspace_bytes,
                    copied.capacity(),
                )?;
                copied.extend_from_slice(row);
                target.push(copied);
                Ok(())
            },
        )?;
        if u64::try_from(target.len()).ok() != Some(count) {
            return Err(NativeError::protocol(
                "native closure root traversal diverges from its count",
            ));
        }
    }
    let retained_bytes = workspace_bytes
        .checked_sub(workspace_floor)
        .ok_or_else(|| NativeError::protocol("native closure scope accounting is inconsistent"))?;
    if retained_bytes != canonical_row_storage_bytes(&output)? {
        return Err(NativeError::protocol(
            "native closure scope row accounting diverges",
        ));
    }
    Ok((output, retained_bytes))
}

fn measure_document_rows(
    storage: &TypedFacadeStorageV2,
    document_ordinal: u64,
    raw_owner_role: bool,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
) -> NativeResult<usize> {
    let mut measured_bytes = 0_usize;
    for collection in structural_collections() {
        let count = storage.canonical_root_count(
            collection,
            TypedFacadeScopeV2::Document,
            Some(document_ordinal),
            raw_owner_role,
        )?;
        let expected_count = usize::try_from(count)
            .map_err(|_| NativeError::limit("native closure root count exceeds usize"))?;
        measured_bytes = expected_count
            .checked_mul(size_of::<Vec<u8>>())
            .and_then(|bytes| measured_bytes.checked_add(bytes))
            .ok_or_else(|| NativeError::limit("native closure root row size overflow"))?;
        let mut emitted = 0_usize;
        storage.visit_canonical_roots(
            collection,
            TypedFacadeScopeV2::Document,
            Some(document_ordinal),
            raw_owner_role,
            cancellation.clone(),
            interrupt.clone(),
            |row| {
                if emitted >= expected_count {
                    return Err(NativeError::protocol(
                        "native closure root traversal exceeds its count",
                    ));
                }
                measured_bytes = measured_bytes
                    .checked_add(row.len())
                    .ok_or_else(|| NativeError::limit("native closure root row size overflow"))?;
                emitted = emitted
                    .checked_add(1)
                    .ok_or_else(|| NativeError::limit("native closure root count overflow"))?;
                Ok(())
            },
        )?;
        if emitted != expected_count {
            return Err(NativeError::protocol(
                "native closure root traversal diverges from its count",
            ));
        }
    }
    Ok(measured_bytes)
}

fn preflight_scope_growth(
    limits: &Limits,
    base_live_bytes: usize,
    current_workspace_bytes: usize,
    growth_bytes: usize,
) -> NativeResult<usize> {
    let following = current_workspace_bytes
        .checked_add(growth_bytes)
        .ok_or_else(|| NativeError::limit("native closure scope workspace overflow"))?;
    enforce_scope_workspace(limits, base_live_bytes, following)?;
    Ok(following)
}

fn canonical_row_storage_bytes(rows: &[Vec<Vec<u8>>; 3]) -> NativeResult<usize> {
    rows.iter().flatten().try_fold(0_usize, |total, row| {
        total
            .checked_add(size_of::<Vec<u8>>())
            .and_then(|value| value.checked_add(row.capacity()))
            .ok_or_else(|| NativeError::limit("native closure canonical row size overflow"))
    })
}

fn row_table_storage_bytes(rows: &[Vec<u8>], outer_capacity: usize) -> NativeResult<usize> {
    rows.iter().try_fold(
        outer_capacity
            .checked_mul(size_of::<Vec<u8>>())
            .ok_or_else(|| NativeError::limit("native closure row-table size overflow"))?,
        |total, row| {
            total
                .checked_add(row.capacity())
                .ok_or_else(|| NativeError::limit("native closure row-table size overflow"))
        },
    )
}

fn source_map_storage_bytes(rows: &TypedSourceMapRowsV2) -> NativeResult<usize> {
    row_table_storage_bytes(&rows.entries, rows.entries.capacity())?
        .checked_add(row_table_storage_bytes(
            &rows.prefixes,
            rows.prefixes.capacity(),
        )?)
        .ok_or_else(|| NativeError::limit("native closure source-map size overflow"))
}

fn rdf_report_storage_bytes(report: &PreparedClosureRdfReportV2) -> NativeResult<usize> {
    report
        .rows
        .header
        .capacity()
        .checked_add(row_table_storage_bytes(
            &report.rows.unconsumed_triples,
            report.rows.unconsumed_triples.capacity(),
        )?)
        .and_then(|value| {
            row_table_storage_bytes(&report.rows.rule_ids, report.rows.rule_ids.capacity())
                .ok()
                .and_then(|bytes| value.checked_add(bytes))
        })
        .and_then(|value| {
            row_table_storage_bytes(&report.rows.diagnostics, report.rows.diagnostics.capacity())
                .ok()
                .and_then(|bytes| value.checked_add(bytes))
        })
        .ok_or_else(|| NativeError::limit("native closure RDF report size overflow"))
}

fn prepared_auxiliary_storage_bytes(
    documents: &[PreparedClosureDocumentV2],
) -> NativeResult<usize> {
    documents.iter().try_fold(0_usize, |total, document| {
        let source = document
            .source_map
            .as_ref()
            .map_or(Ok(0_usize), source_map_storage_bytes)?;
        let effective_origins = document.origin_rows.as_ref().map_or(Ok(0_usize), |rows| {
            row_table_storage_bytes(rows, rows.capacity())
        })?;
        let raw_origins = document
            .raw_origin_rows
            .as_ref()
            .map_or(Ok(0_usize), |rows| {
                row_table_storage_bytes(rows, rows.capacity())
            })?;
        let rdf = document
            .rdf_report
            .as_ref()
            .map_or(Ok(0_usize), rdf_report_storage_bytes)?;
        total
            .checked_add(source)
            .and_then(|value| value.checked_add(effective_origins))
            .and_then(|value| value.checked_add(raw_origins))
            .and_then(|value| value.checked_add(rdf))
            .ok_or_else(|| NativeError::limit("native closure auxiliary storage size overflow"))
    })
}

fn retain_staging_capacity<T>(
    rows: &mut Vec<T>,
    count: usize,
    retained_staging_bytes: &mut usize,
    budget: &mut ClosureEvidenceBudget<'_>,
    label: &'static str,
) -> NativeResult<()> {
    let predicted = count
        .checked_mul(size_of::<T>())
        .ok_or_else(|| NativeError::limit(label))?;
    budget.ensure_temporary(
        retained_staging_bytes
            .checked_add(predicted)
            .ok_or_else(|| NativeError::limit(label))?,
    )?;
    rows.try_reserve_exact(count)
        .map_err(|_| NativeError::limit(label))?;
    let actual = rows
        .capacity()
        .checked_mul(size_of::<T>())
        .ok_or_else(|| NativeError::limit(label))?;
    *retained_staging_bytes = retained_staging_bytes
        .checked_add(actual)
        .ok_or_else(|| NativeError::limit(label))?;
    budget.ensure_temporary(*retained_staging_bytes)
}

fn prepare_auxiliary_plan(
    documents: &mut [PreparedClosureDocumentV2],
    document_count: u64,
    budget: &mut ClosureEvidenceBudget<'_>,
    cancellation: &Cancellation,
    force_failure_after_staging: bool,
) -> NativeResult<PreparedTypedAuxiliaryV2> {
    let expected = usize::try_from(document_count)
        .map_err(|_| NativeError::limit("native closure document count exceeds usize"))?;
    if documents.len() != expected {
        return Err(NativeError::protocol(
            "native closure auxiliary documents are not aligned",
        ));
    }
    let source_present = documents
        .first()
        .is_some_and(|document| document.source_map.is_some());
    let origin_present = documents
        .first()
        .is_some_and(|document| document.origin_rows.is_some());
    if documents.iter().any(|document| {
        document.source_map.is_some() != source_present
            || document.origin_rows.is_some() != origin_present
            || document.raw_origin_rows.is_some() != origin_present
    }) {
        return Err(NativeError::protocol(
            "native closure auxiliary capabilities are not document-aligned",
        ));
    }
    let rdf_present = documents
        .iter()
        .any(|document| document.rdf_report.is_some());
    let input_storage_bytes = prepared_auxiliary_storage_bytes(documents)?;

    let mut retained_staging_bytes = 0_usize;
    let mut origins = origin_present.then(Vec::new);
    let mut raw_origins = origin_present.then(Vec::new);
    let mut source_maps = source_present.then(Vec::new);
    let mut rdf_reports = rdf_present.then(Vec::new);
    if let Some(rows) = origins.as_mut() {
        retain_staging_capacity(
            rows,
            expected,
            &mut retained_staging_bytes,
            budget,
            "native closure origin staging size overflow",
        )?;
    }
    if let Some(rows) = raw_origins.as_mut() {
        retain_staging_capacity(
            rows,
            expected,
            &mut retained_staging_bytes,
            budget,
            "native closure raw-origin staging size overflow",
        )?;
    }
    if let Some(rows) = source_maps.as_mut() {
        retain_staging_capacity(
            rows,
            expected,
            &mut retained_staging_bytes,
            budget,
            "native closure source-map staging size overflow",
        )?;
    }
    if let Some(rows) = rdf_reports.as_mut() {
        retain_staging_capacity(
            rows,
            expected,
            &mut retained_staging_bytes,
            budget,
            "native closure RDF staging size overflow",
        )?;
    }
    budget.retain(retained_staging_bytes)?;

    for document in documents {
        cancellation.checkpoint()?;
        if let Some(rows) = origins.as_mut() {
            rows.push(std::mem::take(
                document
                    .origin_rows
                    .as_mut()
                    .expect("validated effective origin capability"),
            ));
        }
        if let Some(rows) = raw_origins.as_mut() {
            rows.push(std::mem::take(
                document
                    .raw_origin_rows
                    .as_mut()
                    .expect("validated raw origin capability"),
            ));
        }
        if let Some(rows) = source_maps.as_mut() {
            let source = document
                .source_map
                .as_mut()
                .expect("validated source-map capability");
            rows.push(TypedSourceMapRowsV2 {
                entries: std::mem::take(&mut source.entries),
                prefixes: std::mem::take(&mut source.prefixes),
            });
        }
        if let Some(rows) = rdf_reports.as_mut() {
            rows.push(
                document
                    .rdf_report
                    .as_mut()
                    .map(|report| TypedRdfReportRowsV2 {
                        header: std::mem::take(&mut report.rows.header),
                        unconsumed_triples: std::mem::take(&mut report.rows.unconsumed_triples),
                        rule_ids: std::mem::take(&mut report.rows.rule_ids),
                        diagnostics: std::mem::take(&mut report.rows.diagnostics),
                    }),
            );
        }
    }
    if force_failure_after_staging {
        return Err(NativeError::limit(
            "injected native closure auxiliary plan preparation failure",
        ));
    }

    let prepared = {
        let mut check_workspace = |additional| {
            budget
                .ensure_temporary(additional)
                .map_err(map_auxiliary_budget_error)
        };
        crate::publication::prepare_typed_auxiliary_documents_v2(
            origins,
            raw_origins,
            source_maps,
            rdf_reports,
            document_count,
            cancellation,
            &mut check_workspace,
        )?
    };
    budget
        .ensure_temporary(prepared.preparation_peak_additional_bytes())
        .map_err(map_auxiliary_budget_error)?;
    let retained_owner_bytes = prepared.retained_owner_bytes()?;
    budget.release(
        input_storage_bytes
            .checked_add(retained_staging_bytes)
            .ok_or_else(|| {
                NativeError::limit("native closure auxiliary replacement size overflow")
            })?,
    )?;
    budget
        .retain(retained_owner_bytes)
        .map_err(map_auxiliary_budget_error)?;
    Ok(prepared)
}

fn map_auxiliary_budget_error(error: NativeError) -> NativeError {
    error
}

const fn structural_collections() -> [TypedFacadeCollectionV2; 3] {
    [
        TypedFacadeCollectionV2::OntologyAnnotations,
        TypedFacadeCollectionV2::Axioms,
        TypedFacadeCollectionV2::Extensions,
    ]
}

#[allow(clippy::too_many_arguments)]
fn prepare_composite_evidence(
    storage: &TypedFacadeStorageV2,
    metadata: &[Arc<RetainedParseMetadataV2>],
    manifest: &[u8],
    root_document_key: &str,
    document_keys: &[String],
    collect_provenance: bool,
    preserve_source_map: bool,
    scope_maps: &[Option<ScopeDigestMapV2>],
    limits: &Limits,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    external_live_bytes: usize,
    force_auxiliary_plan_failure: bool,
) -> NativeResult<PreparedClosurePublicationV2> {
    validate_document_keys(document_keys, root_document_key)?;
    if storage.document_count()
        != u64::try_from(document_keys.len())
            .map_err(|_| NativeError::limit("native closure document count exceeds u64"))?
    {
        return Err(NativeError::protocol(
            "native closure storage document count diverges from metadata",
        ));
    }
    let retained_owner_bytes = usize::try_from(storage.counters()?.retained_owner_bytes)
        .map_err(|_| NativeError::limit("native closure retained owner exceeds usize"))?;
    let evidence_base_live_bytes = retained_owner_bytes
        .checked_add(external_live_bytes)
        .ok_or_else(|| NativeError::limit("native closure evidence live-byte overflow"))?;
    let mut evidence_budget = ClosureEvidenceBudget::new(limits, evidence_base_live_bytes)?;
    let canonical_row_temporary_bytes = usize::try_from(storage.maximum_row_bytes())
        .map_err(|_| NativeError::limit("native closure canonical row temporary exceeds usize"))?;
    evidence_budget.ensure_temporary(canonical_row_temporary_bytes)?;
    let ingestion = metadata.iter().try_fold(
        [0_u64; 4],
        |mut totals, selected| -> NativeResult<[u64; 4]> {
            for (total, value) in totals.iter_mut().zip(selected.closure_ingestion_counters()) {
                *total = total.checked_add(value).ok_or_else(|| {
                    NativeError::limit("native closure ingestion counter overflow")
                })?;
            }
            Ok(totals)
        },
    )?;

    let document_count = u64::try_from(document_keys.len())
        .map_err(|_| NativeError::limit("native closure document count exceeds u64"))?;
    let mut raw_manifest = MeasuredSha256::domain(ROOT_TABLE_MANIFEST_DOMAIN_V2)?;
    raw_manifest.u32_le(2)?;
    raw_manifest.u64_le(document_count)?;
    let mut effective_manifest = MeasuredSha256::domain(EFFECTIVE_ROOT_TABLE_MANIFEST_DOMAIN_V2)?;
    effective_manifest.u32_le(2)?;
    effective_manifest.u64_le(document_count)?;
    let mut structural = MeasuredSha256::new();
    structural.update(STRUCTURAL_FINGERPRINT_DOMAIN_V2)?;
    structural.frame_varint(manifest)?;

    let mut documents = Vec::new();
    let predicted_document_bytes = document_keys
        .len()
        .checked_mul(size_of::<PreparedClosureDocumentV2>())
        .ok_or_else(|| NativeError::limit("native prepared closure document size overflow"))?;
    evidence_budget.ensure_temporary(predicted_document_bytes)?;
    cancellation.checkpoint()?;
    documents
        .try_reserve_exact(document_keys.len())
        .map_err(|_| NativeError::limit("native prepared closure document allocation failed"))?;
    let retained_document_bytes = documents
        .capacity()
        .checked_mul(size_of::<PreparedClosureDocumentV2>())
        .ok_or_else(|| NativeError::limit("native prepared closure document size overflow"))?;
    evidence_budget.retain(retained_document_bytes)?;
    let mut canonical_rows_encoded = 0_u64;
    let mut canonical_bytes_encoded = 0_u64;
    let mut max_facade_row_bytes = storage.maximum_row_bytes();
    let mut aggregate_term_budget = ScanBudget::from_limits(limits);

    for (ordinal, ((document_key, metadata), scope_map)) in document_keys
        .iter()
        .zip(metadata)
        .zip(scope_maps)
        .enumerate()
    {
        cancellation.checkpoint()?;
        evidence_budget.ensure_temporary(canonical_row_temporary_bytes)?;
        let ordinal = u64::try_from(ordinal)
            .map_err(|_| NativeError::limit("native closure document ordinal exceeds u64"))?;
        let mut raw_document = MeasuredSha256::domain(DOCUMENT_ROOT_TABLE_DOMAIN_V2)?;
        raw_document.text64(document_key)?;
        let mut effective_document =
            MeasuredSha256::domain(EFFECTIVE_DOCUMENT_ROOT_TABLE_DOMAIN_V2)?;
        effective_document.text64(document_key)?;
        structural.frame_varint(document_key.as_bytes())?;

        let mut raw_counts = [0_u64; 3];
        let mut effective_counts = [0_u64; 3];
        for (index, (tag, collection)) in [1_u8, 2, 3]
            .into_iter()
            .zip(structural_collections())
            .enumerate()
        {
            let raw_count = storage.canonical_root_count(
                collection,
                TypedFacadeScopeV2::Document,
                Some(ordinal),
                true,
            )?;
            let effective_count = storage.canonical_root_count(
                collection,
                TypedFacadeScopeV2::Document,
                Some(ordinal),
                false,
            )?;
            raw_counts[index] = raw_count;
            effective_counts[index] = effective_count;
            raw_document.update(&[tag])?;
            raw_document.u64_le(raw_count)?;
            effective_document.update(&[tag])?;
            effective_document.u64_le(effective_count)?;
            structural.varint(effective_count)?;
            storage.visit_canonical_roots(
                collection,
                TypedFacadeScopeV2::Document,
                Some(ordinal),
                true,
                cancellation.clone(),
                interrupt.clone(),
                |row| {
                    scan_canonical(row, &mut aggregate_term_budget)?;
                    account_canonical_row(
                        row,
                        &mut canonical_rows_encoded,
                        &mut canonical_bytes_encoded,
                        &mut max_facade_row_bytes,
                    )?;
                    raw_document.frame64(row)
                },
            )?;
            storage.visit_canonical_roots(
                collection,
                TypedFacadeScopeV2::Document,
                Some(ordinal),
                false,
                cancellation.clone(),
                interrupt.clone(),
                |row| {
                    account_canonical_row(
                        row,
                        &mut canonical_rows_encoded,
                        &mut canonical_bytes_encoded,
                        &mut max_facade_row_bytes,
                    )?;
                    effective_document.frame64(row)?;
                    structural.frame_varint(row)
                },
            )?;
        }
        if raw_counts != metadata.closure_root_counts() {
            return Err(NativeError::protocol(
                "native closure parser metadata diverges from raw roots",
            ));
        }
        let raw_document_digest = raw_document.finish().digest;
        let effective_document_digest = effective_document.finish().digest;
        raw_manifest.text64(document_key)?;
        effective_manifest.text64(document_key)?;
        for count in raw_counts {
            raw_manifest.u64_le(count)?;
        }
        for count in effective_counts {
            effective_manifest.u64_le(count)?;
        }
        raw_manifest.update(&raw_document_digest)?;
        effective_manifest.update(&effective_document_digest)?;

        let source_map = {
            let mut check_workspace = |temporary| evidence_budget.ensure_temporary(temporary);
            metadata.prepare_closure_source_map(
                preserve_source_map,
                limits,
                &cancellation,
                &mut check_workspace,
            )?
        };
        if let Some(rows) = source_map.as_ref() {
            evidence_budget.retain(source_map_storage_bytes(rows)?)?;
        }
        let source_counts = source_map.as_ref().map_or([0_u64; 2], |rows| {
            [
                u64::try_from(rows.entries.len()).unwrap_or(u64::MAX),
                u64::try_from(rows.prefixes.len()).unwrap_or(u64::MAX),
            ]
        });
        if source_counts.contains(&u64::MAX) {
            return Err(NativeError::limit(
                "native closure source-map count exceeds u64",
            ));
        }
        let (origin_rows, raw_origin_rows) = if collect_provenance {
            let effective = {
                let mut check_workspace = |temporary| evidence_budget.ensure_temporary(temporary);
                metadata.prepare_closure_origin_rows(
                    document_key,
                    false,
                    scope_map.as_deref(),
                    limits,
                    &cancellation,
                    &mut check_workspace,
                )?
            };
            evidence_budget.retain(row_table_storage_bytes(&effective, effective.capacity())?)?;
            let raw = {
                let mut check_workspace = |temporary| evidence_budget.ensure_temporary(temporary);
                metadata.prepare_closure_origin_rows(
                    document_key,
                    true,
                    None,
                    limits,
                    &cancellation,
                    &mut check_workspace,
                )?
            };
            evidence_budget.retain(row_table_storage_bytes(&raw, raw.capacity())?)?;
            (Some(effective), Some(raw))
        } else {
            (None, None)
        };
        let origin_counts = [
            origin_rows
                .as_ref()
                .map_or(Ok(0_u64), |rows| u64::try_from(rows.len()))
                .map_err(|_| NativeError::limit("native closure origin count exceeds u64"))?,
            raw_origin_rows
                .as_ref()
                .map_or(Ok(0_u64), |rows| u64::try_from(rows.len()))
                .map_err(|_| NativeError::limit("native closure raw origin count exceeds u64"))?,
        ];
        let rdf_report = {
            let mut check_workspace = |temporary| evidence_budget.ensure_temporary(temporary);
            metadata
                .prepare_closure_rdf_report(
                    document_key,
                    limits,
                    &cancellation,
                    &mut check_workspace,
                )?
                .map(
                    |(rows, conformant, consumed, total, digest, retained_bytes)| {
                        let row_counts = [
                            u64::try_from(rows.unconsumed_triples.len()).map_err(|_| {
                                NativeError::limit("native RDF unconsumed count exceeds u64")
                            })?,
                            u64::try_from(rows.rule_ids.len()).map_err(|_| {
                                NativeError::limit("native RDF rule count exceeds u64")
                            })?,
                            u64::try_from(rows.diagnostics.len()).map_err(|_| {
                                NativeError::limit("native RDF diagnostic count exceeds u64")
                            })?,
                        ];
                        Ok(PreparedClosureRdfReportV2 {
                            rows,
                            row_counts,
                            conformant,
                            consumed_triples: consumed,
                            total_triples: total,
                            digest,
                            retained_bytes,
                        })
                    },
                )
                .transpose()?
        };
        if let Some(report) = rdf_report.as_ref() {
            evidence_budget.retain(rdf_report_storage_bytes(report)?)?;
        }
        let (preimage_bytes, digest) = metadata.closure_document_fingerprint();
        if preimage_bytes < u64::try_from(DOCUMENT_FINGERPRINT_DOMAIN_V2.len()).unwrap_or(u64::MAX)
        {
            return Err(NativeError::protocol(
                "native closure document fingerprint evidence is invalid",
            ));
        }
        evidence_budget.ensure_temporary(document_key.len())?;
        let owned_document_key: Box<str> = document_key.as_str().into();
        evidence_budget.retain(owned_document_key.len())?;
        documents.push(PreparedClosureDocumentV2 {
            document_key: owned_document_key,
            document_fingerprint: FingerprintEvidenceV2 {
                preimage_bytes,
                digest,
            },
            raw_counts,
            effective_counts,
            source_map,
            source_counts,
            origin_rows,
            raw_origin_rows,
            origin_counts,
            rdf_report,
        });
    }

    let closure_counts = closure_structural_counts(storage)?;
    let structural_fingerprint = structural.finish();
    let (logical_fingerprint, logical_workspace, logical_rows, logical_bytes) =
        prepare_logical_fingerprint(
            storage,
            cancellation.clone(),
            interrupt.clone(),
            &evidence_budget,
        )?;
    canonical_rows_encoded = checked_add(
        canonical_rows_encoded,
        logical_rows,
        "native closure canonical row count overflow",
    )?;
    canonical_bytes_encoded = checked_add(
        canonical_bytes_encoded,
        logical_bytes,
        "native closure canonical byte count overflow",
    )?;
    let (signature_fingerprint, signature_rows, signature_bytes, signature_max) =
        prepare_signature_fingerprint(
            storage,
            cancellation.clone(),
            interrupt.clone(),
            &evidence_budget,
        )?;
    canonical_rows_encoded = checked_add(
        canonical_rows_encoded,
        signature_rows,
        "native closure canonical row count overflow",
    )?;
    canonical_bytes_encoded = checked_add(
        canonical_bytes_encoded,
        signature_bytes,
        "native closure canonical byte count overflow",
    )?;
    max_facade_row_bytes = max_facade_row_bytes.max(signature_max);

    let closure_origin_rows = if collect_provenance {
        Some(merge_closure_origins(
            &documents,
            &evidence_budget,
            &cancellation,
        )?)
    } else {
        None
    };
    let closure_origin_storage_bytes =
        closure_origin_rows.as_ref().map_or(Ok(0_usize), |rows| {
            row_table_storage_bytes(rows, rows.capacity())
        })?;
    evidence_budget.retain(closure_origin_storage_bytes)?;
    let closure_origin_count = closure_origin_rows.as_ref().map_or(Ok(0_u64), |rows| {
        u64::try_from(rows.len())
            .map_err(|_| NativeError::limit("native closure origin count exceeds u64"))
    })?;
    let origin_bytes_retained = retained_origin_bytes(&documents, closure_origin_rows.as_deref())?;
    max_facade_row_bytes = max_facade_row_bytes
        .max(max_source_row(&documents))
        .max(max_origin_row(&documents, closure_origin_rows.as_deref()))
        .max(max_rdf_row(&documents))
        .max(1);

    let content = ContentDigestsV2 {
        root_table_sha256: raw_manifest.finish().digest,
        effective_root_table_sha256: effective_manifest.finish().digest,
        fingerprint_inputs_sha256: fingerprint_inputs_digest(
            &documents,
            root_document_key,
            structural_fingerprint,
            logical_fingerprint,
            signature_fingerprint,
        )?,
        source_manifest_sha256: source_manifest_digest(&documents, preserve_source_map)?,
        provenance_manifest_sha256: provenance_manifest_digest(&documents, collect_provenance)?,
        effective_origin_manifest_sha256: effective_origin_manifest_digest(
            &documents,
            closure_origin_rows.as_deref().unwrap_or_default(),
        )?,
    };
    drop(closure_origin_rows);
    evidence_budget.release(closure_origin_storage_bytes)?;
    let auxiliary = prepare_auxiliary_plan(
        &mut documents,
        document_count,
        &mut evidence_budget,
        &cancellation,
        force_auxiliary_plan_failure,
    )?;
    cancellation.checkpoint()?;
    Ok(PreparedClosurePublicationV2 {
        documents,
        structural_fingerprint,
        logical_fingerprint,
        signature_fingerprint,
        content,
        closure_counts,
        closure_origin_count,
        auxiliary,
        max_facade_row_bytes,
        parser_summary_bytes_materialized: ingestion[0],
        canonical_rows_scanned: ingestion[1],
        structural_occurrence_rows_scanned: ingestion[2],
        metadata_iri_objects_materialized: ingestion[3],
        canonical_rows_encoded,
        canonical_bytes_encoded,
        fingerprint_temporary_bytes: logical_workspace,
        origin_bytes_retained,
        evidence_limits: *limits,
        evidence_base_live_bytes,
        evidence_retained_bytes: evidence_budget.retained_bytes,
    })
}

fn validate_document_keys(document_keys: &[String], root_document_key: &str) -> NativeResult<()> {
    if document_keys.is_empty()
        || root_document_key.is_empty()
        || !root_document_key.is_ascii()
        || !document_keys.iter().any(|key| key == root_document_key)
    {
        return Err(NativeError::protocol(
            "native closure document-key metadata is invalid",
        ));
    }
    let mut previous: Option<&[u8]> = None;
    for key in document_keys {
        if key.is_empty()
            || !key.is_ascii()
            || previous.is_some_and(|selected| selected >= key.as_bytes())
        {
            return Err(NativeError::protocol(
                "native closure document keys are not ASCII ascending unique",
            ));
        }
        previous = Some(key.as_bytes());
    }
    Ok(())
}

fn closure_structural_counts(storage: &TypedFacadeStorageV2) -> NativeResult<[u64; 3]> {
    let mut counts = [0_u64; 3];
    for (target, collection) in counts.iter_mut().zip(structural_collections()) {
        *target =
            storage.canonical_root_count(collection, TypedFacadeScopeV2::Closure, None, false)?;
    }
    Ok(counts)
}

fn account_canonical_row(
    row: &[u8],
    rows: &mut u64,
    bytes: &mut u64,
    maximum: &mut u64,
) -> NativeResult<()> {
    *rows = checked_add(*rows, 1, "native closure canonical row count overflow")?;
    let size = u64::try_from(row.len())
        .map_err(|_| NativeError::limit("native closure canonical row exceeds u64"))?;
    *bytes = checked_add(*bytes, size, "native closure canonical byte count overflow")?;
    *maximum = (*maximum).max(size);
    Ok(())
}

fn prepare_logical_fingerprint(
    storage: &TypedFacadeStorageV2,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    budget: &ClosureEvidenceBudget<'_>,
) -> NativeResult<(FingerprintEvidenceV2, u64, u64, u64)> {
    let axiom_count = storage.canonical_root_count(
        TypedFacadeCollectionV2::Axioms,
        TypedFacadeScopeV2::Closure,
        None,
        false,
    )?;
    let extension_count = storage.canonical_root_count(
        TypedFacadeCollectionV2::Extensions,
        TypedFacadeScopeV2::Closure,
        None,
        false,
    )?;
    let canonical_row_temporary_bytes = usize::try_from(storage.maximum_row_bytes())
        .map_err(|_| NativeError::limit("native logical canonical row temporary exceeds usize"))?;
    let predicted_axiom_outer = usize::try_from(axiom_count)
        .map_err(|_| NativeError::limit("native logical axiom count exceeds usize"))?
        .checked_mul(size_of::<Vec<u8>>())
        .ok_or_else(|| NativeError::limit("native logical axiom size overflow"))?;
    let predicted_extension_outer = usize::try_from(extension_count)
        .map_err(|_| NativeError::limit("native logical extension count exceeds usize"))?
        .checked_mul(size_of::<Vec<u8>>())
        .ok_or_else(|| NativeError::limit("native logical extension size overflow"))?;
    budget.ensure_temporary(
        predicted_axiom_outer
            .checked_add(predicted_extension_outer)
            .and_then(|value| value.checked_add(canonical_row_temporary_bytes))
            .ok_or_else(|| NativeError::limit("native logical workspace overflow"))?,
    )?;
    let mut axioms = Vec::new();
    cancellation.checkpoint()?;
    axioms
        .try_reserve_exact(
            usize::try_from(axiom_count)
                .map_err(|_| NativeError::limit("native logical axiom count exceeds usize"))?,
        )
        .map_err(|_| NativeError::limit("native logical axiom allocation failed"))?;
    let mut extensions = Vec::new();
    let axiom_outer_bytes = axioms
        .capacity()
        .checked_mul(size_of::<Vec<u8>>())
        .ok_or_else(|| NativeError::limit("native logical axiom size overflow"))?;
    budget.ensure_temporary(
        axiom_outer_bytes
            .checked_add(predicted_extension_outer)
            .and_then(|value| value.checked_add(canonical_row_temporary_bytes))
            .ok_or_else(|| NativeError::limit("native logical workspace overflow"))?,
    )?;
    cancellation.checkpoint()?;
    extensions
        .try_reserve_exact(
            usize::try_from(extension_count)
                .map_err(|_| NativeError::limit("native logical extension count exceeds usize"))?,
        )
        .map_err(|_| NativeError::limit("native logical extension allocation failed"))?;
    let extension_outer_bytes = extensions
        .capacity()
        .checked_mul(size_of::<Vec<u8>>())
        .ok_or_else(|| NativeError::limit("native logical extension size overflow"))?;
    let outer_bytes = axiom_outer_bytes
        .checked_add(extension_outer_bytes)
        .ok_or_else(|| NativeError::limit("native logical workspace overflow"))?;
    budget.ensure_temporary(
        outer_bytes
            .checked_add(canonical_row_temporary_bytes)
            .ok_or_else(|| NativeError::limit("native logical workspace overflow"))?,
    )?;
    let mut rows = 0_u64;
    let mut bytes = 0_u64;
    let mut payload_bytes = 0_usize;
    storage.visit_canonical_roots(
        TypedFacadeCollectionV2::Axioms,
        TypedFacadeScopeV2::Closure,
        None,
        false,
        cancellation.clone(),
        interrupt.clone(),
        |row| {
            rows = checked_add(rows, 1, "native logical row count overflow")?;
            bytes = checked_add(
                bytes,
                u64::try_from(row.len())
                    .map_err(|_| NativeError::limit("native logical row exceeds u64"))?,
                "native logical row byte count overflow",
            )?;
            if is_logical_axiom(row_tag(row)?) {
                budget.ensure_temporary(
                    outer_bytes
                        .checked_add(payload_bytes)
                        .and_then(|value| value.checked_add(canonical_row_temporary_bytes))
                        .and_then(|value| value.checked_add(row.len()))
                        .ok_or_else(|| NativeError::limit("native logical workspace overflow"))?,
                )?;
                let logical = without_annotations(row)?;
                budget.ensure_temporary(
                    outer_bytes
                        .checked_add(payload_bytes)
                        .and_then(|value| value.checked_add(canonical_row_temporary_bytes))
                        .and_then(|value| value.checked_add(logical.capacity()))
                        .ok_or_else(|| NativeError::limit("native logical workspace overflow"))?,
                )?;
                payload_bytes = payload_bytes
                    .checked_add(logical.capacity())
                    .ok_or_else(|| NativeError::limit("native logical workspace overflow"))?;
                axioms.push(logical);
            }
            Ok(())
        },
    )?;
    storage.visit_canonical_roots(
        TypedFacadeCollectionV2::Extensions,
        TypedFacadeScopeV2::Closure,
        None,
        false,
        cancellation.clone(),
        interrupt.clone(),
        |row| {
            rows = checked_add(rows, 1, "native logical row count overflow")?;
            bytes = checked_add(
                bytes,
                u64::try_from(row.len())
                    .map_err(|_| NativeError::limit("native logical row exceeds u64"))?,
                "native logical row byte count overflow",
            )?;
            budget.ensure_temporary(
                outer_bytes
                    .checked_add(payload_bytes)
                    .and_then(|value| value.checked_add(canonical_row_temporary_bytes))
                    .and_then(|value| value.checked_add(row.len()))
                    .ok_or_else(|| NativeError::limit("native logical workspace overflow"))?,
            )?;
            let logical = without_annotations(row)?;
            budget.ensure_temporary(
                outer_bytes
                    .checked_add(payload_bytes)
                    .and_then(|value| value.checked_add(canonical_row_temporary_bytes))
                    .and_then(|value| value.checked_add(logical.capacity()))
                    .ok_or_else(|| NativeError::limit("native logical workspace overflow"))?,
            )?;
            payload_bytes = payload_bytes
                .checked_add(logical.capacity())
                .ok_or_else(|| NativeError::limit("native logical workspace overflow"))?;
            extensions.push(logical);
            Ok(())
        },
    )?;
    let workspace = logical_workspace_bytes(
        &axioms,
        axioms.capacity(),
        &extensions,
        extensions.capacity(),
    )?;
    let accounted_workspace = outer_bytes
        .checked_add(payload_bytes)
        .ok_or_else(|| NativeError::limit("native logical workspace overflow"))?;
    if usize::try_from(workspace).ok() != Some(accounted_workspace) {
        return Err(NativeError::protocol(
            "native logical workspace accounting diverges",
        ));
    }
    budget.ensure_temporary(accounted_workspace)?;
    cancellation.checkpoint()?;
    axioms.sort_unstable();
    cancellation.checkpoint()?;
    axioms.dedup();
    cancellation.checkpoint()?;
    extensions.sort_unstable();
    cancellation.checkpoint()?;
    extensions.dedup();
    let mut logical = MeasuredSha256::new();
    logical.update(LOGICAL_FINGERPRINT_DOMAIN_V2)?;
    logical.update(LOGICAL_POLICY_V1)?;
    logical.varint(
        u64::try_from(axioms.len())
            .map_err(|_| NativeError::limit("native logical axiom count exceeds u64"))?,
    )?;
    for row in &axioms {
        cancellation.checkpoint()?;
        logical.frame_varint(row)?;
    }
    logical.varint(
        u64::try_from(extensions.len())
            .map_err(|_| NativeError::limit("native logical extension count exceeds u64"))?,
    )?;
    for row in &extensions {
        cancellation.checkpoint()?;
        logical.update(b"E")?;
        logical.frame_varint(row)?;
    }
    Ok((logical.finish(), workspace, rows, bytes))
}

fn prepare_signature_fingerprint(
    storage: &TypedFacadeStorageV2,
    cancellation: Cancellation,
    interrupt: Option<InterruptSlot>,
    budget: &ClosureEvidenceBudget<'_>,
) -> NativeResult<(FingerprintEvidenceV2, u64, u64, u64)> {
    let count = storage.canonical_root_count(
        TypedFacadeCollectionV2::Signature,
        TypedFacadeScopeV2::Closure,
        None,
        false,
    )?;
    let canonical_row_temporary_bytes =
        usize::try_from(storage.maximum_row_bytes()).map_err(|_| {
            NativeError::limit("native signature canonical row temporary exceeds usize")
        })?;
    budget.ensure_temporary(canonical_row_temporary_bytes)?;
    let mut signature = MeasuredSha256::new();
    signature.update(SIGNATURE_FINGERPRINT_DOMAIN_V2)?;
    signature.update(&[1])?;
    signature.varint(count)?;
    let mut emitted = 0_u64;
    let mut bytes = 0_u64;
    let mut maximum = 1_u64;
    storage.visit_canonical_roots(
        TypedFacadeCollectionV2::Signature,
        TypedFacadeScopeV2::Closure,
        None,
        false,
        cancellation,
        interrupt,
        |row| {
            emitted = checked_add(emitted, 1, "native signature row count overflow")?;
            let size = u64::try_from(row.len())
                .map_err(|_| NativeError::limit("native signature row exceeds u64"))?;
            bytes = checked_add(bytes, size, "native signature byte count overflow")?;
            maximum = maximum.max(size);
            signature.frame_varint(row)
        },
    )?;
    if emitted != count {
        return Err(NativeError::protocol(
            "native signature traversal diverges from its count",
        ));
    }
    Ok((signature.finish(), emitted, bytes, maximum))
}

fn logical_workspace_bytes(
    axioms: &[Vec<u8>],
    axiom_capacity: usize,
    extensions: &[Vec<u8>],
    extension_capacity: usize,
) -> NativeResult<u64> {
    let outer = axiom_capacity
        .checked_add(extension_capacity)
        .and_then(|count| count.checked_mul(size_of::<Vec<u8>>()))
        .ok_or_else(|| NativeError::limit("native logical workspace size overflow"))?;
    axioms.iter().chain(extensions).try_fold(
        u64::try_from(outer)
            .map_err(|_| NativeError::limit("native logical workspace exceeds u64"))?,
        |total, row| {
            total
                .checked_add(
                    u64::try_from(row.capacity())
                        .map_err(|_| NativeError::limit("native logical row exceeds u64"))?,
                )
                .ok_or_else(|| NativeError::limit("native logical workspace overflow"))
        },
    )
}

fn merge_closure_origins(
    documents: &[PreparedClosureDocumentV2],
    budget: &ClosureEvidenceBudget<'_>,
    cancellation: &Cancellation,
) -> NativeResult<Vec<Vec<u8>>> {
    let total = documents.iter().try_fold(0_usize, |sum, document| {
        sum.checked_add(document.origin_rows.as_ref().map_or(0, Vec::len))
            .ok_or_else(|| NativeError::limit("native closure origin count overflow"))
    })?;
    let mut keyed = Vec::new();
    let predicted_keyed_bytes = total
        .checked_mul(size_of::<([u8; 32], usize, u64, &[u8])>())
        .ok_or_else(|| NativeError::limit("native closure origin key size overflow"))?;
    budget.ensure_temporary(predicted_keyed_bytes)?;
    cancellation.checkpoint()?;
    keyed
        .try_reserve_exact(total)
        .map_err(|_| NativeError::limit("native closure origin allocation failed"))?;
    let keyed_bytes = keyed
        .capacity()
        .checked_mul(size_of::<([u8; 32], usize, u64, &[u8])>())
        .ok_or_else(|| NativeError::limit("native closure origin key size overflow"))?;
    budget.ensure_temporary(keyed_bytes)?;
    for (document_ordinal, document) in documents.iter().enumerate() {
        for row in document.origin_rows.as_deref().unwrap_or_default() {
            cancellation.checkpoint()?;
            let (digest, occurrence) = validate_origin_row(row, document.document_key.as_ref())?;
            keyed.push((digest, document_ordinal, occurrence, row.as_slice()));
        }
    }
    cancellation.checkpoint()?;
    keyed.sort_unstable_by(|left, right| {
        left.0
            .cmp(&right.0)
            .then_with(|| left.1.cmp(&right.1))
            .then_with(|| left.2.cmp(&right.2))
            .then_with(|| left.3.cmp(right.3))
    });
    if keyed.windows(2).any(|pair| pair[0].3 == pair[1].3) {
        return Err(NativeError::protocol(
            "native closure effective origins are not unique",
        ));
    }
    let mut rows = Vec::new();
    let predicted_row_table_bytes = keyed
        .len()
        .checked_mul(size_of::<Vec<u8>>())
        .ok_or_else(|| NativeError::limit("native closure origin row size overflow"))?;
    budget.ensure_temporary(
        keyed_bytes
            .checked_add(predicted_row_table_bytes)
            .ok_or_else(|| NativeError::limit("native closure origin workspace overflow"))?,
    )?;
    cancellation.checkpoint()?;
    rows.try_reserve_exact(keyed.len())
        .map_err(|_| NativeError::limit("native closure origin row allocation failed"))?;
    let row_table_bytes = rows
        .capacity()
        .checked_mul(size_of::<Vec<u8>>())
        .ok_or_else(|| NativeError::limit("native closure origin row size overflow"))?;
    budget.ensure_temporary(
        keyed_bytes
            .checked_add(row_table_bytes)
            .ok_or_else(|| NativeError::limit("native closure origin workspace overflow"))?,
    )?;
    let mut payload_bytes = 0_usize;
    for (_digest, _document, _occurrence, row) in keyed {
        cancellation.checkpoint()?;
        budget.ensure_temporary(
            keyed_bytes
                .checked_add(row_table_bytes)
                .and_then(|value| value.checked_add(payload_bytes))
                .and_then(|value| value.checked_add(row.len()))
                .ok_or_else(|| NativeError::limit("native closure origin workspace overflow"))?,
        )?;
        let mut copied = Vec::new();
        copied
            .try_reserve_exact(row.len())
            .map_err(|_| NativeError::limit("native closure origin payload allocation failed"))?;
        budget.ensure_temporary(
            keyed_bytes
                .checked_add(row_table_bytes)
                .and_then(|value| value.checked_add(payload_bytes))
                .and_then(|value| value.checked_add(copied.capacity()))
                .ok_or_else(|| NativeError::limit("native closure origin workspace overflow"))?,
        )?;
        copied.extend_from_slice(row);
        payload_bytes = payload_bytes
            .checked_add(copied.capacity())
            .ok_or_else(|| NativeError::limit("native closure origin payload size overflow"))?;
        rows.push(copied);
    }
    Ok(rows)
}

fn validate_origin_row(row: &[u8], document_key: &str) -> NativeResult<([u8; 32], u64)> {
    if row.len() < 45 {
        return Err(NativeError::protocol(
            "native closure origin row is truncated",
        ));
    }
    let mut digest = [0_u8; 32];
    digest.copy_from_slice(&row[..32]);
    let key_length = u32::from_le_bytes(
        row[32..36]
            .try_into()
            .map_err(|_| NativeError::protocol("native origin key length is truncated"))?,
    ) as usize;
    let key_end = 36_usize
        .checked_add(key_length)
        .ok_or_else(|| NativeError::limit("native origin key length overflow"))?;
    if row.get(36..key_end) != Some(document_key.as_bytes()) {
        return Err(NativeError::protocol(
            "native closure origin belongs to the wrong document",
        ));
    }
    let occurrence_end = key_end
        .checked_add(8)
        .ok_or_else(|| NativeError::limit("native origin occurrence offset overflow"))?;
    let occurrence = u64::from_le_bytes(
        row.get(key_end..occurrence_end)
            .ok_or_else(|| NativeError::protocol("native origin occurrence is truncated"))?
            .try_into()
            .map_err(|_| NativeError::protocol("native origin occurrence is invalid"))?,
    );
    Ok((digest, occurrence))
}

fn retained_origin_bytes(
    documents: &[PreparedClosureDocumentV2],
    closure: Option<&[Vec<u8>]>,
) -> NativeResult<u64> {
    documents
        .iter()
        .flat_map(|document| {
            document
                .raw_origin_rows
                .iter()
                .flat_map(|rows| rows.iter())
                .chain(document.origin_rows.iter().flat_map(|rows| rows.iter()))
        })
        .chain(closure.into_iter().flatten())
        .try_fold(0_u64, |total, row| {
            total
                .checked_add(
                    u64::try_from(row.len())
                        .map_err(|_| NativeError::limit("native origin row exceeds u64"))?,
                )
                .ok_or_else(|| NativeError::limit("native origin byte count overflow"))
        })
}

fn max_source_row(documents: &[PreparedClosureDocumentV2]) -> u64 {
    documents
        .iter()
        .flat_map(|document| document.source_map.iter())
        .flat_map(|source| source.entries.iter().chain(&source.prefixes))
        .map(|row| row.len() as u64)
        .max()
        .unwrap_or(1)
}

fn max_origin_row(documents: &[PreparedClosureDocumentV2], closure: Option<&[Vec<u8>]>) -> u64 {
    documents
        .iter()
        .flat_map(|document| {
            document
                .raw_origin_rows
                .iter()
                .flat_map(|rows| rows.iter())
                .chain(document.origin_rows.iter().flat_map(|rows| rows.iter()))
        })
        .chain(closure.into_iter().flatten())
        .map(|row| row.len() as u64)
        .max()
        .unwrap_or(1)
}

fn max_rdf_row(documents: &[PreparedClosureDocumentV2]) -> u64 {
    documents
        .iter()
        .flat_map(|document| document.rdf_report.iter())
        .flat_map(|report| {
            std::iter::once(&report.rows.header)
                .chain(&report.rows.unconsumed_triples)
                .chain(&report.rows.rule_ids)
                .chain(&report.rows.diagnostics)
        })
        .map(|row| row.len() as u64)
        .max()
        .unwrap_or(1)
}

fn fingerprint_inputs_digest(
    documents: &[PreparedClosureDocumentV2],
    root_document_key: &str,
    structural: FingerprintEvidenceV2,
    logical: FingerprintEvidenceV2,
    signature: FingerprintEvidenceV2,
) -> NativeResult<[u8; 32]> {
    let mut manifest = MeasuredSha256::domain(FINGERPRINT_INPUTS_MANIFEST_DOMAIN_V2)?;
    manifest.u32_le(2)?;
    manifest.text64(root_document_key)?;
    manifest.u64_le(
        u64::try_from(documents.len())
            .map_err(|_| NativeError::limit("native closure document count exceeds u64"))?,
    )?;
    for document in documents {
        manifest.update(&[1])?;
        manifest.text64(document.document_key.as_ref())?;
        append_fingerprint_evidence(&mut manifest, document.document_fingerprint)?;
    }
    for (tag, evidence) in [(2_u8, structural), (3, logical), (4, signature)] {
        manifest.update(&[tag])?;
        append_fingerprint_evidence(&mut manifest, evidence)?;
    }
    Ok(manifest.finish().digest)
}

fn append_fingerprint_evidence(
    manifest: &mut MeasuredSha256,
    evidence: FingerprintEvidenceV2,
) -> NativeResult<()> {
    manifest.u64_le(evidence.preimage_bytes)?;
    manifest.u32_le(2)?;
    manifest.update(&evidence.digest)
}

fn source_manifest_digest(
    documents: &[PreparedClosureDocumentV2],
    present: bool,
) -> NativeResult<[u8; 32]> {
    let mut manifest = MeasuredSha256::domain(SOURCE_MANIFEST_DOMAIN_V2)?;
    manifest.update(&AUXILIARY_CODEC_SCHEMA_SHA256_V2)?;
    manifest.u64_le(
        u64::try_from(documents.len())
            .map_err(|_| NativeError::limit("native closure document count exceeds u64"))?,
    )?;
    for document in documents {
        manifest.text64(document.document_key.as_ref())?;
        match (&document.source_map, present) {
            (None, false) => manifest.update(&[0])?,
            (Some(source), true) => {
                let entries = u64::try_from(source.entries.len())
                    .map_err(|_| NativeError::limit("native source-map count exceeds u64"))?;
                let prefixes = u64::try_from(source.prefixes.len())
                    .map_err(|_| NativeError::limit("native source-prefix count exceeds u64"))?;
                let mut table = MeasuredSha256::domain(DOCUMENT_SOURCE_TABLE_DOMAIN_V2)?;
                table.text64(document.document_key.as_ref())?;
                table.u64_le(entries)?;
                for row in &source.entries {
                    table.frame64(row)?;
                }
                table.u64_le(prefixes)?;
                for row in &source.prefixes {
                    table.frame64(row)?;
                }
                manifest.update(&[1])?;
                manifest.u64_le(entries)?;
                manifest.u64_le(prefixes)?;
                manifest.update(&table.finish().digest)?;
            }
            _ => {
                return Err(NativeError::protocol(
                    "native closure source capability diverges from retained rows",
                ));
            }
        }
    }
    Ok(manifest.finish().digest)
}

fn provenance_manifest_digest(
    documents: &[PreparedClosureDocumentV2],
    origin_present: bool,
) -> NativeResult<[u8; 32]> {
    let mut manifest = MeasuredSha256::domain(PROVENANCE_MANIFEST_DOMAIN_V2)?;
    manifest.update(&AUXILIARY_CODEC_SCHEMA_SHA256_V2)?;
    manifest.u64_le(
        u64::try_from(documents.len())
            .map_err(|_| NativeError::limit("native closure document count exceeds u64"))?,
    )?;
    for document in documents {
        manifest.text64(document.document_key.as_ref())?;
        match (&document.raw_origin_rows, origin_present) {
            (None, false) => manifest.update(&[0])?,
            (Some(origins), true) => {
                let count = u64::try_from(origins.len())
                    .map_err(|_| NativeError::limit("native raw origin count exceeds u64"))?;
                let mut table = MeasuredSha256::domain(DOCUMENT_ORIGIN_TABLE_DOMAIN_V2)?;
                table.text64(document.document_key.as_ref())?;
                table.u64_le(count)?;
                for row in origins {
                    table.frame64(row)?;
                }
                manifest.update(&[1])?;
                manifest.u64_le(count)?;
                manifest.update(&table.finish().digest)?;
            }
            _ => {
                return Err(NativeError::protocol(
                    "native closure origin capability diverges from retained rows",
                ));
            }
        }
        match &document.rdf_report {
            None => manifest.update(&[0])?,
            Some(report) => {
                manifest.update(&[1])?;
                manifest.u64_le(u64::try_from(report.rows.unconsumed_triples.len()).map_err(
                    |_| NativeError::limit("native RDF unconsumed count exceeds u64"),
                )?)?;
                manifest.u64_le(
                    u64::try_from(report.rows.rule_ids.len())
                        .map_err(|_| NativeError::limit("native RDF rule count exceeds u64"))?,
                )?;
                manifest.u64_le(u64::try_from(report.rows.diagnostics.len()).map_err(|_| {
                    NativeError::limit("native RDF diagnostic count exceeds u64")
                })?)?;
                manifest.update(&report.digest)?;
            }
        }
    }
    Ok(manifest.finish().digest)
}

fn effective_origin_manifest_digest(
    documents: &[PreparedClosureDocumentV2],
    closure_origins: &[Vec<u8>],
) -> NativeResult<[u8; 32]> {
    let mut manifest = MeasuredSha256::domain(EFFECTIVE_ORIGIN_MANIFEST_DOMAIN_V2)?;
    manifest.update(&AUXILIARY_CODEC_SCHEMA_SHA256_V2)?;
    manifest.u64_le(
        u64::try_from(documents.len())
            .map_err(|_| NativeError::limit("native closure document count exceeds u64"))?,
    )?;
    for document in documents {
        let origins = document.origin_rows.as_deref().unwrap_or_default();
        let count = u64::try_from(origins.len())
            .map_err(|_| NativeError::limit("native effective origin count exceeds u64"))?;
        let mut table = MeasuredSha256::domain(EFFECTIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2)?;
        table.text64(document.document_key.as_ref())?;
        table.u64_le(count)?;
        for row in origins {
            table.frame64(row)?;
        }
        manifest.text64(document.document_key.as_ref())?;
        manifest.u64_le(count)?;
        manifest.update(&table.finish().digest)?;
    }
    let closure_count = u64::try_from(closure_origins.len())
        .map_err(|_| NativeError::limit("native closure origin count exceeds u64"))?;
    let mut closure = MeasuredSha256::domain(EFFECTIVE_CLOSURE_ORIGIN_TABLE_DOMAIN_V2)?;
    closure.u64_le(closure_count)?;
    for row in closure_origins {
        closure.frame64(row)?;
    }
    manifest.u64_le(closure_count)?;
    manifest.update(&closure.finish().digest)?;
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
    result
        .try_reserve_exact(
            last.checked_add(2)
                .ok_or_else(|| NativeError::limit("logical row size overflow"))?,
        )
        .map_err(|_| NativeError::limit("logical row allocation failed"))?;
    result.extend_from_slice(&row[..last]);
    result.extend_from_slice(&[6, 0]);
    Ok(result)
}

fn skip_component(data: &[u8], offset: usize) -> NativeResult<usize> {
    let marker = *data
        .get(offset)
        .ok_or_else(|| NativeError::protocol("canonical component is truncated"))?;
    let following = offset
        .checked_add(1)
        .ok_or_else(|| NativeError::limit("canonical component offset overflow"))?;
    match marker {
        0 => Ok(following),
        1 | 2 | 3 | 5 => {
            let (length, start) = read_varint(data, following)?;
            start
                .checked_add(
                    usize::try_from(length)
                        .map_err(|_| NativeError::limit("canonical frame exceeds usize"))?,
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
    let mut index = 0_usize;
    loop {
        let mut byte = (value & 0x7f) as u8;
        value >>= 7;
        if value != 0 {
            byte |= 0x80;
        }
        output[index] = byte;
        index += 1;
        if value == 0 {
            return (output, index);
        }
    }
}

fn append(output: &mut Vec<u8>, value: &[u8]) -> NativeResult<()> {
    output
        .try_reserve(value.len())
        .map_err(|_| NativeError::limit("native closure summary allocation failed"))?;
    output.extend_from_slice(value);
    Ok(())
}

fn append_u64(output: &mut Vec<u8>, value: u64) -> NativeResult<()> {
    append(output, &value.to_le_bytes())
}

fn checked_add(left: u64, right: u64, message: &'static str) -> NativeResult<u64> {
    left.checked_add(right)
        .ok_or_else(|| NativeError::limit(message))
}
