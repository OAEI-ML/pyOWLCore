use std::collections::BTreeMap;

use pyowl_native::comparator::{
    ComparatorCommonEvidence, ComparatorFingerprintEvidence, ComparatorRecordInventory,
};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};

const COMMON_CONTRACT_SCHEMA: &str = "pyowl-core/comparator-common-contract/v1";
const RECORD_INVENTORY_DOMAIN: &[u8] = b"pyowl-core:comparator-record-inventory:v1\0";
const DOCUMENT_INVENTORY_DOMAIN: &[u8] = b"pyowl-core:comparator-document-inventory:v1\0";
const RESOLVER_DOMAIN: &[u8] = b"pyowl-core:resolver-configuration:v1\0";

#[derive(Debug)]
pub(crate) struct ContractError(String);

impl ContractError {
    fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

impl std::fmt::Display for ContractError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for ContractError {}

pub(crate) fn build_contract(
    evidence: &ComparatorCommonEvidence,
    corpus_id: &str,
    source_sha256: &str,
    options_sha256: &str,
    format: &str,
) -> Result<Value, ContractError> {
    if corpus_id.is_empty()
        || !is_sha256(source_sha256)
        || !is_sha256(options_sha256)
        || !matches!(format, "functional" | "rdfxml")
    {
        return Err(ContractError::new("direct contract identity is invalid"));
    }
    if hex(evidence.source_sha256) != source_sha256 {
        return Err(ContractError::new(
            "native retained source digest differs from the request",
        ));
    }
    let identity = identity(evidence, source_sha256, format);
    let provenance = provenance(evidence, format)?;
    let diagnostics = Value::Array(Vec::new());
    let identity_bytes = canonical_json(&identity)?;
    let provenance_bytes = canonical_json(&provenance)?;
    let diagnostics_bytes = canonical_json(&diagnostics)?;
    let inventories = json!({
        "ontology_annotations": inventory(evidence.inventories[0]),
        "axioms": inventory(evidence.inventories[1]),
        "extensions": inventory(evidence.inventories[2]),
        "signature": inventory(evidence.inventories[3]),
        "documents": document_inventory(evidence)?,
    });
    let fingerprints = json!({
        "document": fingerprint(evidence.fingerprints[0]),
        "structural": fingerprint(evidence.fingerprints[1]),
        "logical": fingerprint(evidence.fingerprints[2]),
        "signature": fingerprint(evidence.fingerprints[3]),
    });
    let ledger = json!({
        "inventories": inventories,
        "identity_sha256": hex_digest(&identity_bytes),
        "identity_bytes": identity_bytes.len(),
        "provenance_sha256": hex_digest(&provenance_bytes),
        "provenance_bytes": provenance_bytes.len(),
        "diagnostics_sha256": hex_digest(&diagnostics_bytes),
        "diagnostics_bytes": diagnostics_bytes.len(),
        "diagnostic_count": 0,
    });
    let mut payload = json!({
        "schema": COMMON_CONTRACT_SCHEMA,
        "model_schema": 1,
        "corpus_id": corpus_id,
        "source_sha256": source_sha256,
        "options_sha256": options_sha256,
        "complete_import_closure": true,
        "root_document_key": evidence.document_key,
        "identity": identity,
        "provenance": provenance,
        "diagnostics": diagnostics,
        "fingerprints": fingerprints,
        "ledger": ledger,
    });
    let digest = hex_digest(&canonical_json(&payload)?);
    payload
        .as_object_mut()
        .ok_or_else(|| ContractError::new("direct contract payload is not an object"))?
        .insert("contract_sha256".to_owned(), Value::String(digest));
    Ok(payload)
}

pub(crate) fn validate_contract(value: &Value) -> Result<(), ContractError> {
    let object = value
        .as_object()
        .ok_or_else(|| ContractError::new("common contract is not an object"))?;
    let expected_fields = [
        "schema",
        "model_schema",
        "corpus_id",
        "source_sha256",
        "options_sha256",
        "complete_import_closure",
        "root_document_key",
        "identity",
        "provenance",
        "diagnostics",
        "fingerprints",
        "ledger",
        "contract_sha256",
    ];
    if object.len() != expected_fields.len()
        || expected_fields
            .iter()
            .any(|name| !object.contains_key(*name))
    {
        return Err(ContractError::new("common contract fields differ"));
    }
    if object.get("schema").and_then(Value::as_str) != Some(COMMON_CONTRACT_SCHEMA)
        || object.get("model_schema").and_then(Value::as_u64) != Some(1)
        || object
            .get("complete_import_closure")
            .and_then(Value::as_bool)
            != Some(true)
    {
        return Err(ContractError::new("common contract scalar policy differs"));
    }
    for name in ["corpus_id", "root_document_key"] {
        if object
            .get(name)
            .and_then(Value::as_str)
            .is_none_or(str::is_empty)
        {
            return Err(ContractError::new(format!(
                "common contract {name} is invalid"
            )));
        }
    }
    for name in ["source_sha256", "options_sha256", "contract_sha256"] {
        if !object
            .get(name)
            .and_then(Value::as_str)
            .is_some_and(is_sha256)
        {
            return Err(ContractError::new(format!(
                "common contract {name} is invalid"
            )));
        }
    }
    validate_fingerprints(object.get("fingerprints"))?;
    let ledger = exact_object(object.get("ledger"), "common contract ledger")?;
    let expected_ledger = [
        "inventories",
        "identity_sha256",
        "identity_bytes",
        "provenance_sha256",
        "provenance_bytes",
        "diagnostics_sha256",
        "diagnostics_bytes",
        "diagnostic_count",
    ];
    exact_fields(ledger, &expected_ledger, "common contract ledger")?;
    validate_inventories(ledger.get("inventories"))?;
    for (name, payload_name) in [
        ("identity", "identity"),
        ("provenance", "provenance"),
        ("diagnostics", "diagnostics"),
    ] {
        let encoded = canonical_json(
            object
                .get(payload_name)
                .ok_or_else(|| ContractError::new("common contract payload is missing"))?,
        )?;
        if ledger
            .get(&format!("{name}_sha256"))
            .and_then(Value::as_str)
            != Some(hex_digest(&encoded).as_str())
            || ledger.get(&format!("{name}_bytes")).and_then(Value::as_u64)
                != u64::try_from(encoded.len()).ok()
        {
            return Err(ContractError::new(format!(
                "common contract {name} ledger differs"
            )));
        }
    }
    let diagnostics = object
        .get("diagnostics")
        .and_then(Value::as_array)
        .ok_or_else(|| ContractError::new("common contract diagnostics are invalid"))?;
    if ledger.get("diagnostic_count").and_then(Value::as_u64)
        != u64::try_from(diagnostics.len()).ok()
    {
        return Err(ContractError::new(
            "common contract diagnostic count differs",
        ));
    }
    validate_identity(object.get("identity"), object.get("root_document_key"))?;
    validate_provenance(object.get("provenance"))?;

    let observed = object
        .get("contract_sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| ContractError::new("common contract digest is missing"))?;
    let mut unsigned = object.clone();
    unsigned.remove("contract_sha256");
    if observed != hex_digest(&canonical_json(&Value::Object(unsigned))?) {
        return Err(ContractError::new("common contract digest differs"));
    }
    Ok(())
}

fn identity(evidence: &ComparatorCommonEvidence, source_sha256: &str, format: &str) -> Value {
    let resolver = resolver_configuration_sha256();
    json!({
        "documents": [{
            "document_key": evidence.document_key,
            "document_iri": hex(&evidence.document_iri),
            "ontology_iri": evidence.ontology_iri.as_ref().map(hex),
            "version_iri": evidence.version_iri.as_ref().map(hex),
            "source_sha256": source_sha256,
            "document_fingerprint": hex(evidence.fingerprints[0].sha256),
            "format": format,
            "status": "root",
        }],
        "imports": [],
        "import_policy": "record_unresolved",
        "offline": true,
        "resolver_configuration_sha256": hex(resolver),
        "root_document_key": evidence.document_key,
    })
}

fn provenance(evidence: &ComparatorCommonEvidence, format: &str) -> Result<Value, ContractError> {
    let mut grouped: BTreeMap<[u8; 32], Vec<Value>> = BTreeMap::new();
    let mut origin_rows = evidence.origins.iter().collect::<Vec<_>>();
    let canonical_rdf_ordinals = format == "rdfxml";
    if canonical_rdf_ordinals {
        // The direct comparator is import-free, so every row has the same
        // document key. The producer ordinal is only a duplicate tie-break.
        origin_rows.sort_by_key(|origin| (origin.structural_sha256, origin.occurrence));
    }
    for (index, origin) in origin_rows.into_iter().enumerate() {
        let occurrence = if canonical_rdf_ordinals {
            u64::try_from(index)
                .map_err(|_| ContractError::new("RDF provenance exceeds u64 ordinals"))?
        } else {
            origin.occurrence
        };
        grouped
            .entry(origin.structural_sha256)
            .or_default()
            .push(json!({
                "document_key": evidence.document_key,
                "occurrence": occurrence,
                "span": null,
            }));
    }
    let origins = grouped
        .into_iter()
        .map(|(digest, occurrences)| {
            json!({
                "structural_sha256": hex(digest),
                "occurrences": occurrences,
            })
        })
        .collect::<Vec<_>>();
    let origin_entry_count = origins.len();
    Ok(json!({
        "origins": origins,
        "origin_entry_count": origin_entry_count,
        "source_byte_count": evidence.source_byte_count,
        "document_count": 1,
    }))
}

fn fingerprint(value: ComparatorFingerprintEvidence) -> Value {
    let digest = hex(value.sha256);
    json!({
        "algorithm": "sha256",
        "schema": 1,
        "preimage_bytes": value.preimage_bytes,
        "preimage_sha256": digest,
        "digest": digest,
    })
}

fn inventory(value: ComparatorRecordInventory) -> Value {
    json!({
        "count": value.count,
        "canonical_bytes": value.canonical_bytes,
        "transcript_bytes": value.transcript_bytes,
        "sha256": hex(value.sha256),
    })
}

fn document_inventory(evidence: &ComparatorCommonEvidence) -> Result<Value, ContractError> {
    let mut row = Vec::new();
    frame_varint(&mut row, evidence.document_key.as_bytes())?;
    row.extend_from_slice(&evidence.source_sha256);
    row.extend_from_slice(&evidence.fingerprints[0].sha256);
    let mut transcript = Vec::new();
    transcript.extend_from_slice(DOCUMENT_INVENTORY_DOMAIN);
    varint(&mut transcript, 1);
    transcript.extend_from_slice(&row);
    Ok(json!({
        "count": 1,
        "canonical_bytes": row.len(),
        "transcript_bytes": transcript.len(),
        "sha256": hex_digest(&transcript),
    }))
}

fn resolver_configuration_sha256() -> [u8; 32] {
    let mut payload = Vec::new();
    payload.extend_from_slice(RESOLVER_DOMAIN);
    varint(&mut payload, 4);
    payload.extend_from_slice(b"none");
    sha256(&payload)
}

fn validate_fingerprints(value: Option<&Value>) -> Result<(), ContractError> {
    let fingerprints = exact_object(value, "common contract fingerprints")?;
    exact_fields(
        fingerprints,
        &["document", "structural", "logical", "signature"],
        "common contract fingerprints",
    )?;
    for (name, raw) in fingerprints {
        let row = exact_object(Some(raw), "common contract fingerprint")?;
        exact_fields(
            row,
            &[
                "algorithm",
                "schema",
                "preimage_bytes",
                "preimage_sha256",
                "digest",
            ],
            "common contract fingerprint",
        )?;
        if row.get("algorithm").and_then(Value::as_str) != Some("sha256")
            || row.get("schema").and_then(Value::as_u64) != Some(1)
            || row.get("preimage_bytes").and_then(Value::as_u64) == Some(0)
            || !row
                .get("preimage_sha256")
                .and_then(Value::as_str)
                .is_some_and(is_sha256)
            || row.get("preimage_sha256") != row.get("digest")
        {
            return Err(ContractError::new(format!(
                "common contract {name} fingerprint differs"
            )));
        }
    }
    Ok(())
}

fn validate_inventories(value: Option<&Value>) -> Result<(), ContractError> {
    let inventories = exact_object(value, "common contract inventories")?;
    exact_fields(
        inventories,
        &[
            "ontology_annotations",
            "axioms",
            "extensions",
            "signature",
            "documents",
        ],
        "common contract inventories",
    )?;
    for (name, raw) in inventories {
        let row = exact_object(Some(raw), "common contract inventory")?;
        exact_fields(
            row,
            &["count", "canonical_bytes", "transcript_bytes", "sha256"],
            "common contract inventory",
        )?;
        let count = row.get("count").and_then(Value::as_u64);
        let canonical_bytes = row.get("canonical_bytes").and_then(Value::as_u64);
        let transcript_bytes = row.get("transcript_bytes").and_then(Value::as_u64);
        if count.is_none()
            || canonical_bytes.is_none()
            || transcript_bytes.is_none()
            || !row
                .get("sha256")
                .and_then(Value::as_str)
                .is_some_and(is_sha256)
            || (count == Some(0)) != (canonical_bytes == Some(0))
        {
            return Err(ContractError::new(format!(
                "common contract {name} inventory differs"
            )));
        }
        if name != "documents" {
            let minimum = u64::try_from(RECORD_INVENTORY_DOMAIN.len())
                .ok()
                .and_then(|value| value.checked_add(varint_len(count.unwrap_or_default())))
                .and_then(|value| value.checked_add(count.unwrap_or_default()))
                .and_then(|value| value.checked_add(canonical_bytes.unwrap_or_default()));
            if transcript_bytes < minimum {
                return Err(ContractError::new(format!(
                    "common contract {name} inventory transcript is undersized"
                )));
            }
        }
    }
    Ok(())
}

fn validate_identity(value: Option<&Value>, root: Option<&Value>) -> Result<(), ContractError> {
    let identity = exact_object(value, "common contract identity")?;
    exact_fields(
        identity,
        &[
            "documents",
            "imports",
            "import_policy",
            "offline",
            "resolver_configuration_sha256",
            "root_document_key",
        ],
        "common contract identity",
    )?;
    let documents = identity
        .get("documents")
        .and_then(Value::as_array)
        .ok_or_else(|| ContractError::new("common contract documents are invalid"))?;
    if documents.len() != 1
        || !identity
            .get("imports")
            .and_then(Value::as_array)
            .is_some_and(Vec::is_empty)
        || identity.get("import_policy").and_then(Value::as_str) != Some("record_unresolved")
        || identity.get("offline").and_then(Value::as_bool) != Some(true)
        || identity.get("root_document_key") != root
        || !identity
            .get("resolver_configuration_sha256")
            .and_then(Value::as_str)
            .is_some_and(is_sha256)
    {
        return Err(ContractError::new("common contract identity differs"));
    }
    Ok(())
}

fn validate_provenance(value: Option<&Value>) -> Result<(), ContractError> {
    let provenance = exact_object(value, "common contract provenance")?;
    exact_fields(
        provenance,
        &[
            "origins",
            "origin_entry_count",
            "source_byte_count",
            "document_count",
        ],
        "common contract provenance",
    )?;
    let origins = provenance
        .get("origins")
        .and_then(Value::as_array)
        .ok_or_else(|| ContractError::new("common contract origins are invalid"))?;
    if provenance.get("origin_entry_count").and_then(Value::as_u64)
        != u64::try_from(origins.len()).ok()
        || provenance
            .get("source_byte_count")
            .and_then(Value::as_u64)
            .is_none()
        || provenance.get("document_count").and_then(Value::as_u64) != Some(1)
    {
        return Err(ContractError::new("common contract provenance differs"));
    }
    Ok(())
}

fn exact_object<'a>(
    value: Option<&'a Value>,
    name: &str,
) -> Result<&'a Map<String, Value>, ContractError> {
    value
        .and_then(Value::as_object)
        .ok_or_else(|| ContractError::new(format!("{name} is not an object")))
}

fn exact_fields(
    value: &Map<String, Value>,
    expected: &[&str],
    name: &str,
) -> Result<(), ContractError> {
    if value.len() != expected.len() || expected.iter().any(|key| !value.contains_key(*key)) {
        return Err(ContractError::new(format!("{name} fields differ")));
    }
    Ok(())
}

fn canonical_json(value: &Value) -> Result<Vec<u8>, ContractError> {
    serde_json::to_vec(value)
        .map_err(|_| ContractError::new("common contract could not be canonically serialized"))
}

fn frame_varint(output: &mut Vec<u8>, value: &[u8]) -> Result<(), ContractError> {
    varint(
        output,
        u64::try_from(value.len())
            .map_err(|_| ContractError::new("common contract frame exceeds u64"))?,
    );
    output.extend_from_slice(value);
    Ok(())
}

fn varint(output: &mut Vec<u8>, mut value: u64) {
    loop {
        let byte = (value & 0x7f) as u8;
        value >>= 7;
        output.push(byte | if value == 0 { 0 } else { 0x80 });
        if value == 0 {
            return;
        }
    }
}

fn varint_len(mut value: u64) -> u64 {
    let mut length = 1_u64;
    while value >= 0x80 {
        value >>= 7;
        length += 1;
    }
    length
}

fn sha256(value: &[u8]) -> [u8; 32] {
    Sha256::digest(value).into()
}

fn hex_digest(value: &[u8]) -> String {
    hex(sha256(value))
}

fn hex(value: impl AsRef<[u8]>) -> String {
    use std::fmt::Write;

    value
        .as_ref()
        .iter()
        .fold(String::new(), |mut output, byte| {
            let _ = write!(output, "{byte:02x}");
            output
        })
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(test)]
mod tests {
    use super::*;
    use pyowl_native::comparator::{load_functional_common, load_rdfxml_common};

    #[test]
    fn contract_digest_and_nested_ledgers_are_self_consistent() {
        let source = b"Ontology(Declaration(Class(<https://example.org/C>)))";
        let source_sha256 = hex_digest(source);
        let evidence = load_functional_common(
            source,
            &format!("urn:pyowl-core:comparator-source:sha256:{source_sha256}"),
        )
        .expect("retained evidence");
        let contract = build_contract(
            &evidence,
            "fixture",
            &source_sha256,
            &"a".repeat(64),
            "functional",
        )
        .expect("contract");

        validate_contract(&contract).expect("valid contract");
        let mut tampered = contract;
        tampered["ledger"]["provenance_bytes"] = json!(0);
        assert!(validate_contract(&tampered).is_err());
    }

    #[test]
    fn rdf_provenance_ordinals_are_canonical_and_producer_order_independent() {
        let source = br#"<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
            xmlns:owl="http://www.w3.org/2002/07/owl#">
          <owl:Ontology rdf:about="https://example.org/o"/>
          <owl:Class rdf:about="https://example.org/A"/>
          <owl:Class rdf:about="https://example.org/B"/>
        </rdf:RDF>"#;
        let source_sha256 = hex_digest(source);
        let mut evidence = load_rdfxml_common(
            source,
            &format!("urn:pyowl-core:comparator-source:sha256:{source_sha256}"),
        )
        .expect("retained RDF/XML evidence");
        for (index, origin) in evidence.origins.iter_mut().enumerate() {
            origin.occurrence = 100 - u64::try_from(index).expect("test ordinal");
        }
        let mut reversed = evidence.clone();
        reversed.origins.reverse();

        let canonical = provenance(&evidence, "rdfxml").expect("canonical provenance");
        let reversed_canonical =
            provenance(&reversed, "rdfxml").expect("reversed canonical provenance");

        assert_eq!(canonical, reversed_canonical);
        assert_eq!(
            published_occurrences(&canonical),
            (0..u64::try_from(evidence.origins.len()).expect("origin count")).collect::<Vec<_>>()
        );
    }

    #[test]
    fn functional_provenance_preserves_source_ordinals() {
        let source = b"Ontology(Declaration(Class(<https://example.org/C>)))";
        let source_sha256 = hex_digest(source);
        let mut evidence = load_functional_common(
            source,
            &format!("urn:pyowl-core:comparator-source:sha256:{source_sha256}"),
        )
        .expect("retained evidence");
        evidence.origins[0].occurrence = 41;

        let value = provenance(&evidence, "functional").expect("functional provenance");

        assert_eq!(published_occurrences(&value), vec![41]);
    }

    fn published_occurrences(provenance: &Value) -> Vec<u64> {
        provenance["origins"]
            .as_array()
            .expect("origin groups")
            .iter()
            .flat_map(|origin| {
                origin["occurrences"]
                    .as_array()
                    .expect("occurrences")
                    .iter()
            })
            .map(|occurrence| {
                occurrence["occurrence"]
                    .as_u64()
                    .expect("published u64 occurrence")
            })
            .collect()
    }
}
