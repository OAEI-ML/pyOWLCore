//! Exact tagged-length-framed v1 attestation encoding.

use crate::error::{NativeError, NativeResult};
use crate::hash::sha256;

use super::records::{
    DeadlineSecondsV1, DiagnosticScalarV1, DiagnosticV1, DocumentPublicationV1, FingerprintV1,
    ImportManifestV1, LoadOptionsV1, LoadReportV1, NativeSnapshotAttestationV1, OntologyIdV1,
    ParseLimitsV1, PositiveIntegerV1,
};

const ATTESTATION_DOMAIN: &[u8] = b"pyowl-core:native-snapshot-publication-attestation:v1\0";
const DIAGNOSTIC_DOMAIN: &[u8] = b"pyowl-core:native-diagnostic:v1\0";
const DIAGNOSTICS_DOMAIN: &[u8] = b"pyowl-core:native-diagnostics-manifest:v1\0";
const LOAD_OPTIONS_DOMAIN: &[u8] = b"pyowl-core:native-load-options:v1\0";
const REPORT_DOMAIN: &[u8] = b"pyowl-core:native-load-report:v1\0";

pub(super) fn attestation_bytes(
    attestation: &NativeSnapshotAttestationV1,
) -> NativeResult<Vec<u8>> {
    let values = [
        scalar_u64(u64::from(attestation.version))?,
        scalar_bytes(&attestation.ledger_sha256)?,
        scalar_bytes(&attestation.root_table_sha256)?,
        scalar_bytes(&attestation.fingerprint_inputs_sha256)?,
        scalar_bytes(&attestation.source_manifest_sha256)?,
        scalar_bytes(&attestation.provenance_manifest_sha256)?,
        scalar_bytes(&attestation.diagnostics_manifest_sha256)?,
        scalar_bytes(&attestation.load_options_sha256)?,
        scalar_bytes(&attestation.report_sha256)?,
        scalar_u64(attestation.document_count)?,
        scalar_u64(attestation.import_edge_count)?,
        scalar_u64(attestation.diagnostic_count)?,
        scalar_u64(attestation.ontology_annotation_count)?,
        scalar_u64(attestation.stored_axiom_count)?,
        scalar_u64(attestation.effective_axiom_count)?,
        scalar_u64(attestation.extension_count)?,
        scalar_u64(attestation.total_source_bytes)?,
        scalar_u64(attestation.source_map_entry_count)?,
        scalar_u64(attestation.origin_entry_count)?,
        scalar_u64(attestation.rdf_mapping_report_count)?,
        scalar_u64(attestation.capability_bits)?,
        scalar_sequence(&[
            scalar_u64(u64::from(attestation.api_version.0))?,
            scalar_u64(u64::from(attestation.api_version.1))?,
        ])?,
        scalar_u64(u64::from(attestation.model_schema))?,
        scalar_str(&attestation.backend)?,
        scalar_str(&attestation.root_document_key)?,
        scalar_bool(attestation.owl2_dl_validated)?,
        scalar_optional_bool(attestation.owl2_dl_conforms)?,
        scalar_optional_bytes(attestation.owl2_dl_report_sha256.as_ref())?,
    ];
    domain_sequence(ATTESTATION_DOMAIN, &values)
}

pub(super) fn attestation_digest(
    attestation: &NativeSnapshotAttestationV1,
) -> NativeResult<[u8; 32]> {
    Ok(sha256(&attestation_bytes(attestation)?))
}

pub(super) fn load_options_digest(options: &LoadOptionsV1) -> NativeResult<[u8; 32]> {
    Ok(sha256(&load_options_bytes(options)?))
}

fn load_options_bytes(options: &LoadOptionsV1) -> NativeResult<Vec<u8>> {
    let values = [
        match options.format {
            Some(value) => scalar_str(value.as_str())?,
            None => scalar_none()?,
        },
        scalar_str(options.imports.as_str())?,
        scalar_str(options.backend.as_str())?,
        parse_limits_scalar(&options.limits)?,
        scalar_bool(options.offline)?,
        scalar_bool(options.preserve_source_map)?,
        scalar_bool(options.collect_provenance)?,
        scalar_bool(options.validate_owl2_dl)?,
        scalar_bool(options.deterministic)?,
    ];
    domain_sequence(LOAD_OPTIONS_DOMAIN, &values)
}

pub(super) fn report_digest(report: &LoadReportV1) -> NativeResult<[u8; 32]> {
    Ok(sha256(&report_bytes(report)?))
}

fn report_bytes(report: &LoadReportV1) -> NativeResult<Vec<u8>> {
    let mut timing_values = Vec::new();
    timing_values
        .try_reserve_exact(report.timings.len())
        .map_err(|_| NativeError::limit("native publication timing encoding allocation failed"))?;
    for (name, value) in &report.timings {
        timing_values.push(scalar_sequence(&[scalar_str(name)?, scalar_f64(*value)?])?);
    }
    let values = [
        scalar_str(&report.backend)?,
        scalar_sequence(&[
            scalar_u64(u64::from(report.api_version.0))?,
            scalar_u64(u64::from(report.api_version.1))?,
        ])?,
        scalar_u64(u64::from(report.model_schema))?,
        scalar_u64(report.document_count)?,
        scalar_u64(report.total_source_bytes)?,
        scalar_u64(report.effective_axiom_count)?,
        scalar_u64(report.resolution_attempts)?,
        scalar_u64(report.acquisition_cache_hits)?,
        scalar_u64(report.document_cache_hits)?,
        scalar_sequence(&timing_values)?,
        scalar_fingerprint(&report.structural_fingerprint)?,
        scalar_fingerprint(&report.logical_fingerprint)?,
        scalar_fingerprint(&report.signature_fingerprint)?,
        scalar_bool(report.owl2_dl_validated)?,
        scalar_optional_bool(report.owl2_dl_conforms)?,
        scalar_optional_bytes(report.owl2_dl_report_sha256.as_ref())?,
    ];
    domain_sequence(REPORT_DOMAIN, &values)
}

pub(super) fn diagnostics_digest(
    diagnostics: &[DiagnosticV1],
    documents: &[DocumentPublicationV1],
    manifest: &ImportManifestV1,
) -> NativeResult<[u8; 32]> {
    Ok(sha256(&diagnostics_bytes(
        diagnostics,
        documents,
        manifest,
    )?))
}

fn diagnostics_bytes(
    diagnostics: &[DiagnosticV1],
    documents: &[DocumentPublicationV1],
    manifest: &ImportManifestV1,
) -> NativeResult<Vec<u8>> {
    let mut body = owned(DIAGNOSTICS_DOMAIN)?;
    let mut root_diagnostics = Vec::new();
    root_diagnostics
        .try_reserve_exact(diagnostics.len())
        .map_err(|_| NativeError::limit("native diagnostic encoding allocation failed"))?;
    for diagnostic in diagnostics {
        root_diagnostics.push(scalar_diagnostic(diagnostic)?);
    }
    append(&mut body, &scalar_sequence(&root_diagnostics)?)?;
    for document in documents {
        append(&mut body, &scalar_str(&document.document_key)?)?;
        let mut values = Vec::new();
        values
            .try_reserve_exact(document.diagnostics.len())
            .map_err(|_| NativeError::limit("native diagnostic encoding allocation failed"))?;
        for diagnostic in &document.diagnostics {
            values.push(scalar_diagnostic(diagnostic)?);
        }
        append(&mut body, &scalar_sequence(&values)?)?;
    }
    for edge in &manifest.edges {
        append(&mut body, &scalar_str(&edge.importing_document_key)?)?;
        // Python freezes the edge IRI to ``IRI`` metadata, then deliberately
        // encodes ``import_iri.value`` here as an ordinary string scalar.
        append(&mut body, &scalar_str(&edge.import_iri)?)?;
        append(
            &mut body,
            &match &edge.diagnostic {
                Some(value) => scalar_diagnostic(value)?,
                None => scalar_none()?,
            },
        )?;
    }
    Ok(body)
}

fn diagnostic_bytes(diagnostic: &DiagnosticV1) -> NativeResult<Vec<u8>> {
    let mut chain = Vec::new();
    chain
        .try_reserve_exact(diagnostic.import_chain.len())
        .map_err(|_| NativeError::limit("native diagnostic chain allocation failed"))?;
    for value in &diagnostic.import_chain {
        chain.push(scalar_str(value)?);
    }
    let mut details = Vec::new();
    details
        .try_reserve_exact(diagnostic.details.len())
        .map_err(|_| NativeError::limit("native diagnostic detail allocation failed"))?;
    for (key, value) in &diagnostic.details {
        let value = match value {
            DiagnosticScalarV1::Text(value) => scalar_str(value)?,
            DiagnosticScalarV1::Integer(value) => scalar_i64(*value)?,
            DiagnosticScalarV1::Boolean(value) => scalar_bool(*value)?,
        };
        details.push(scalar_sequence(&[scalar_str(key)?, value])?);
    }
    let values = [
        scalar_str(&diagnostic.code)?,
        scalar_str(diagnostic.severity.as_str())?,
        scalar_str(&diagnostic.message)?,
        scalar_optional_str(diagnostic.document_iri.as_deref())?,
        scalar_optional_u64(diagnostic.byte_start)?,
        scalar_optional_u64(diagnostic.byte_end)?,
        scalar_optional_u64(diagnostic.line_start)?,
        scalar_optional_u64(diagnostic.column_start)?,
        scalar_optional_u64(diagnostic.line_end)?,
        scalar_optional_u64(diagnostic.column_end)?,
        scalar_sequence(&chain)?,
        scalar_sequence(&details)?,
    ];
    domain_sequence(DIAGNOSTIC_DOMAIN, &values)
}

fn parse_limits_scalar(limits: &ParseLimitsV1) -> NativeResult<Vec<u8>> {
    // PositiveIntegerV1 retains the producer's canonical Python decimal. In
    // particular, an integer deadline must never be coerced through f64 or
    // narrowed to u64 merely because native runtime counters use u64.
    let values = [
        scalar_positive(&limits.max_source_bytes)?,
        scalar_positive(&limits.max_documents)?,
        scalar_positive(&limits.max_total_source_bytes)?,
        scalar_positive(&limits.max_axioms)?,
        scalar_positive(&limits.max_terms)?,
        scalar_positive(&limits.max_nesting_depth)?,
        scalar_positive(&limits.max_rdf_list_length)?,
        scalar_positive(&limits.max_literal_bytes)?,
        scalar_positive(&limits.max_iri_bytes)?,
        scalar_positive(&limits.max_prefixes)?,
        scalar_positive(&limits.max_import_depth)?,
        scalar_positive(&limits.max_redirects)?,
        scalar_positive(&limits.max_diagnostics)?,
        scalar_optional_positive(limits.max_memory_bytes.as_ref())?,
        match &limits.deadline_seconds {
            None => scalar_none()?,
            Some(DeadlineSecondsV1::Integer(value)) => scalar_positive(value)?,
            Some(DeadlineSecondsV1::Float(value)) => scalar_f64(*value)?,
        },
        scalar_positive(&limits.max_triples)?,
        scalar_positive(&limits.max_strings)?,
        scalar_positive(&limits.max_annotations)?,
        scalar_positive(&limits.max_rule_atoms)?,
        scalar_positive(&limits.max_sequence_arity)?,
        scalar_positive(&limits.max_catalog_rewrites)?,
        scalar_positive(&limits.max_resolver_attempts)?,
        scalar_positive(&limits.max_concurrent_fetches)?,
        scalar_positive(&limits.max_source_map_entries)?,
        scalar_positive(&limits.max_origin_entries)?,
        scalar_positive(&limits.max_overlay_depth)?,
        scalar_positive(&limits.max_delta_entries)?,
        scalar_positive(&limits.max_composite_members)?,
        scalar_positive(&limits.max_index_rows)?,
        scalar_positive(&limits.max_index_bytes)?,
        scalar_positive(&limits.max_wire_rows)?,
        scalar_positive(&limits.max_wire_bytes)?,
        scalar_positive(&limits.max_temporary_bytes)?,
        scalar_positive(&limits.max_disk_cache_bytes)?,
        scalar_positive(&limits.max_decompressed_bytes)?,
        scalar_positive(&limits.max_canonical_work)?,
        scalar_positive(&limits.cancellation_check_interval)?,
    ];
    scalar_sequence(&values)
}

fn scalar_diagnostic(value: &DiagnosticV1) -> NativeResult<Vec<u8>> {
    tagged_frame(b'd', &diagnostic_bytes(value)?)
}

fn scalar_fingerprint(value: &FingerprintV1) -> NativeResult<Vec<u8>> {
    let sequence = scalar_sequence(&[
        scalar_str("sha256")?,
        scalar_positive(&value.schema)?,
        scalar_bytes(&value.digest)?,
    ])?;
    prefixed(b'p', &sequence)
}

#[allow(dead_code)]
fn scalar_ontology_id(value: &OntologyIdV1) -> NativeResult<Vec<u8>> {
    let sequence = scalar_sequence(&[
        scalar_optional_iri(value.ontology_iri.as_deref())?,
        scalar_optional_iri(value.version_iri.as_deref())?,
    ])?;
    prefixed(b'o', &sequence)
}

fn scalar_optional_iri(value: Option<&str>) -> NativeResult<Vec<u8>> {
    value.map_or_else(scalar_none, scalar_iri)
}

fn scalar_iri(value: &str) -> NativeResult<Vec<u8>> {
    tagged_frame(b'r', value.as_bytes())
}

fn scalar_optional_str(value: Option<&str>) -> NativeResult<Vec<u8>> {
    value.map_or_else(scalar_none, scalar_str)
}

fn scalar_str(value: &str) -> NativeResult<Vec<u8>> {
    tagged_frame(b's', value.as_bytes())
}

fn scalar_optional_bytes(value: Option<&[u8; 32]>) -> NativeResult<Vec<u8>> {
    value.map_or_else(scalar_none, |value| scalar_bytes(value))
}

fn scalar_bytes(value: &[u8]) -> NativeResult<Vec<u8>> {
    tagged_frame(b'y', value)
}

fn scalar_optional_u64(value: Option<u64>) -> NativeResult<Vec<u8>> {
    value.map_or_else(scalar_none, scalar_u64)
}

fn scalar_optional_positive(value: Option<&PositiveIntegerV1>) -> NativeResult<Vec<u8>> {
    value.map_or_else(scalar_none, scalar_positive)
}

fn scalar_positive(value: &PositiveIntegerV1) -> NativeResult<Vec<u8>> {
    prefixed_terminated(b'i', value.decimal().as_bytes())
}

fn scalar_u64(value: u64) -> NativeResult<Vec<u8>> {
    let mut decimal = [0_u8; 39];
    let digits = unsigned_decimal(u128::from(value), &mut decimal);
    prefixed_terminated(b'i', digits)
}

fn scalar_i64(value: i64) -> NativeResult<Vec<u8>> {
    let mut decimal = [0_u8; 39];
    let digits = unsigned_decimal(u128::from(value.unsigned_abs()), &mut decimal);
    if value >= 0 {
        return prefixed_terminated(b'i', digits);
    }
    let capacity = digits.len().checked_add(3).ok_or_else(encoding_limit)?;
    let mut result = reserved(capacity)?;
    result.extend_from_slice(b"i-");
    result.extend_from_slice(digits);
    result.push(b';');
    Ok(result)
}

fn scalar_f64(value: f64) -> NativeResult<Vec<u8>> {
    prefixed_terminated(b'f', &python_float_hex(value)?)
}

fn scalar_optional_bool(value: Option<bool>) -> NativeResult<Vec<u8>> {
    value.map_or_else(scalar_none, scalar_bool)
}

fn scalar_bool(value: bool) -> NativeResult<Vec<u8>> {
    owned(if value { b"b1" } else { b"b0" })
}

fn scalar_none() -> NativeResult<Vec<u8>> {
    owned(b"n")
}

fn scalar_sequence(values: &[Vec<u8>]) -> NativeResult<Vec<u8>> {
    let mut body_length = 0_usize;
    for value in values {
        body_length = body_length
            .checked_add(decimal_length(value.len()))
            .and_then(|length| length.checked_add(1))
            .and_then(|length| length.checked_add(value.len()))
            .ok_or_else(encoding_limit)?;
    }
    let capacity = 1_usize
        .checked_add(decimal_length(body_length))
        .and_then(|length| length.checked_add(1))
        .and_then(|length| length.checked_add(body_length))
        .ok_or_else(encoding_limit)?;
    let mut result = reserved(capacity)?;
    result.push(b'q');
    append_decimal_usize(&mut result, body_length)?;
    result.push(b':');
    for value in values {
        append_frame(&mut result, value)?;
    }
    Ok(result)
}

fn domain_sequence(domain: &[u8], values: &[Vec<u8>]) -> NativeResult<Vec<u8>> {
    let sequence = scalar_sequence(values)?;
    let capacity = domain
        .len()
        .checked_add(sequence.len())
        .ok_or_else(encoding_limit)?;
    let mut result = reserved(capacity)?;
    result.extend_from_slice(domain);
    result.extend_from_slice(&sequence);
    Ok(result)
}

fn tagged_frame(tag: u8, value: &[u8]) -> NativeResult<Vec<u8>> {
    let capacity = 1_usize
        .checked_add(decimal_length(value.len()))
        .and_then(|length| length.checked_add(1))
        .and_then(|length| length.checked_add(value.len()))
        .ok_or_else(encoding_limit)?;
    let mut result = reserved(capacity)?;
    result.push(tag);
    append_decimal_usize(&mut result, value.len())?;
    result.push(b':');
    result.extend_from_slice(value);
    Ok(result)
}

fn prefixed(tag: u8, value: &[u8]) -> NativeResult<Vec<u8>> {
    let capacity = value.len().checked_add(1).ok_or_else(encoding_limit)?;
    let mut result = reserved(capacity)?;
    result.push(tag);
    result.extend_from_slice(value);
    Ok(result)
}

fn prefixed_terminated(tag: u8, value: &[u8]) -> NativeResult<Vec<u8>> {
    let capacity = value.len().checked_add(2).ok_or_else(encoding_limit)?;
    let mut result = reserved(capacity)?;
    result.push(tag);
    result.extend_from_slice(value);
    result.push(b';');
    Ok(result)
}

fn append_frame(target: &mut Vec<u8>, value: &[u8]) -> NativeResult<()> {
    let additional = decimal_length(value.len())
        .checked_add(1)
        .and_then(|length| length.checked_add(value.len()))
        .ok_or_else(encoding_limit)?;
    reserve(target, additional)?;
    append_decimal_usize(target, value.len())?;
    append_byte(target, b':')?;
    append(target, value)
}

fn append(target: &mut Vec<u8>, value: &[u8]) -> NativeResult<()> {
    reserve(target, value.len())?;
    target.extend_from_slice(value);
    Ok(())
}

fn append_byte(target: &mut Vec<u8>, value: u8) -> NativeResult<()> {
    reserve(target, 1)?;
    target.push(value);
    Ok(())
}

fn reserve(target: &mut Vec<u8>, additional: usize) -> NativeResult<()> {
    target
        .try_reserve_exact(additional)
        .map_err(|_| encoding_limit())
}

fn reserved(capacity: usize) -> NativeResult<Vec<u8>> {
    let mut result = Vec::new();
    reserve(&mut result, capacity)?;
    Ok(result)
}

const fn encoding_limit() -> NativeError {
    NativeError::limit("native publication encoding allocation failed")
}

fn decimal_length(mut value: usize) -> usize {
    let mut length = 1;
    while value >= 10 {
        value /= 10;
        length += 1;
    }
    length
}

fn append_decimal_usize(target: &mut Vec<u8>, value: usize) -> NativeResult<()> {
    let mut decimal = [0_u8; 39];
    append(target, unsigned_decimal(value as u128, &mut decimal))
}

fn unsigned_decimal(mut value: u128, output: &mut [u8; 39]) -> &[u8] {
    let mut offset = output.len();
    loop {
        offset -= 1;
        output[offset] = b'0' + u8::try_from(value % 10).expect("decimal digit fits in u8");
        value /= 10;
        if value == 0 {
            return &output[offset..];
        }
    }
}

fn append_fraction_hex(target: &mut Vec<u8>, fraction: u64) -> NativeResult<()> {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    reserve(target, 13)?;
    for shift in (0..=48).rev().step_by(4) {
        let index = usize::try_from((fraction >> shift) & 0xf).expect("hex digit fits usize");
        target.push(HEX[index]);
    }
    Ok(())
}

fn owned(value: &[u8]) -> NativeResult<Vec<u8>> {
    let mut result = reserved(value.len())?;
    result.extend_from_slice(value);
    Ok(result)
}

fn python_float_hex(value: f64) -> NativeResult<Vec<u8>> {
    if !value.is_finite() {
        return Err(NativeError::protocol(
            "native publication float must be finite",
        ));
    }
    let bits = value.to_bits();
    let negative = bits >> 63 != 0;
    let exponent_bits = ((bits >> 52) & 0x7ff) as i32;
    let fraction = bits & ((1_u64 << 52) - 1);
    let mut result = reserved(24)?;
    if negative {
        result.push(b'-');
    }
    if exponent_bits == 0 {
        if fraction == 0 {
            result.extend_from_slice(b"0x0.0p+0");
            return Ok(result);
        }
        result.extend_from_slice(b"0x0.");
        append_fraction_hex(&mut result, fraction)?;
        result.extend_from_slice(b"p-1022");
        return Ok(result);
    }
    let exponent = exponent_bits - 1023;
    result.extend_from_slice(b"0x1.");
    append_fraction_hex(&mut result, fraction)?;
    result.push(b'p');
    if exponent < 0 {
        result.push(b'-');
    } else {
        result.push(b'+');
    }
    let magnitude = u128::from(exponent.unsigned_abs());
    let mut decimal = [0_u8; 39];
    result.extend_from_slice(unsigned_decimal(magnitude, &mut decimal));
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_float_hex_vectors_are_exact() {
        assert_eq!(hex(0.001), b"0x1.0624dd2f1a9fcp-10");
        assert_eq!(hex(1.0), b"0x1.0000000000000p+0");
        assert_eq!(hex(-1.0), b"-0x1.0000000000000p+0");
        assert_eq!(hex(0.0), b"0x0.0p+0");
        assert_eq!(hex(-0.0), b"-0x0.0p+0");
        assert_eq!(hex(f64::from_bits(1)), b"0x0.0000000000001p-1022");
        assert_eq!(
            hex(f64::from_bits(0x000f_ffff_ffff_ffff)),
            b"0x0.fffffffffffffp-1022"
        );
        assert_eq!(hex(f64::MIN_POSITIVE), b"0x1.0000000000000p-1022");
        assert_eq!(hex(f64::MAX), b"0x1.fffffffffffffp+1023");
        assert!(python_float_hex(f64::NAN).is_err());
        assert!(python_float_hex(f64::INFINITY).is_err());
        assert!(python_float_hex(f64::NEG_INFINITY).is_err());
    }

    #[test]
    fn tagged_scalars_match_the_frozen_python_grammar() {
        assert_eq!(scalar_none().expect("none"), b"n");
        assert_eq!(scalar_bool(true).expect("bool"), b"b1");
        assert_eq!(scalar_i64(-7).expect("integer"), b"i-7;");
        assert_eq!(
            scalar_i64(i64::MIN).expect("minimum integer"),
            b"i-9223372036854775808;"
        );
        assert_eq!(scalar_str("owl").expect("string"), b"s3:owl");
        assert_eq!(
            scalar_sequence(&[scalar_u64(1).expect("one"), scalar_none().expect("none")])
                .expect("sequence"),
            b"q8:3:i1;1:n"
        );
    }

    #[test]
    fn arbitrary_positive_integers_and_deadline_kinds_are_not_narrowed() {
        let beyond_u64 =
            PositiveIntegerV1::from_decimal("18446744073709551616").expect("canonical integer");
        assert_eq!(
            scalar_positive(&beyond_u64).expect("wide integer"),
            b"i18446744073709551616;"
        );

        let integer_limits = ParseLimitsV1 {
            deadline_seconds: Some(DeadlineSecondsV1::Integer(beyond_u64.clone())),
            ..ParseLimitsV1::default()
        };
        let integer_encoding = parse_limits_scalar(&integer_limits).expect("integer deadline");
        assert!(contains(&integer_encoding, b"i18446744073709551616;"));

        let float_limits = ParseLimitsV1 {
            deadline_seconds: Some(DeadlineSecondsV1::Float(18_446_744_073_709_551_616.0)),
            ..ParseLimitsV1::default()
        };
        let float_encoding = parse_limits_scalar(&float_limits).expect("float deadline");
        assert!(contains(&float_encoding, b"f0x1.0000000000000p+64;"));
        assert_ne!(integer_encoding, float_encoding);

        let fingerprint = FingerprintV1 {
            schema: beyond_u64,
            digest: [0; 32],
        };
        assert!(contains(
            &scalar_fingerprint(&fingerprint).expect("wide fingerprint schema"),
            b"i18446744073709551616;"
        ));
    }

    #[test]
    fn generated_fixture_matches_the_published_python_v1_vectors() {
        let publication = super::super::fixture::publication().expect("generated fixture");
        let storage = publication.storage();

        let options = load_options_bytes(&storage.load_options).expect("load options bytes");
        assert_eq!(options.len(), 524);
        assert_eq!(
            sha256(&options),
            digest("f12b10bc025c91a63954690b5811f03e9aeff5c1793891e7e9bb1e0d131d807b")
        );
        assert_eq!(
            load_options_digest(&storage.load_options).expect("load options digest"),
            digest("f12b10bc025c91a63954690b5811f03e9aeff5c1793891e7e9bb1e0d131d807b")
        );

        let report = report_bytes(&storage.report).expect("report bytes");
        assert_eq!(report.len(), 362);
        assert_eq!(
            sha256(&report),
            digest("92d62c4e495e8406b6a0a0a8ee2c56cbb6ed762c89f79c8a62da294c480934a5")
        );
        assert_eq!(
            report_digest(&storage.report).expect("report digest"),
            digest("92d62c4e495e8406b6a0a0a8ee2c56cbb6ed762c89f79c8a62da294c480934a5")
        );

        let diagnostic = diagnostic_bytes(&storage.diagnostics[0]).expect("diagnostic bytes");
        assert_eq!(diagnostic.len(), 159);
        assert_eq!(
            sha256(&diagnostic),
            digest("54e369810b25113f6d7b332dbfd85d356605219f34f35ce5b26a570b1d3a0969")
        );

        let diagnostics = diagnostics_bytes(
            &storage.diagnostics,
            &storage.documents,
            &storage.import_manifest,
        )
        .expect("diagnostics bytes");
        assert_eq!(diagnostics.len(), 459);
        assert_eq!(
            sha256(&diagnostics),
            digest("e3abd529990ad1d2aa33c416855b9991975ee97d0b4aa5b139acedb84e6a5e7e")
        );
        assert_eq!(
            diagnostics_digest(
                &storage.diagnostics,
                &storage.documents,
                &storage.import_manifest,
            )
            .expect("diagnostics digest"),
            digest("e3abd529990ad1d2aa33c416855b9991975ee97d0b4aa5b139acedb84e6a5e7e")
        );

        let attestation = attestation_bytes(storage.attestation()).expect("attestation bytes");
        assert_eq!(attestation.len(), 555);
        assert_eq!(
            sha256(&attestation),
            digest("97e02d37406dfcc065723c621969ea7377c1def9e6919c2a8dc6e1b957c40616")
        );
        assert_eq!(
            attestation_digest(storage.attestation()).expect("attestation digest"),
            digest("97e02d37406dfcc065723c621969ea7377c1def9e6919c2a8dc6e1b957c40616")
        );
    }

    fn hex(value: f64) -> Vec<u8> {
        python_float_hex(value).expect("finite float")
    }

    fn contains(haystack: &[u8], needle: &[u8]) -> bool {
        haystack
            .windows(needle.len())
            .any(|window| window == needle)
    }

    fn digest(value: &str) -> [u8; 32] {
        assert_eq!(value.len(), 64);
        let mut result = [0_u8; 32];
        for (index, pair) in value.as_bytes().chunks_exact(2).enumerate() {
            result[index] = (nibble(pair[0]) << 4) | nibble(pair[1]);
        }
        result
    }

    fn nibble(value: u8) -> u8 {
        match value {
            b'0'..=b'9' => value - b'0',
            b'a'..=b'f' => value - b'a' + 10,
            _ => panic!("fixture digest is not lowercase hexadecimal"),
        }
    }
}
