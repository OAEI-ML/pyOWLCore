//! Rust-only retained-engine seam for the excluded WP14 comparator binary.
//!
//! This module is compiled only with the `comparator` feature. It exposes
//! bounded scalar evidence, never PyO3 objects or retained model internals.

use std::time::Instant;

use crate::cancel::{Cancellation, Guard};
use crate::canonical::iri;
use crate::error::NativeError;
use crate::hash::{sha256, Sha256};
use crate::ingestion::parse_rdfxml_retained_v2;
use crate::limits::Limits;
use crate::parse::{
    parse_retained, prepare_retained_publication_v2, RetainedParseMetadataV2, RetainedParsePhases,
};
use crate::publication::TypedFacadeStorageV2;
use crate::session::Session;
use crate::source::SourceRequest;

const FUNCTIONAL_SEED_MAGIC: &[u8; 8] = b"PYNFRS2\0";
const RDFXML_SEED_MAGIC: &[u8; 8] = b"PYNRRS2\0";
const SEED_SCHEMA: u16 = 1;
const DOCUMENT_KEY_DOMAIN: &[u8] = b"pyowl-core:document-key:v1\0";
const RESOLVER_DOMAIN: &[u8] = b"pyowl-core:resolver-configuration:v1\0";
const IMPORT_MANIFEST_DOMAIN: &[u8] = b"pyowl-core:import-manifest:v1\0";

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ComparatorFailureKind {
    Ineligible,
    Error,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ComparatorFailure {
    pub kind: ComparatorFailureKind,
    pub code: &'static str,
    pub message: &'static str,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ComparatorFingerprintEvidence {
    pub preimage_bytes: u64,
    pub sha256: [u8; 32],
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ComparatorRecordInventory {
    pub count: u64,
    pub canonical_bytes: u64,
    pub transcript_bytes: u64,
    pub sha256: [u8; 32],
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ComparatorOrigin {
    pub structural_sha256: [u8; 32],
    pub occurrence: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ComparatorPhaseEvidence {
    pub syntax_parse_ns: u64,
    pub rdf_mapping_ns: u64,
    pub result_encode_ns: u64,
    pub arena_construction_ns: u64,
    pub freeze_ns: u64,
    pub common_prepare_ns: u64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ComparatorCommonEvidence {
    pub source_sha256: [u8; 32],
    pub source_byte_count: u64,
    pub decoded_codepoints: u64,
    pub document_key: String,
    pub document_iri: Vec<u8>,
    pub ontology_iri: Option<Vec<u8>>,
    pub version_iri: Option<Vec<u8>>,
    pub fingerprints: [ComparatorFingerprintEvidence; 4],
    pub inventories: [ComparatorRecordInventory; 4],
    pub origins: Vec<ComparatorOrigin>,
    pub root_count: u64,
    pub node_count: u64,
    pub temporary_bytes: u64,
    pub phases: ComparatorPhaseEvidence,
}

/// Parse one import-free Functional Syntax document through the exact retained
/// native engine and publish all ontology-sized common-contract evidence.
pub fn load_functional_common(
    source: &[u8],
    document_iri: &str,
) -> Result<ComparatorCommonEvidence, ComparatorFailure> {
    let limits = Limits::default();
    let cancellation = Cancellation::with_duration(None);
    let mut guard = Guard::new(
        cancellation.clone(),
        limits.deadline,
        limits.cancellation_stride,
    );
    let mut session = Session::new(&mut guard, &limits, source.len()).map_err(failure)?;
    let request = SourceRequest {
        source,
        allow_swrl: true,
    };
    let outcome = parse_retained(
        request,
        &mut session,
        limits,
        cancellation.clone(),
        None,
        source.len(),
        true,
        false,
        true,
        false,
        false,
    )
    .map_err(failure)?;
    let metadata = outcome.metadata.ok_or(ComparatorFailure {
        kind: ComparatorFailureKind::Ineligible,
        code: "NATIVE_COMPARATOR_RETAINED_UNAVAILABLE",
        message: "native retained common evidence is unavailable for this document",
    })?;
    let seed = decode_functional_seed(&outcome.encoded).map_err(failure)?;
    finish_common(
        source,
        document_iri,
        outcome.storage,
        metadata,
        outcome.phases,
        0,
        seed,
        limits,
        cancellation,
    )
}

/// Parse one import-free RDF/XML document through the exact streaming mapper
/// and retained native engine, then publish common-contract evidence.
pub fn load_rdfxml_common(
    source: &[u8],
    document_iri: &str,
) -> Result<ComparatorCommonEvidence, ComparatorFailure> {
    let limits = Limits::default();
    let cancellation = Cancellation::with_duration(None);
    let mut guard = Guard::new(
        cancellation.clone(),
        limits.deadline,
        limits.cancellation_stride,
    );
    let caller_external_bytes =
        source
            .len()
            .checked_add(document_iri.len())
            .ok_or(ComparatorFailure {
                kind: ComparatorFailureKind::Error,
                code: "NATIVE_WIRE_LIMIT",
                message: "native comparator input accounting overflow",
            })?;
    let mut session = Session::new(&mut guard, &limits, caller_external_bytes).map_err(failure)?;
    let outcome = parse_rdfxml_retained_v2(
        source,
        Some(document_iri),
        &mut session,
        limits,
        cancellation.clone(),
        None,
        caller_external_bytes,
        true,
        false,
        true,
        true,
    )
    .map_err(failure)?;
    let seed = decode_rdfxml_seed(&outcome.encoded).map_err(failure)?;
    finish_common(
        source,
        document_iri,
        outcome.storage,
        outcome.metadata,
        outcome.phases,
        outcome.mapping_ns,
        seed,
        limits,
        cancellation,
    )
}

#[allow(clippy::too_many_arguments)]
fn finish_common(
    source: &[u8],
    document_iri: &str,
    storage: TypedFacadeStorageV2,
    metadata: RetainedParseMetadataV2,
    phases: RetainedParsePhases,
    rdf_mapping_ns: u64,
    seed: FunctionalSeed,
    limits: Limits,
    cancellation: Cancellation,
) -> Result<ComparatorCommonEvidence, ComparatorFailure> {
    if !seed.imports.is_empty() {
        return Err(ComparatorFailure {
            kind: ComparatorFailureKind::Ineligible,
            code: "NATIVE_COMPARATOR_IMPORTS_UNSUPPORTED",
            message: "direct retained comparator does not bypass import resolution",
        });
    }
    if metadata.closure_has_scoped_roots() {
        return Err(ComparatorFailure {
            kind: ComparatorFailureKind::Ineligible,
            code: "NATIVE_COMPARATOR_ANONYMOUS_UNSUPPORTED",
            message: "direct retained comparator does not compare anonymous identities",
        });
    }
    if seed.document_fingerprint != metadata.document_fingerprint.digest
        || seed.document_preimage_bytes != metadata.document_fingerprint.preimage_bytes
        || seed.root_counts != metadata.root_counts
    {
        return Err(failure(NativeError::protocol(
            "native retained comparator seed diverges from parser metadata",
        )));
    }

    let document_key = document_key(
        seed.ontology_iri.as_deref(),
        seed.version_iri.as_deref(),
        seed.document_fingerprint,
    )?;
    let manifest = import_free_manifest(
        &document_key,
        seed.ontology_iri.as_deref(),
        seed.version_iri.as_deref(),
        seed.document_fingerprint,
    )?;
    let prepare_started = Instant::now();
    let prepared = prepare_retained_publication_v2(
        &storage,
        &metadata,
        &manifest,
        &document_key,
        true,
        false,
        &limits,
        cancellation,
        None,
    )
    .map_err(failure)?;
    let common_prepare_ns = elapsed_ns(prepare_started)?;
    if prepared.document_key.as_ref() != document_key {
        return Err(failure(NativeError::protocol(
            "native retained comparator document key diverged",
        )));
    }
    let origins = decode_origins(
        prepared.origin_rows.as_deref().ok_or(ComparatorFailure {
            kind: ComparatorFailureKind::Error,
            code: "NATIVE_COMPARATOR_PROVENANCE_MISSING",
            message: "native retained comparator omitted required provenance",
        })?,
        &document_key,
    )?;
    let document_iri = iri(document_iri.to_owned()).map_err(failure)?.into_bytes();
    let ontology_iri = seed
        .ontology_iri
        .map(iri)
        .transpose()
        .map_err(failure)?
        .map(crate::canonical::Node::into_bytes);
    let version_iri = seed
        .version_iri
        .map(iri)
        .transpose()
        .map_err(failure)?
        .map(crate::canonical::Node::into_bytes);
    let source_byte_count = u64::try_from(source.len()).map_err(|_| ComparatorFailure {
        kind: ComparatorFailureKind::Error,
        code: "NATIVE_WIRE_LIMIT",
        message: "native comparator source length exceeds u64",
    })?;

    Ok(ComparatorCommonEvidence {
        source_sha256: sha256(source),
        source_byte_count,
        decoded_codepoints: seed.decoded_codepoints,
        document_key,
        document_iri,
        ontology_iri,
        version_iri,
        fingerprints: [
            ComparatorFingerprintEvidence {
                preimage_bytes: prepared.document_fingerprint.preimage_bytes,
                sha256: prepared.document_fingerprint.digest,
            },
            ComparatorFingerprintEvidence {
                preimage_bytes: prepared.structural_fingerprint.preimage_bytes,
                sha256: prepared.structural_fingerprint.digest,
            },
            ComparatorFingerprintEvidence {
                preimage_bytes: prepared.logical_fingerprint.preimage_bytes,
                sha256: prepared.logical_fingerprint.digest,
            },
            ComparatorFingerprintEvidence {
                preimage_bytes: prepared.signature_fingerprint.preimage_bytes,
                sha256: prepared.signature_fingerprint.digest,
            },
        ],
        inventories: prepared
            .record_inventories
            .map(|value| ComparatorRecordInventory {
                count: value.count,
                canonical_bytes: value.canonical_bytes,
                transcript_bytes: value.transcript_bytes,
                sha256: value.digest,
            }),
        origins,
        root_count: prepared.root_count,
        node_count: prepared.node_count,
        temporary_bytes: prepared.fingerprint_temporary_bytes,
        phases: ComparatorPhaseEvidence {
            syntax_parse_ns: phases.syntax_parse_ns,
            rdf_mapping_ns,
            result_encode_ns: phases.result_encode_ns,
            arena_construction_ns: phases.arena_construction_ns,
            freeze_ns: phases.freeze_ns,
            common_prepare_ns,
        },
    })
}

#[derive(Debug)]
struct FunctionalSeed {
    decoded_codepoints: u64,
    root_counts: [u64; 3],
    document_preimage_bytes: u64,
    document_fingerprint: [u8; 32],
    ontology_iri: Option<String>,
    version_iri: Option<String>,
    imports: Vec<String>,
}

fn decode_functional_seed(data: &[u8]) -> Result<FunctionalSeed, NativeError> {
    let mut reader = Reader::new(data);
    if reader.take(8)? != FUNCTIONAL_SEED_MAGIC {
        return Err(NativeError::protocol(
            "native comparator received an invalid retained seed",
        ));
    }
    if reader.u16()? != SEED_SCHEMA || reader.u16()? != 0 {
        return Err(NativeError::protocol(
            "native comparator received an unsupported retained seed",
        ));
    }
    let decoded_codepoints = reader.u64()?;
    let _canonical_rows_scanned = reader.u64()?;
    let _occurrence_count = reader.u64()?;
    let root_counts = [reader.u64()?, reader.u64()?, reader.u64()?];
    let _metadata_iri_objects = reader.u64()?;
    let document_preimage_bytes = reader.u64()?;
    let document_fingerprint = reader.bytes32()?;
    let ontology_iri = reader.optional_text()?;
    let version_iri = reader.optional_text()?;
    let import_count = reader.u64()?;
    let capacity = usize::try_from(import_count)
        .map_err(|_| NativeError::limit("native comparator import count exceeds usize"))?;
    let mut imports = Vec::new();
    imports
        .try_reserve_exact(capacity)
        .map_err(|_| NativeError::limit("native comparator import allocation failed"))?;
    for _ in 0..capacity {
        imports.push(reader.text()?);
    }
    if !reader.is_finished() {
        return Err(NativeError::protocol(
            "native comparator retained seed contains trailing bytes",
        ));
    }
    Ok(FunctionalSeed {
        decoded_codepoints,
        root_counts,
        document_preimage_bytes,
        document_fingerprint,
        ontology_iri,
        version_iri,
        imports,
    })
}

fn decode_rdfxml_seed(data: &[u8]) -> Result<FunctionalSeed, NativeError> {
    let mut reader = Reader::new(data);
    if reader.take(8)? != RDFXML_SEED_MAGIC {
        return Err(NativeError::protocol(
            "native comparator received an invalid retained RDF/XML seed",
        ));
    }
    if reader.u16()? != SEED_SCHEMA || reader.u16()? != 0 {
        return Err(NativeError::protocol(
            "native comparator received an unsupported retained RDF/XML seed",
        ));
    }
    let decoded_codepoints = reader.u64()?;
    let _canonical_rows_scanned = reader.u64()?;
    let _occurrence_count = reader.u64()?;
    let root_counts = [reader.u64()?, reader.u64()?, reader.u64()?];
    let _metadata_iri_objects = reader.u64()?;
    let document_preimage_bytes = reader.u64()?;
    let document_fingerprint = reader.bytes32()?;
    let ontology_iri = reader.optional_text()?;
    let version_iri = reader.optional_text()?;
    let import_count = reader.u64()?;
    let capacity = usize::try_from(import_count)
        .map_err(|_| NativeError::limit("native comparator import count exceeds usize"))?;
    let mut imports = Vec::new();
    imports
        .try_reserve_exact(capacity)
        .map_err(|_| NativeError::limit("native comparator import allocation failed"))?;
    for _ in 0..capacity {
        imports.push(reader.text()?);
    }
    let _total_triples = reader.u64()?;
    if !reader.is_finished() {
        return Err(NativeError::protocol(
            "native comparator retained RDF/XML seed contains trailing bytes",
        ));
    }
    Ok(FunctionalSeed {
        decoded_codepoints,
        root_counts,
        document_preimage_bytes,
        document_fingerprint,
        ontology_iri,
        version_iri,
        imports,
    })
}

fn document_key(
    ontology_iri: Option<&str>,
    version_iri: Option<&str>,
    document_fingerprint: [u8; 32],
) -> Result<String, ComparatorFailure> {
    let mut payload = Vec::new();
    if let Some(ontology) = ontology_iri {
        payload.extend_from_slice(b"named");
        frame_varint(&mut payload, b"ontology")?;
        frame_varint(&mut payload, ontology.as_bytes())?;
        if let Some(version) = version_iri {
            payload.truncate(b"named".len());
            frame_varint(&mut payload, b"version")?;
            frame_varint(&mut payload, ontology.as_bytes())?;
            frame_varint(&mut payload, version.as_bytes())?;
        }
    } else {
        if version_iri.is_some() {
            return Err(failure(NativeError::protocol(
                "native comparator version IRI lacks an ontology IRI",
            )));
        }
        payload.extend_from_slice(b"anonymous");
        payload.extend_from_slice(&document_fingerprint);
    }
    let mut hasher = Sha256::new();
    hasher.update(DOCUMENT_KEY_DOMAIN);
    hasher.update(&payload);
    Ok(format!("d1:{}", hex(hasher.finish())))
}

fn import_free_manifest(
    document_key: &str,
    ontology_iri: Option<&str>,
    version_iri: Option<&str>,
    document_fingerprint: [u8; 32],
) -> Result<Vec<u8>, ComparatorFailure> {
    let mut resolver_preimage = Vec::new();
    resolver_preimage.extend_from_slice(RESOLVER_DOMAIN);
    frame_varint(&mut resolver_preimage, b"none")?;
    let resolver = sha256(&resolver_preimage);

    let mut manifest = Vec::new();
    manifest.extend_from_slice(IMPORT_MANIFEST_DOMAIN);
    frame_varint(&mut manifest, b"record_unresolved")?;
    manifest.push(1);
    manifest.extend_from_slice(&resolver);
    varint(&mut manifest, 1)?;
    frame_varint(&mut manifest, document_key.as_bytes())?;
    optional_iri(&mut manifest, ontology_iri)?;
    optional_iri(&mut manifest, version_iri)?;
    manifest.extend_from_slice(&document_fingerprint);
    frame_varint(&mut manifest, b"root")?;
    varint(&mut manifest, 0)?;
    Ok(manifest)
}

fn optional_iri(output: &mut Vec<u8>, value: Option<&str>) -> Result<(), ComparatorFailure> {
    match value {
        Some(text) => {
            output.push(b'1');
            let canonical = iri(text.to_owned()).map_err(failure)?.into_bytes();
            frame_varint(output, &canonical)
        }
        None => {
            output.push(b'0');
            Ok(())
        }
    }
}

fn decode_origins(
    rows: &[Vec<u8>],
    document_key: &str,
) -> Result<Vec<ComparatorOrigin>, ComparatorFailure> {
    let mut output = Vec::new();
    output
        .try_reserve_exact(rows.len())
        .map_err(|_| ComparatorFailure {
            kind: ComparatorFailureKind::Error,
            code: "NATIVE_WIRE_LIMIT",
            message: "native comparator origin allocation failed",
        })?;
    for row in rows {
        let mut reader = Reader::new(row);
        let structural_sha256 = reader.bytes32().map_err(failure)?;
        let key_size =
            usize::try_from(reader.u32().map_err(failure)?).map_err(|_| ComparatorFailure {
                kind: ComparatorFailureKind::Error,
                code: "NATIVE_WIRE_LIMIT",
                message: "native comparator origin key exceeds usize",
            })?;
        if reader.take(key_size).map_err(failure)? != document_key.as_bytes() {
            return Err(failure(NativeError::protocol(
                "native comparator origin document key diverged",
            )));
        }
        let occurrence = reader.u64().map_err(failure)?;
        match reader.byte().map_err(failure)? {
            0 => {}
            0x8f => {
                for _ in 0..4 {
                    let _coordinate = reader.u64().map_err(failure)?;
                }
            }
            _ => {
                return Err(failure(NativeError::protocol(
                    "native comparator origin span marker is invalid",
                )))
            }
        }
        if !reader.is_finished() {
            return Err(failure(NativeError::protocol(
                "native comparator origin row contains trailing bytes",
            )));
        }
        output.push(ComparatorOrigin {
            structural_sha256,
            occurrence,
        });
    }
    Ok(output)
}

fn failure(error: NativeError) -> ComparatorFailure {
    ComparatorFailure {
        kind: if error.code == "NATIVE_RDFXML_RETAINED_UNSUPPORTED" {
            ComparatorFailureKind::Ineligible
        } else {
            ComparatorFailureKind::Error
        },
        code: error.code,
        message: error.message,
    }
}

fn elapsed_ns(started: Instant) -> Result<u64, ComparatorFailure> {
    u64::try_from(started.elapsed().as_nanos()).map_err(|_| ComparatorFailure {
        kind: ComparatorFailureKind::Error,
        code: "NATIVE_WIRE_LIMIT",
        message: "native comparator phase duration exceeds u64",
    })
}

fn varint(output: &mut Vec<u8>, mut value: u64) -> Result<(), ComparatorFailure> {
    loop {
        let byte = (value & 0x7f) as u8;
        value >>= 7;
        output.try_reserve(1).map_err(|_| ComparatorFailure {
            kind: ComparatorFailureKind::Error,
            code: "NATIVE_WIRE_LIMIT",
            message: "native comparator framing allocation failed",
        })?;
        output.push(byte | if value == 0 { 0 } else { 0x80 });
        if value == 0 {
            return Ok(());
        }
    }
}

fn frame_varint(output: &mut Vec<u8>, value: &[u8]) -> Result<(), ComparatorFailure> {
    varint(
        output,
        u64::try_from(value.len()).map_err(|_| ComparatorFailure {
            kind: ComparatorFailureKind::Error,
            code: "NATIVE_WIRE_LIMIT",
            message: "native comparator frame exceeds u64",
        })?,
    )?;
    output
        .try_reserve(value.len())
        .map_err(|_| ComparatorFailure {
            kind: ComparatorFailureKind::Error,
            code: "NATIVE_WIRE_LIMIT",
            message: "native comparator frame allocation failed",
        })?;
    output.extend_from_slice(value);
    Ok(())
}

fn hex(value: [u8; 32]) -> String {
    use std::fmt::Write;

    value.iter().fold(String::new(), |mut output, byte| {
        let _ = write!(output, "{byte:02x}");
        output
    })
}

struct Reader<'a> {
    data: &'a [u8],
    offset: usize,
}

impl<'a> Reader<'a> {
    fn new(data: &'a [u8]) -> Self {
        Self { data, offset: 0 }
    }

    fn take(&mut self, size: usize) -> Result<&'a [u8], NativeError> {
        let end = self
            .offset
            .checked_add(size)
            .ok_or_else(|| NativeError::limit("native comparator framing overflow"))?;
        let value = self
            .data
            .get(self.offset..end)
            .ok_or_else(|| NativeError::protocol("native comparator framing is truncated"))?;
        self.offset = end;
        Ok(value)
    }

    fn byte(&mut self) -> Result<u8, NativeError> {
        Ok(self.take(1)?[0])
    }

    fn u16(&mut self) -> Result<u16, NativeError> {
        let bytes: [u8; 2] = self
            .take(2)?
            .try_into()
            .map_err(|_| NativeError::protocol("native comparator u16 is truncated"))?;
        Ok(u16::from_le_bytes(bytes))
    }

    fn u32(&mut self) -> Result<u32, NativeError> {
        let bytes: [u8; 4] = self
            .take(4)?
            .try_into()
            .map_err(|_| NativeError::protocol("native comparator u32 is truncated"))?;
        Ok(u32::from_le_bytes(bytes))
    }

    fn u64(&mut self) -> Result<u64, NativeError> {
        let bytes: [u8; 8] = self
            .take(8)?
            .try_into()
            .map_err(|_| NativeError::protocol("native comparator u64 is truncated"))?;
        Ok(u64::from_le_bytes(bytes))
    }

    fn bytes32(&mut self) -> Result<[u8; 32], NativeError> {
        self.take(32)?
            .try_into()
            .map_err(|_| NativeError::protocol("native comparator digest is truncated"))
    }

    fn text(&mut self) -> Result<String, NativeError> {
        let size = usize::try_from(self.u64()?)
            .map_err(|_| NativeError::limit("native comparator text exceeds usize"))?;
        let value = self.take(size)?;
        std::str::from_utf8(value)
            .map(str::to_owned)
            .map_err(|_| NativeError::protocol("native comparator text is not UTF-8"))
    }

    fn optional_text(&mut self) -> Result<Option<String>, NativeError> {
        match self.byte()? {
            0 => Ok(None),
            1 => self.text().map(Some),
            _ => Err(NativeError::protocol(
                "native comparator optional text marker is invalid",
            )),
        }
    }

    fn is_finished(&self) -> bool {
        self.offset == self.data.len()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn retained_functional_evidence_matches_known_document_identity() {
        let source =
            b"Ontology(<https://example.org/o> Declaration(Class(<https://example.org/C>)))";
        let result = load_functional_common(
            source,
            "urn:pyowl-core:comparator-source:sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )
        .expect("retained comparator evidence");

        assert_eq!(result.source_sha256, sha256(source));
        assert_eq!(result.inventories[1].count, 1);
        assert_eq!(result.root_count, 1);
        assert_eq!(result.origins.len(), 1);
        assert!(result.document_key.starts_with("d1:"));
        assert!(result
            .fingerprints
            .iter()
            .all(|value| value.preimage_bytes > 0));
    }

    #[test]
    fn imports_and_anonymous_rows_fail_closed() {
        let document_iri =
            "urn:pyowl-core:comparator-source:sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
        let imported = load_functional_common(
            b"Ontology(Import(<https://example.org/imported>))",
            document_iri,
        )
        .expect_err("imports are ineligible");
        assert_eq!(imported.kind, ComparatorFailureKind::Ineligible);

        let anonymous = load_functional_common(
            b"Ontology(ObjectPropertyAssertion(<https://example.org/p> _:a _:b))",
            document_iri,
        )
        .expect_err("anonymous rows are ineligible");
        assert_eq!(anonymous.kind, ComparatorFailureKind::Ineligible);
    }

    #[test]
    fn retained_rdfxml_evidence_uses_streaming_mapper_storage() {
        let source = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:owl="http://www.w3.org/2002/07/owl#">
          <owl:Ontology rdf:about="https://example.org/o"/>
          <owl:Class rdf:about="https://example.org/C"/>
        </rdf:RDF>"#;
        let result = load_rdfxml_common(
            source,
            "urn:pyowl-core:comparator-source:sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        )
        .expect("retained RDF/XML comparator evidence");

        assert_eq!(result.source_sha256, sha256(source));
        assert_eq!(result.inventories[1].count, 1);
        assert_eq!(result.root_count, 1);
        assert_eq!(result.origins.len(), 1);
        assert!(result.document_key.starts_with("d1:"));
    }
}
