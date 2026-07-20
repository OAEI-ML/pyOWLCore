//! Compact owner-first publication preparation for retained Functional loads.
//!
//! The optimized path never exports canonical ontology rows or fingerprint
//! preimages to Python.  It returns bounded metadata, then streams canonical
//! temporaries from the retained component arena into native digest state.

use std::mem::size_of;

use crate::cancel::{Cancellation, InterruptSlot};
use crate::canonical::Node;
use crate::error::{NativeError, NativeResult};
use crate::hash::Sha256;
use crate::limits::{LimitKey, Limits};
use crate::model::{
    canonical_contains_tag, canonical_field_count, structural_digest_v1, ScanBudget,
};
use crate::publication::{
    TypedFacadeCollectionV2, TypedFacadeScopeV2, TypedFacadeStorageV2,
    AUXILIARY_CODEC_SCHEMA_SHA256_V2,
};

use super::{ParsedDocument, Span, SpannedNode};

pub(crate) const RETAINED_SEED_MAGIC_V2: &[u8; 8] = b"PYNFRS2\0";
pub(crate) const RETAINED_PREPARED_MAGIC_V2: &[u8; 8] = b"PYNFPP2\0";
const RETAINED_SEED_SCHEMA_V2: u16 = 1;
const RETAINED_PREPARED_SCHEMA_V2: u16 = 2;

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
const PROVENANCE_MANIFEST_DOMAIN_V2: &[u8] = b"pyowl-core:native-provenance-manifest:v2";
const DOCUMENT_ORIGIN_TABLE_DOMAIN_V2: &[u8] = b"pyowl-core:native-document-origin-table:v2";
const EFFECTIVE_ORIGIN_MANIFEST_DOMAIN_V2: &[u8] =
    b"pyowl-core:native-effective-origin-manifest:v2";
const EFFECTIVE_DOCUMENT_ORIGIN_TABLE_DOMAIN_V2: &[u8] =
    b"pyowl-core:native-effective-document-origin-table:v2";
const EFFECTIVE_CLOSURE_ORIGIN_TABLE_DOMAIN_V2: &[u8] =
    b"pyowl-core:native-effective-closure-origin-table:v2";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct FingerprintEvidenceV2 {
    pub(crate) preimage_bytes: u64,
    pub(crate) digest: [u8; 32],
}

#[derive(Clone, Copy, Debug)]
pub(crate) struct RetainedOccurrenceV2 {
    digest: [u8; 32],
    span: Span,
    source_order: u64,
}

#[derive(Debug)]
pub(crate) struct RetainedParseMetadataV2 {
    pub(crate) document_fingerprint: FingerprintEvidenceV2,
    pub(crate) occurrence_count: u64,
    pub(crate) root_counts: [u64; 3],
    occurrences: Vec<RetainedOccurrenceV2>,
}

type RetainedSeedV2 = (Vec<u8>, RetainedParseMetadataV2, [Vec<Vec<u8>>; 3]);

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
    pub(crate) origin_rows: Option<Vec<Vec<u8>>>,
    pub(crate) max_facade_row_bytes: u64,
    pub(crate) canonical_rows_encoded: u64,
    pub(crate) canonical_bytes_encoded: u64,
    pub(crate) fingerprint_temporary_bytes: u64,
    pub(crate) origin_bytes_retained: u64,
    pub(crate) document_key: Box<str>,
}

impl PreparedRetainedPublicationV2 {
    pub(crate) fn encode_summary(&self, prepare_ns: u64) -> NativeResult<Vec<u8>> {
        let mut output = Vec::new();
        output
            .try_reserve_exact(640)
            .map_err(|_| NativeError::limit("native retained summary allocation failed"))?;
        append(&mut output, RETAINED_PREPARED_MAGIC_V2)?;
        append(&mut output, &RETAINED_PREPARED_SCHEMA_V2.to_le_bytes())?;
        append(&mut output, &0_u16.to_le_bytes())?;
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
        append_u64(&mut output, origin_rows)?;
        append_u64(&mut output, self.max_facade_row_bytes)?;
        append_u64(&mut output, self.canonical_rows_encoded)?;
        append_u64(&mut output, self.canonical_bytes_encoded)?;
        append_u64(&mut output, self.fingerprint_temporary_bytes)?;
        append_u64(&mut output, self.origin_bytes_retained)?;
        append_u64(&mut output, prepare_ns)?;
        Ok(output)
    }
}

impl RetainedParseMetadataV2 {
    pub(crate) fn retained_bytes(&self) -> NativeResult<usize> {
        self.occurrences
            .capacity()
            .checked_mul(size_of::<RetainedOccurrenceV2>())
            .ok_or_else(|| NativeError::limit("native retained parser metadata overflow"))
    }
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

pub(crate) fn build_seed(
    parsed: ParsedDocument,
    collect_provenance: bool,
) -> NativeResult<RetainedSeedV2> {
    let occurrence_count = total_occurrences(&parsed)?;
    let occurrences = retained_occurrences(&parsed, occurrence_count, collect_provenance)?;
    let ParsedDocument {
        ontology_iri,
        version_iri,
        mut imports,
        annotations,
        axioms,
        extensions,
        prefixes: _,
        decoded_codepoints,
    } = parsed;
    let raw_import_count = imports.len();
    imports.sort_unstable_by(|left, right| left.as_bytes().cmp(right.as_bytes()));
    imports.dedup_by(|left, right| left.as_bytes() == right.as_bytes());
    let rows = [
        canonical_root_rows(annotations),
        canonical_root_rows(axioms),
        canonical_root_rows(extensions),
    ];
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
        },
        rows,
    ))
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn prepare_publication(
    storage: &TypedFacadeStorageV2,
    metadata: &RetainedParseMetadataV2,
    manifest: &[u8],
    document_key: &str,
    collect_provenance: bool,
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
    if collect_provenance {
        if u64::try_from(metadata.occurrences.len()).ok() != Some(metadata.occurrence_count) {
            return Err(NativeError::protocol(
                "native retained provenance occurrences are incomplete",
            ));
        }
        if metadata.occurrence_count > limits.max_origin_entries {
            return Err(NativeError::limit(
                "native retained publication exceeds max_origin_entries",
            ));
        }
    } else if !metadata.occurrences.is_empty() {
        return Err(NativeError::protocol(
            "native retained provenance was prepared while disabled",
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
                let row_bytes = u64::try_from(row.len())
                    .map_err(|_| NativeError::limit("native retained canonical row exceeds u64"))?;
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
                if collection == TypedFacadeCollectionV2::Axioms && is_logical_axiom(row_tag(row)?)
                {
                    logical_axioms.push(without_annotations(row)?);
                } else if collection == TypedFacadeCollectionV2::Extensions {
                    logical_extensions.push(without_annotations(row)?);
                }
                Ok(())
            },
        )?;
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

    let (origin_rows, origin_bytes_retained) = if collect_provenance {
        let rows = encode_origin_rows(metadata, document_key, limits, &cancellation)?;
        let bytes = rows
            .iter()
            .try_fold(0_u64, |total, row| {
                total.checked_add(u64::try_from(row.len()).ok()?)
            })
            .ok_or_else(|| NativeError::limit("native retained origin byte count overflow"))?;
        (Some(rows), bytes)
    } else {
        (None, 0)
    };
    let selected_origins = origin_rows.as_deref().unwrap_or_default();
    let source_manifest_sha256 = source_manifest_digest(document_key)?;
    let provenance_manifest_sha256 =
        provenance_manifest_digest(document_key, origin_rows.is_some(), selected_origins)?;
    let effective_origin_manifest_sha256 =
        effective_origin_manifest_digest(document_key, selected_origins)?;
    let fingerprint_inputs_sha256 = fingerprint_inputs_digest(
        document_key,
        metadata.document_fingerprint,
        structural_fingerprint,
        logical_fingerprint,
        signature_fingerprint,
    )?;
    let origin_max = selected_origins.iter().try_fold(1_u64, |maximum, row| {
        Ok::<u64, NativeError>(
            maximum.max(
                u64::try_from(row.len())
                    .map_err(|_| NativeError::limit("native retained origin row exceeds u64"))?,
            ),
        )
    })?;
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
        origin_rows,
        max_facade_row_bytes: storage.maximum_row_bytes().max(origin_max),
        canonical_rows_encoded,
        canonical_bytes_encoded,
        fingerprint_temporary_bytes,
        origin_bytes_retained,
        document_key: document_key.into(),
    })
}

fn document_fingerprint(
    ontology_iri: &Option<Node>,
    version_iri: &Option<Node>,
    imports: &[Node],
    rows: &[Vec<Vec<u8>>; 3],
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

fn retained_occurrences(
    parsed: &ParsedDocument,
    count: u64,
    collect: bool,
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
            span: value.span,
            source_order: u64::try_from(source_order)
                .map_err(|_| NativeError::limit("native occurrence ordinal exceeds u64"))?,
        });
    }
    result.sort_unstable_by_key(|value| {
        (
            value.span.byte_start,
            value.span.byte_end,
            value.source_order,
        )
    });
    Ok(result)
}

fn total_occurrences(parsed: &ParsedDocument) -> NativeResult<u64> {
    [&parsed.annotations, &parsed.axioms, &parsed.extensions]
        .into_iter()
        .try_fold(0_u64, |total, values| {
            total.checked_add(u64::try_from(values.len()).ok()?)
        })
        .ok_or_else(|| NativeError::limit("native occurrence count overflow"))
}

fn canonical_root_rows(values: Vec<SpannedNode>) -> Vec<Vec<u8>> {
    let mut rows: Vec<Vec<u8>> = values
        .into_iter()
        .map(|value| value.node.into_bytes())
        .collect();
    rows.sort_unstable();
    rows.dedup();
    rows
}

fn encode_origin_rows(
    metadata: &RetainedParseMetadataV2,
    document_key: &str,
    limits: &Limits,
    cancellation: &Cancellation,
) -> NativeResult<Vec<Vec<u8>>> {
    let mut keyed = Vec::new();
    keyed
        .try_reserve_exact(metadata.occurrences.len())
        .map_err(|_| NativeError::limit("native origin table allocation failed"))?;
    for (occurrence, value) in metadata.occurrences.iter().enumerate() {
        cancellation.checkpoint()?;
        let occurrence = u64::try_from(occurrence)
            .map_err(|_| NativeError::limit("native origin occurrence exceeds u64"))?;
        let row = encode_origin_row(value.digest, document_key, occurrence, value.span)?;
        if u64::try_from(row.len()).map_or(true, |size| size > limits.max_wire_bytes) {
            return Err(NativeError::limit(
                "native retained origin row exceeds max_wire_bytes",
            ));
        }
        keyed.push((value.digest, occurrence, row));
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

fn encode_origin_row(
    digest: [u8; 32],
    document_key: &str,
    occurrence: u64,
    span: Span,
) -> NativeResult<Vec<u8>> {
    let key = document_key.as_bytes();
    let key_len = u32::try_from(key.len())
        .map_err(|_| NativeError::limit("native document key exceeds u32"))?;
    let size = 32_usize
        .checked_add(4)
        .and_then(|value| value.checked_add(key.len()))
        .and_then(|value| value.checked_add(8 + 1 + 4 * 8))
        .ok_or_else(|| NativeError::limit("native origin row size overflow"))?;
    let mut row = Vec::new();
    row.try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native origin row allocation failed"))?;
    row.extend_from_slice(&digest);
    row.extend_from_slice(&key_len.to_le_bytes());
    row.extend_from_slice(key);
    row.extend_from_slice(&occurrence.to_le_bytes());
    row.push(0x8f);
    for coordinate in [span.byte_start, span.byte_end, span.line, span.column] {
        row.extend_from_slice(&coordinate.to_le_bytes());
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

fn source_manifest_digest(document_key: &str) -> NativeResult<[u8; 32]> {
    let mut hasher = MeasuredSha256::domain(SOURCE_MANIFEST_DOMAIN_V2)?;
    hasher.update(&AUXILIARY_CODEC_SCHEMA_SHA256_V2)?;
    hasher.u64_le(1)?;
    hasher.text64(document_key)?;
    hasher.update(&[0])?;
    Ok(hasher.finish().digest)
}

fn provenance_manifest_digest(
    document_key: &str,
    present: bool,
    origins: &[Vec<u8>],
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
    // The guarded retained Functional path never carries an RDF mapping report.
    hasher.update(&[0])?;
    Ok(hasher.finish().digest)
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
