//! First-slice adapter from publication-independent ingestion output to the
//! frozen WP15 V1 seam.  This is test evidence, not the terminal WP16 API.

use crate::error::{NativeError, NativeResult};
use crate::hash::{sha256, Sha256};
#[cfg(feature = "test-hooks")]
use crate::limits::LimitKey;
use crate::model::{CanonicalRowId, NativeArenaBuilder};
use crate::publication::{
    ApiVersion, BackendPreferenceV1, DetectionBasisV1, DigestKindV1, DocumentFormatV1,
    DocumentMembersV1, DocumentProvenanceV1, DocumentPublicationV1, FingerprintV1,
    ImportDocumentStatusV1, ImportDocumentV1, ImportEdgeStatusV1, ImportEdgeV1, ImportManifestV1,
    ImportPolicyV1, LoadOptionsV1, LoadReportV1, NativeSnapshotPublicationV1, OntologyIdV1,
    ParseLimitsV1, PositiveIntegerV1, PublicationDraftV1,
};
use crate::session::Session;

use super::CanonicalDocument;

#[cfg(feature = "test-hooks")]
const OBSERVATION_MAGIC: &[u8; 8] = b"PYRXOBS1";
const CAPABILITIES: u64 = 1 | 2 | 4 | 32;

pub(super) fn publish(
    document: &CanonicalDocument,
    session: &mut Session<'_>,
) -> NativeResult<NativeSnapshotPublicationV1> {
    let mut arena_builder = NativeArenaBuilder::new(session.limits());
    arena_builder.intern_document_scope(document.source_sha256)?;
    for annotation in &document.ontology_annotations {
        arena_builder.intern_canonical_row(annotation)?;
    }
    for axiom in &document.axioms {
        arena_builder.intern_canonical_row(axiom)?;
    }
    for extension in &document.extensions {
        arena_builder.intern_canonical_row(extension)?;
    }
    let arena = arena_builder.freeze()?;
    let annotations = identifiers(0, document.ontology_annotations.len(), session)?;
    let axioms = identifiers(annotations.len(), document.axioms.len(), session)?;
    let extensions = identifiers(
        annotations
            .len()
            .checked_add(axioms.len())
            .ok_or_else(|| NativeError::limit("native V1 member offset overflow"))?,
        document.extensions.len(),
        session,
    )?;
    if arena.canonical_rows().len()
        != annotations
            .len()
            .checked_add(axioms.len())
            .and_then(|value| value.checked_add(extensions.len()))
            .ok_or_else(|| NativeError::limit("native V1 member count overflow"))?
    {
        return Err(NativeError::protocol(
            "native first-slice canonical rows unexpectedly deduplicated across partitions",
        ));
    }

    let document_fingerprint = document_fingerprint(document, session)?;
    let rdf_report_sha256 = mapping_digest(document);
    let provenance = DocumentProvenanceV1 {
        source_sha256: document.source_sha256,
        digest_kind: DigestKindV1::ExactBytes,
        byte_length: document.byte_length,
        decoded_codepoint_length: document.decoded_codepoints,
        document_iri: optional_boxed(document.document_iri.as_deref(), session)?,
        acquisition_locator: None,
        format: DocumentFormatV1::RdfXml,
        detection_basis: DetectionBasisV1::Explicit,
        media_type: None,
        expected_sha256: None,
        parser: boxed("pyowl_core.native.wp16.rdfxml-slice-v1", session)?,
        backend: boxed("native", session)?,
        api_version: ApiVersion(0, 1),
        model_schema: 1,
    };
    let published_document = DocumentPublicationV1 {
        document_key: document_key(document.source_sha256, session)?,
        ontology_id: ontology_id(document, session)?,
        document_iri: optional_boxed(document.document_iri.as_deref(), session)?,
        direct_imports: boxed_strings(&document.imports, session)?,
        provenance,
        document_fingerprint: fingerprint(document_fingerprint, session)?,
        diagnostics: Box::new([]),
        ontology_annotation_count: usize_u64(document.ontology_annotations.len())?,
        axiom_count: usize_u64(document.axioms.len())?,
        extension_count: usize_u64(document.extensions.len())?,
        source_map_entry_count: 0,
        origin_entry_count: 0,
        rdf_mapping_conformant: Some(true),
        rdf_mapping_report_sha256: Some(rdf_report_sha256),
    };
    let import_document = ImportDocumentV1 {
        document_key: document_key(document.source_sha256, session)?,
        ontology_id: ontology_id(document, session)?,
        document_iri: optional_boxed(document.document_iri.as_deref(), session)?,
        source_sha256: document.source_sha256,
        document_fingerprint: fingerprint(document_fingerprint, session)?,
        format: DocumentFormatV1::RdfXml,
        status: ImportDocumentStatusV1::Root,
    };
    let mut edges = reserved_vec(document.imports.len(), session)?;
    for value in &document.imports {
        edges.push(ImportEdgeV1 {
            importing_document_key: document_key(document.source_sha256, session)?,
            import_iri: boxed(value, session)?,
            status: ImportEdgeStatusV1::Ignored,
            resolved_document_key: None,
            resolver_name: None,
            sanitized_locator: None,
            diagnostic: None,
        });
    }
    let edges = edges.into_boxed_slice();
    let options = LoadOptionsV1 {
        format: Some(DocumentFormatV1::RdfXml),
        imports: ImportPolicyV1::Ignore,
        backend: BackendPreferenceV1::Native,
        limits: ParseLimitsV1::default(),
        offline: true,
        preserve_source_map: false,
        collect_provenance: false,
        validate_owl2_dl: false,
        deterministic: true,
    };
    let structural = domain_digest(
        b"pyowl-core:wp16-v1-structural-adapter:v1\0",
        &[&document_fingerprint, &document.source_sha256],
    );
    let logical = sha256(b"pyowl-core:snapshot-logical:v1\0datatype-policy:owl2-v1\0\0\0");
    let signature = signature_digest(document);
    let report = LoadReportV1 {
        backend: boxed("native", session)?,
        api_version: ApiVersion(0, 1),
        model_schema: 1,
        document_count: 1,
        total_source_bytes: document.byte_length,
        effective_axiom_count: usize_u64(document.axioms.len())?,
        resolution_attempts: 0,
        acquisition_cache_hits: 0,
        document_cache_hits: 0,
        timings: Box::new([]),
        structural_fingerprint: fingerprint(structural, session)?,
        logical_fingerprint: fingerprint(logical, session)?,
        signature_fingerprint: fingerprint(signature, session)?,
        owl2_dl_validated: false,
        owl2_dl_conforms: None,
        owl2_dl_report_sha256: None,
    };
    let documents = singleton_boxed_slice(published_document, session)?;
    let document_members = singleton_boxed_slice(
        DocumentMembersV1 {
            ontology_annotations: annotations,
            axioms,
            extensions,
        },
        session,
    )?;
    let import_documents = singleton_boxed_slice(import_document, session)?;
    PublicationDraftV1 {
        arena,
        documents,
        document_members,
        import_manifest: ImportManifestV1 {
            policy: ImportPolicyV1::Ignore,
            offline: true,
            resolver_configuration_fingerprint: sha256(b"pyowl-core:resolver:none:v1\0"),
            documents: import_documents,
            edges,
        },
        root_document_key: document_key(document.source_sha256, session)?,
        load_options: options,
        diagnostics: Box::new([]),
        report,
        capability_bits: CAPABILITIES,
        root_table_sha256: domain_digest(
            b"pyowl-core:wp16-v1-roots:v1\0",
            &[document_fingerprint.as_slice()],
        ),
        fingerprint_inputs_sha256: domain_digest(
            b"pyowl-core:wp16-v1-fingerprint-inputs:v1\0",
            &[
                document_fingerprint.as_slice(),
                rdf_report_sha256.as_slice(),
            ],
        ),
        source_manifest_sha256: domain_digest(
            b"pyowl-core:wp16-v1-sources:v1\0",
            &[document.source_sha256.as_slice()],
        ),
        provenance_manifest_sha256: domain_digest(
            b"pyowl-core:wp16-v1-provenance:v1\0",
            &[document.source_sha256.as_slice()],
        ),
    }
    .freeze()
}

#[cfg(feature = "test-hooks")]
pub(super) fn encode_observation(
    document: &CanonicalDocument,
    session: &mut Session<'_>,
) -> NativeResult<Vec<u8>> {
    let size = observation_size(document)?;
    if u64::try_from(size).map_or(true, |size| {
        size > session.limits().value(LimitKey::MaxTemporaryBytes)
    }) {
        return Err(NativeError::limit(
            "native RDF/XML test observation exceeds max_temporary_bytes",
        ));
    }
    session.reserve_bytes(size)?;
    let mut output = Vec::new();
    output
        .try_reserve_exact(size)
        .map_err(|_| NativeError::limit("native RDF/XML observation allocation failed"))?;
    output.extend_from_slice(OBSERVATION_MAGIC);
    output.extend_from_slice(&1_u16.to_le_bytes());
    output.extend_from_slice(&0_u16.to_le_bytes());
    output.extend_from_slice(&document.decoded_codepoints.to_le_bytes());
    output.extend_from_slice(&document.mapping.total_triples.to_le_bytes());
    output.extend_from_slice(&document.mapping.consumed_triples.to_le_bytes());
    encode_optional(document.ontology_iri.as_deref(), &mut output)?;
    encode_optional(document.version_iri.as_deref(), &mut output)?;
    encode_strings(&document.imports, &mut output)?;
    encode_rows(&document.axioms, &mut output)?;
    if output.len() != size {
        return Err(NativeError::protocol(
            "native RDF/XML observation size ledger diverged",
        ));
    }
    Ok(output)
}

#[cfg(feature = "test-hooks")]
fn observation_size(document: &CanonicalDocument) -> NativeResult<usize> {
    let mut size = checked_observation_add(8, 2 + 2 + 8 + 8 + 8)?;
    for value in [
        document.ontology_iri.as_deref(),
        document.version_iri.as_deref(),
    ] {
        size = checked_observation_add(size, 1)?;
        if let Some(value) = value {
            size = checked_observation_add(size, 8)?;
            size = checked_observation_add(size, value.len())?;
        }
    }
    size = checked_observation_add(size, 8)?;
    for value in &document.imports {
        size = checked_observation_add(size, 8)?;
        size = checked_observation_add(size, value.len())?;
    }
    size = checked_observation_add(size, 8)?;
    for value in &document.axioms {
        size = checked_observation_add(size, 8)?;
        size = checked_observation_add(size, value.len())?;
    }
    Ok(size)
}

#[cfg(feature = "test-hooks")]
fn checked_observation_add(left: usize, right: usize) -> NativeResult<usize> {
    left.checked_add(right)
        .ok_or_else(|| NativeError::limit("native RDF/XML observation size overflow"))
}

fn document_fingerprint(
    document: &CanonicalDocument,
    session: &mut Session<'_>,
) -> NativeResult<[u8; 32]> {
    let mut hasher = Sha256::new();
    hasher.update(b"pyowl-core:document-fingerprint:v1\0");
    for value in [
        document.ontology_iri.as_deref(),
        document.version_iri.as_deref(),
    ] {
        match value {
            Some(value) => {
                hasher.update(b"1");
                let encoded = crate::canonical::iri(owned_text(value, session)?)?;
                update_frame(&mut hasher, encoded.as_bytes())?;
            }
            None => hasher.update(b"0"),
        }
    }
    update_varint(&mut hasher, document.imports.len())?;
    for value in &document.imports {
        let encoded = crate::canonical::iri(owned_text(value, session)?)?;
        update_frame(&mut hasher, encoded.as_bytes())?;
    }
    for collection in [
        document.ontology_annotations.as_slice(),
        document.axioms.as_slice(),
        document.extensions.as_slice(),
    ] {
        update_varint(&mut hasher, collection.len())?;
        for value in collection {
            update_frame(&mut hasher, value)?;
        }
    }
    Ok(hasher.finish())
}

fn mapping_digest(document: &CanonicalDocument) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"pyowl-core:rdf-mapping-report:v1\0");
    hasher.update(&document.mapping.total_triples.to_le_bytes());
    hasher.update(&document.mapping.consumed_triples.to_le_bytes());
    for rule in document.mapping.rule_ids {
        hasher.update(rule.as_bytes());
        hasher.update(b"\0");
    }
    hasher.finish()
}

fn signature_digest(document: &CanonicalDocument) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(b"pyowl-core:wp16-v1-signature-adapter:v1\0");
    for axiom in &document.axioms {
        hasher.update(axiom);
    }
    hasher.finish()
}

fn domain_digest(domain: &[u8], values: &[&[u8]]) -> [u8; 32] {
    let mut hasher = Sha256::new();
    hasher.update(domain);
    for value in values {
        hasher.update(value);
    }
    hasher.finish()
}

fn identifiers(
    start: usize,
    count: usize,
    session: &mut Session<'_>,
) -> NativeResult<Box<[CanonicalRowId]>> {
    let end = start
        .checked_add(count)
        .ok_or_else(|| NativeError::limit("native V1 member identifier overflow"))?;
    let mut values = reserved_vec(count, session)?;
    for index in start..end {
        values.push(CanonicalRowId::try_from_index(index)?);
    }
    Ok(values.into_boxed_slice())
}

fn fingerprint(digest: [u8; 32], session: &mut Session<'_>) -> NativeResult<FingerprintV1> {
    session.reserve_bytes(1)?;
    Ok(FingerprintV1 {
        schema: PositiveIntegerV1::from_decimal("1")?,
        digest,
    })
}

fn document_key(digest: [u8; 32], session: &mut Session<'_>) -> NativeResult<Box<str>> {
    use std::fmt::Write;

    session.reserve_bytes(67)?;
    let mut value = String::new();
    value
        .try_reserve_exact(67)
        .map_err(|_| NativeError::limit("native V1 document-key allocation failed"))?;
    value.push_str("d1:");
    for byte in digest {
        write!(&mut value, "{byte:02x}")
            .map_err(|_| NativeError::protocol("native V1 document-key formatting failed"))?;
    }
    Ok(value.into_boxed_str())
}

#[cfg(feature = "test-hooks")]
fn encode_optional(value: Option<&str>, output: &mut Vec<u8>) -> NativeResult<()> {
    match value {
        Some(value) => {
            output.push(1);
            encode_frame(value.as_bytes(), output)
        }
        None => {
            output.push(0);
            Ok(())
        }
    }
}

#[cfg(feature = "test-hooks")]
fn encode_strings(values: &[String], output: &mut Vec<u8>) -> NativeResult<()> {
    output.extend_from_slice(&usize_u64(values.len())?.to_le_bytes());
    for value in values {
        encode_frame(value.as_bytes(), output)?;
    }
    Ok(())
}

#[cfg(feature = "test-hooks")]
fn encode_rows(values: &[Vec<u8>], output: &mut Vec<u8>) -> NativeResult<()> {
    output.extend_from_slice(&usize_u64(values.len())?.to_le_bytes());
    for value in values {
        encode_frame(value, output)?;
    }
    Ok(())
}

#[cfg(feature = "test-hooks")]
fn encode_frame(value: &[u8], output: &mut Vec<u8>) -> NativeResult<()> {
    output.extend_from_slice(&usize_u64(value.len())?.to_le_bytes());
    output.extend_from_slice(value);
    Ok(())
}

fn update_frame(hasher: &mut Sha256, value: &[u8]) -> NativeResult<()> {
    update_varint(hasher, value.len())?;
    hasher.update(value);
    Ok(())
}

fn update_varint(hasher: &mut Sha256, value: usize) -> NativeResult<()> {
    let mut encoded = [0_u8; 10];
    let mut value = usize_u64(value)?;
    let mut size = 0_usize;
    loop {
        let byte = (value & 0x7f) as u8;
        value >>= 7;
        encoded[size] = byte | if value == 0 { 0 } else { 0x80 };
        size += 1;
        if value == 0 {
            break;
        }
    }
    hasher.update(&encoded[..size]);
    Ok(())
}

fn usize_u64(value: usize) -> NativeResult<u64> {
    u64::try_from(value).map_err(|_| NativeError::limit("native V1 count exceeds u64"))
}

fn ontology_id(
    document: &CanonicalDocument,
    session: &mut Session<'_>,
) -> NativeResult<OntologyIdV1> {
    Ok(OntologyIdV1 {
        ontology_iri: optional_boxed(document.ontology_iri.as_deref(), session)?,
        version_iri: optional_boxed(document.version_iri.as_deref(), session)?,
    })
}

fn optional_boxed(
    value: Option<&str>,
    session: &mut Session<'_>,
) -> NativeResult<Option<Box<str>>> {
    value.map(|value| boxed(value, session)).transpose()
}

fn boxed_strings(values: &[String], session: &mut Session<'_>) -> NativeResult<Box<[Box<str>]>> {
    let mut output = reserved_vec(values.len(), session)?;
    for value in values {
        output.push(boxed(value, session)?);
    }
    Ok(output.into_boxed_slice())
}

fn singleton_boxed_slice<T>(value: T, session: &mut Session<'_>) -> NativeResult<Box<[T]>> {
    let mut values = reserved_vec(1, session)?;
    values.push(value);
    Ok(values.into_boxed_slice())
}

fn reserved_vec<T>(count: usize, session: &mut Session<'_>) -> NativeResult<Vec<T>> {
    let bytes = count
        .checked_mul(std::mem::size_of::<T>())
        .ok_or_else(|| NativeError::limit("native V1 adapter allocation accounting overflow"))?;
    session.reserve_bytes(bytes)?;
    let mut values = Vec::new();
    values
        .try_reserve_exact(count)
        .map_err(|_| NativeError::limit("native V1 adapter allocation failed"))?;
    Ok(values)
}

fn boxed(value: &str, session: &mut Session<'_>) -> NativeResult<Box<str>> {
    Ok(owned_text(value, session)?.into_boxed_str())
}

fn owned_text(value: &str, session: &mut Session<'_>) -> NativeResult<String> {
    session.reserve_bytes(value.len())?;
    let mut output = String::new();
    output
        .try_reserve_exact(value.len())
        .map_err(|_| NativeError::limit("native V1 string allocation failed"))?;
    output.push_str(value);
    Ok(output)
}
