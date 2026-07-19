//! Bounded generated v1 fixture shared by Rust and extension contract tests.

use crate::error::NativeResult;
use crate::hash::sha256;
use crate::limits::Limits;
use crate::model::{CanonicalRowId, NativeArenaBuilder};

use super::records::*;
use super::NativeSnapshotPublicationV1;

pub(super) fn publication() -> NativeResult<NativeSnapshotPublicationV1> {
    draft()?.freeze()
}

pub(super) fn draft() -> NativeResult<PublicationDraftV1> {
    let document_key = boxed("d1:1111111111111111111111111111111111111111111111111111111111111111");
    let ontology_iri = boxed("urn:handoff:ontology");
    let ontology_id = OntologyIdV1 {
        ontology_iri: Some(ontology_iri.clone()),
        version_iri: None,
    };
    let source_sha256 = sha256(b"native handoff source");
    let document_fingerprint = fingerprint(b"document")?;
    let diagnostic = DiagnosticV1 {
        code: boxed("NATIVE_FIXTURE"),
        severity: DiagnosticSeverityV1::Info,
        message: boxed("retained publication fixture"),
        document_iri: None,
        byte_start: None,
        byte_end: None,
        line_start: None,
        column_start: None,
        line_end: None,
        column_end: None,
        import_chain: Box::new([]),
        details: vec![(boxed("fixture"), DiagnosticScalarV1::Boolean(true))].into_boxed_slice(),
    };
    let provenance = DocumentProvenanceV1 {
        source_sha256,
        digest_kind: DigestKindV1::ExactBytes,
        byte_length: 21,
        decoded_codepoint_length: 21,
        document_iri: Some(ontology_iri.clone()),
        acquisition_locator: None,
        format: DocumentFormatV1::Functional,
        detection_basis: DetectionBasisV1::Explicit,
        media_type: None,
        expected_sha256: None,
        parser: boxed("pyowl_core.backends.native.fixture"),
        backend: boxed("native"),
        api_version: ApiVersion(0, 1),
        model_schema: 1,
    };
    let document = DocumentPublicationV1 {
        document_key: document_key.clone(),
        ontology_id: ontology_id.clone(),
        document_iri: Some(ontology_iri.clone()),
        direct_imports: Box::new([]),
        provenance: provenance.clone(),
        document_fingerprint: document_fingerprint.clone(),
        diagnostics: vec![diagnostic.clone()].into_boxed_slice(),
        ontology_annotation_count: 0,
        axiom_count: 1,
        extension_count: 0,
        source_map_entry_count: 0,
        origin_entry_count: 1,
        rdf_mapping_conformant: None,
        rdf_mapping_report_sha256: None,
    };
    let mut arena_builder = NativeArenaBuilder::new(&Limits::default());
    let axiom = arena_builder.intern_canonical_row(&declaration_axiom())?;
    debug_assert_eq!(axiom, CanonicalRowId::from_raw(0));
    let arena = arena_builder.freeze()?;
    let members = DocumentMembersV1 {
        ontology_annotations: Box::new([]),
        axioms: vec![CanonicalRowId::from_raw(0)].into_boxed_slice(),
        extensions: Box::new([]),
    };
    let import_document = ImportDocumentV1 {
        document_key: document_key.clone(),
        ontology_id,
        document_iri: Some(ontology_iri),
        source_sha256,
        document_fingerprint,
        format: DocumentFormatV1::Functional,
        status: ImportDocumentStatusV1::Root,
    };
    let options = LoadOptionsV1 {
        format: None,
        imports: ImportPolicyV1::ResolveLocal,
        backend: BackendPreferenceV1::Native,
        limits: ParseLimitsV1::default(),
        offline: true,
        preserve_source_map: false,
        collect_provenance: true,
        validate_owl2_dl: false,
        deterministic: true,
    };
    let report = LoadReportV1 {
        backend: boxed("native"),
        api_version: ApiVersion(0, 1),
        model_schema: 1,
        document_count: 1,
        total_source_bytes: 21,
        effective_axiom_count: 1,
        resolution_attempts: 0,
        acquisition_cache_hits: 0,
        document_cache_hits: 0,
        timings: vec![(boxed("freeze_seconds"), 0.001)].into_boxed_slice(),
        structural_fingerprint: fingerprint(b"structural")?,
        logical_fingerprint: fingerprint(b"logical")?,
        signature_fingerprint: fingerprint(b"signature")?,
        owl2_dl_validated: false,
        owl2_dl_conforms: None,
        owl2_dl_report_sha256: None,
    };
    Ok(PublicationDraftV1 {
        arena,
        documents: vec![document].into_boxed_slice(),
        document_members: vec![members].into_boxed_slice(),
        import_manifest: ImportManifestV1 {
            policy: ImportPolicyV1::ResolveLocal,
            offline: true,
            resolver_configuration_fingerprint: sha256(b"resolver"),
            documents: vec![import_document].into_boxed_slice(),
            edges: Box::new([]),
        },
        root_document_key: document_key,
        load_options: options,
        diagnostics: vec![diagnostic].into_boxed_slice(),
        report,
        capability_bits: 1 | 2 | 4 | 16,
        root_table_sha256: sha256(b"roots"),
        fingerprint_inputs_sha256: sha256(b"fingerprint inputs"),
        source_manifest_sha256: sha256(b"sources"),
        provenance_manifest_sha256: sha256(b"provenance"),
    })
}

fn fingerprint(value: &[u8]) -> NativeResult<FingerprintV1> {
    Ok(FingerprintV1 {
        schema: PositiveIntegerV1::from_u64(1)?,
        digest: sha256(value),
    })
}

fn declaration_axiom() -> Vec<u8> {
    let iri = node(1, &[(2, b"urn:handoff:class")]);
    let entity = node(2, &[(5, b"class"), (1, &iri)]);
    let mut result = node(60, &[(1, &entity)]);
    result.extend_from_slice(&[6, 0]);
    result
}

fn node(tag: u8, fields: &[(u8, &[u8])]) -> Vec<u8> {
    let mut result = vec![tag];
    for (marker, value) in fields {
        result.push(*marker);
        result.extend(varint(value.len()));
        result.extend_from_slice(value);
    }
    result
}

fn varint(mut value: usize) -> Vec<u8> {
    let mut result = Vec::new();
    loop {
        let byte = (value & 0x7f) as u8;
        value >>= 7;
        result.push(if value == 0 { byte } else { byte | 0x80 });
        if value == 0 {
            return result;
        }
    }
}

fn boxed(value: &str) -> Box<str> {
    value.to_owned().into_boxed_str()
}
