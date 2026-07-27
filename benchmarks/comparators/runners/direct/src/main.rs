#![forbid(unsafe_op_in_unsafe_fn)]

mod contract;

use std::fs::{File, OpenOptions};
use std::io::{self, BufReader, Read, Write};
use std::panic::{self, AssertUnwindSafe};
use std::path::PathBuf;
use std::process;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use base64::Engine;
use pyowl_native::comparator::{
    load_functional_common, load_rdfxml_common, ComparatorFailureKind, ComparatorPhaseEvidence,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const LANE: &str = "pyowl-direct-rust-common";
const IMPLEMENTATION: &str = "pyowl-core-direct-rust";
const BOUNDARY: &str = "common-contract-ready";
const ENGINE_VERSION: &str = "0.1.0.dev0";
const ENGINE_REVISION: &str = "Cargo.lock SHA-256 plus exact Git revision captured in every report";
const ENGINE_ARTIFACT: &str = "release direct-engine runner built from native/Cargo.lock";
const ALLOCATOR: &str = "Rust system allocator";
const THREAD_CEILING: u64 = 1;
const RUNNER_REVISION: &str = "pyowl-core-direct-rust-common-runner-v2";
const FEATURES: &[&str] = &["direct-rust-engine", "common-contract-v1"];

const ADAPTER_REQUEST_SCHEMA: &str = "pyowl-core/comparator-adapter-request/v2";
const ADAPTER_RESULT_SCHEMA: &str = "pyowl-core/comparator-adapter-result/v1";
const TIMED_VALIDATION_SCHEMA: &str = "pyowl-core/comparator-timed-validation/v1";
const PERSISTENT_PROTOCOL_SCHEMA: &str = "pyowl-core/comparator-persistent-runner/v1";
const PERSISTENT_HANDSHAKE_SCHEMA: &str = "pyowl-core/comparator-persistent-handshake/v1";
const PERSISTENT_REQUEST_SCHEMA: &str = "pyowl-core/comparator-persistent-request/v1";
const PERSISTENT_RESPONSE_SCHEMA: &str = "pyowl-core/comparator-persistent-response/v1";
const PERSISTENT_SHUTDOWN_SCHEMA: &str = "pyowl-core/comparator-persistent-shutdown/v1";
const PERSISTENT_SHUTDOWN_ACK_SCHEMA: &str = "pyowl-core/comparator-persistent-shutdown-ack/v1";
const DOCUMENT_IRI_PREFIX: &str = "urn:pyowl-core:comparator-source:sha256:";
const MAX_REQUEST_BYTES: usize = 512 * 1024 * 1024;
const MAX_FRAME_HEADER_BYTES: usize = 32;
const MAX_REASON_CHARS: usize = 1_000;
const NATIVE_CARGO_LOCK: &[u8] = include_bytes!("../../../../../native/Cargo.lock");
const NATIVE_LOCK_STANZA: &str = concat!(
    "[[package]]\n",
    "name = \"pyowl-core-native\"\n",
    "version = \"0.1.0-dev.0\"\n",
);

static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

#[derive(Debug)]
struct RunnerError(String);

impl RunnerError {
    fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}

impl std::fmt::Display for RunnerError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(&self.0)
    }
}

impl std::error::Error for RunnerError {}

impl From<io::Error> for RunnerError {
    fn from(error: io::Error) -> Self {
        Self::new(format!("I/O error: {error}"))
    }
}

impl From<contract::ContractError> for RunnerError {
    fn from(error: contract::ContractError) -> Self {
        Self::new(error.to_string())
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct AdapterRequest {
    schema: String,
    lane: String,
    implementation: String,
    boundary: String,
    corpus_id: String,
    source_b64: String,
    source_sha256: String,
    document_iri: String,
    format: String,
    options_sha256: String,
    options: Options,
    input_mode: String,
    process_mode: String,
    expected_artifact_sha256: Option<String>,
    expected_features: Vec<String>,
    expected_allocator: String,
    expected_thread_ceiling: u64,
    expected_runner_revision: String,
    expected_runner_sha256: String,
}

#[derive(Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct Options {
    format: String,
    imports: String,
    offline: bool,
    preserve_source_map: bool,
    collect_provenance: bool,
    validate_owl2_dl: bool,
    deterministic: bool,
    limits: Limits,
}

#[derive(Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
struct Limits {
    cancellation_check_interval: u64,
    deadline_seconds: Option<f64>,
    max_annotations: u64,
    max_axioms: u64,
    max_canonical_work: u64,
    max_catalog_rewrites: u64,
    max_composite_members: u64,
    max_concurrent_fetches: u64,
    max_decompressed_bytes: u64,
    max_delta_entries: u64,
    max_diagnostics: u64,
    max_disk_cache_bytes: u64,
    max_documents: u64,
    max_import_depth: u64,
    max_index_bytes: u64,
    max_index_rows: u64,
    max_iri_bytes: u64,
    max_literal_bytes: u64,
    max_memory_bytes: Option<u64>,
    max_nesting_depth: u64,
    max_origin_entries: u64,
    max_overlay_depth: u64,
    max_prefixes: u64,
    max_rdf_list_length: u64,
    max_redirects: u64,
    max_resolver_attempts: u64,
    max_rule_atoms: u64,
    max_sequence_arity: u64,
    max_source_bytes: u64,
    max_source_map_entries: u64,
    max_strings: u64,
    max_temporary_bytes: u64,
    max_terms: u64,
    max_total_source_bytes: u64,
    max_triples: u64,
    max_wire_bytes: u64,
    max_wire_rows: u64,
}

#[derive(Debug)]
struct ValidatedRequest {
    corpus_id: String,
    source: Vec<u8>,
    source_sha256: String,
    document_iri: String,
    format: String,
    options_sha256: String,
    input_mode: String,
    process_mode: String,
}

#[derive(Debug, Serialize)]
struct Artifact {
    pin_state: &'static str,
    version: &'static str,
    revision: &'static str,
    artifact: &'static str,
    artifact_sha256: String,
    features: Vec<&'static str>,
    allocator: &'static str,
    thread_ceiling: u64,
    runner_revision: &'static str,
    runner_sha256: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PersistentRequest {
    schema: String,
    protocol: String,
    sequence: u64,
    request: AdapterRequest,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct PersistentShutdown {
    schema: String,
    protocol: String,
    sequence: u64,
}

struct TempInput {
    path: PathBuf,
}

impl Drop for TempInput {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.path);
    }
}

fn expected_limits() -> Limits {
    Limits {
        cancellation_check_interval: 4096,
        deadline_seconds: None,
        max_annotations: 100_000_000,
        max_axioms: 100_000_000,
        max_canonical_work: 1_000_000_000,
        max_catalog_rewrites: 128,
        max_composite_members: 1024,
        max_concurrent_fetches: 8,
        max_decompressed_bytes: 8_589_934_592,
        max_delta_entries: 10_000_000,
        max_diagnostics: 10_000,
        max_disk_cache_bytes: 68_719_476_736,
        max_documents: 1000,
        max_import_depth: 128,
        max_index_bytes: 17_179_869_184,
        max_index_rows: 500_000_000,
        max_iri_bytes: 1_048_576,
        max_literal_bytes: 67_108_864,
        max_memory_bytes: None,
        max_nesting_depth: 512,
        max_origin_entries: 100_000_000,
        max_overlay_depth: 32,
        max_prefixes: 1_000_000,
        max_rdf_list_length: 10_000_000,
        max_redirects: 5,
        max_resolver_attempts: 10_000,
        max_rule_atoms: 10_000_000,
        max_sequence_arity: 10_000_000,
        max_source_bytes: 2_147_483_648,
        max_source_map_entries: 100_000_000,
        max_strings: 500_000_000,
        max_temporary_bytes: 17_179_869_184,
        max_terms: 500_000_000,
        max_total_source_bytes: 8_589_934_592,
        max_triples: 100_000_000,
        max_wire_bytes: 17_179_869_184,
        max_wire_rows: 500_000_000,
    }
}

fn expected_options(format: &str) -> Options {
    Options {
        format: format.to_owned(),
        imports: "record_unresolved".to_owned(),
        offline: true,
        preserve_source_map: false,
        collect_provenance: true,
        validate_owl2_dl: false,
        deterministic: true,
        limits: expected_limits(),
    }
}

fn expected_options_sha256(format: &str) -> Option<&'static str> {
    match format {
        "functional" => Some("a68176678f9e39941cd6258b3b7181355afbbf751c89e43cc69e516aed82d24c"),
        "owlxml" => Some("a24b7713aa79cad899ffe819abc25ac9e53f8b9657b2e22507b1745073a8253e"),
        "rdfxml" => Some("fdfc954b7b8f0253c8e90ee4542170f506ca069ac6bd93744ac0ceabf04f8d2f"),
        "turtle" => Some("6ad540e139870561dc6d37919e52c6534a494441e40a80fad8ab0f2e7a0f169b"),
        _ => None,
    }
}

fn validate_request(
    request: AdapterRequest,
    protocol_mode: &str,
) -> Result<ValidatedRequest, RunnerError> {
    let executable_sha256 = runner_sha256()?;
    for (name, observed, expected) in [
        ("schema", request.schema.as_str(), ADAPTER_REQUEST_SCHEMA),
        ("lane", request.lane.as_str(), LANE),
        (
            "implementation",
            request.implementation.as_str(),
            IMPLEMENTATION,
        ),
        ("boundary", request.boundary.as_str(), BOUNDARY),
        (
            "expected_allocator",
            request.expected_allocator.as_str(),
            ALLOCATOR,
        ),
        (
            "expected_runner_revision",
            request.expected_runner_revision.as_str(),
            RUNNER_REVISION,
        ),
        (
            "expected_runner_sha256",
            request.expected_runner_sha256.as_str(),
            executable_sha256.as_str(),
        ),
    ] {
        if observed != expected {
            return Err(RunnerError::new(format!(
                "adapter request {name} differs from runner pin"
            )));
        }
    }
    if request.expected_artifact_sha256.is_some() {
        return Err(RunnerError::new(
            "runtime-captured direct engine must not receive a fixed artifact pin",
        ));
    }
    if request
        .expected_features
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        != FEATURES
        || request.expected_thread_ceiling != THREAD_CEILING
    {
        return Err(RunnerError::new(
            "adapter request capabilities differ from runner pin",
        ));
    }
    if request.corpus_id.is_empty()
        || !is_sha256(&request.source_sha256)
        || !is_sha256(&request.options_sha256)
    {
        return Err(RunnerError::new("adapter identity is invalid"));
    }
    if request.document_iri != format!("{DOCUMENT_IRI_PREFIX}{}", request.source_sha256) {
        return Err(RunnerError::new(
            "adapter document IRI differs from source identity",
        ));
    }
    if !matches!(request.input_mode.as_str(), "resident-bytes" | "file") {
        return Err(RunnerError::new("adapter input mode is unsupported"));
    }
    let expected_process_mode = match protocol_mode {
        "fresh" => "fresh-process",
        "persistent" => "steady-process",
        _ => return Err(RunnerError::new("runner protocol mode is unsupported")),
    };
    if request.process_mode != expected_process_mode {
        return Err(RunnerError::new(
            "adapter process mode differs from protocol mode",
        ));
    }
    if request.options != expected_options(&request.format) {
        return Err(RunnerError::new(
            "direct Rust runner supports only exact comparator options",
        ));
    }
    if expected_options_sha256(&request.format) != Some(request.options_sha256.as_str()) {
        return Err(RunnerError::new(
            "direct Rust runner format or semantic options differ",
        ));
    }
    let source = base64::engine::general_purpose::STANDARD
        .decode(request.source_b64.as_bytes())
        .map_err(|_| RunnerError::new("adapter source is not strict base64"))?;
    if base64::engine::general_purpose::STANDARD.encode(&source) != request.source_b64
        || sha256_hex(&source) != request.source_sha256
    {
        return Err(RunnerError::new(
            "adapter source differs from canonical pinned bytes",
        ));
    }
    Ok(ValidatedRequest {
        corpus_id: request.corpus_id,
        source,
        source_sha256: request.source_sha256,
        document_iri: request.document_iri,
        format: request.format,
        options_sha256: request.options_sha256,
        input_mode: request.input_mode,
        process_mode: request.process_mode,
    })
}

fn run_request(request: AdapterRequest, protocol_mode: &str) -> Value {
    let fallback = request_identity(&request);
    match panic::catch_unwind(AssertUnwindSafe(|| {
        let validated = validate_request(request, protocol_mode)?;
        execute_common(validated)
    })) {
        Ok(Ok(value)) => value,
        Ok(Err(error)) => fallback_status_result(&fallback, "error", &safe_reason(&error)),
        Err(_) => fallback_status_result(
            &fallback,
            "error",
            "direct retained engine panicked while processing the bounded request",
        ),
    }
}

fn execute_common(request: ValidatedRequest) -> Result<Value, RunnerError> {
    if !matches!(request.format.as_str(), "functional" | "rdfxml") {
        return Ok(status_result(
            &request,
            "ineligible",
            "direct retained engine does not advertise this native syntax",
        ));
    }
    let prepared = if request.input_mode == "file" {
        Some(prepare_file(
            &request.source,
            &request.source_sha256,
            &request.format,
        )?)
    } else {
        None
    };
    let file_temporary_bytes = if prepared.is_some() {
        usize_to_u64(request.source.len(), "source size")?
    } else {
        0
    };
    let rss_before = rss_peak_bytes()?;
    let cpu_before = cpu_time_ns()?;
    let wall_started = Instant::now();
    let load_started = Instant::now();
    let file_source;
    let source = if let Some(input) = &prepared {
        file_source = std::fs::read(&input.path)?;
        file_source.as_slice()
    } else {
        request.source.as_slice()
    };
    let native_started = Instant::now();
    let loaded = match request.format.as_str() {
        "functional" => load_functional_common(source, &request.document_iri),
        "rdfxml" => load_rdfxml_common(source, &request.document_iri),
        _ => unreachable!("unsupported formats returned before timed execution"),
    };
    let evidence = match loaded {
        Ok(value) => value,
        Err(failure) if failure.kind == ComparatorFailureKind::Ineligible => {
            return Ok(status_result(
                &request,
                "ineligible",
                &format!("{}: {}", failure.code, failure.message),
            ))
        }
        Err(failure) => {
            return Err(RunnerError::new(format!(
                "{}: {}",
                failure.code, failure.message
            )))
        }
    };
    let native_ns = elapsed_ns(native_started);
    let load_ns = elapsed_ns(load_started).saturating_sub(evidence.phases.common_prepare_ns);
    let common_started = Instant::now();
    let contract = contract::build_contract(
        &evidence,
        &request.corpus_id,
        &request.source_sha256,
        &request.options_sha256,
        &request.format,
    )?;
    let validation_started = Instant::now();
    contract::validate_contract(&contract)?;
    let validation_ns = elapsed_ns(validation_started);
    let contract_build_validation_ns = elapsed_ns(common_started);
    let common_adapter_ns = checked_add(
        evidence.phases.common_prepare_ns,
        contract_build_validation_ns,
        "common adapter duration",
    )?;
    let contract_sha256 = contract
        .get("contract_sha256")
        .and_then(Value::as_str)
        .ok_or_else(|| RunnerError::new("common contract digest is missing"))?
        .to_owned();
    let temporary_bytes = checked_add(
        file_temporary_bytes,
        evidence.temporary_bytes,
        "temporary byte count",
    )?;
    let rss_after = rss_peak_bytes()?;
    let wall_ns = elapsed_ns(wall_started);
    let cpu_after = cpu_time_ns()?;
    Ok(json!({
        "schema": ADAPTER_RESULT_SCHEMA,
        "lane": LANE,
        "implementation": IMPLEMENTATION,
        "boundary": BOUNDARY,
        "status": "ok",
        "reason": Value::Null,
        "corpus_id": request.corpus_id,
        "source_sha256": request.source_sha256,
        "options_sha256": request.options_sha256,
        "input_mode": request.input_mode,
        "process_mode": request.process_mode,
        "contract": contract,
        "raw_inventory": Value::Null,
        "metrics": {
            "wall_ns": wall_ns,
            "cpu_ns": cpu_after.saturating_sub(cpu_before),
            "load_ns": load_ns,
            "common_adapter_ns": common_adapter_ns,
            "rss_peak_before_bytes": rss_before,
            "rss_peak_after_bytes": rss_after,
            "rss_peak_increment_bytes": rss_after.saturating_sub(rss_before),
            "temporary_bytes": temporary_bytes,
            "object_count": evidence.node_count,
            "phase_ns": phase_metrics(evidence.phases, native_ns, contract_build_validation_ns, validation_ns),
        },
        "timed_validation": {
            "schema": TIMED_VALIDATION_SCHEMA,
            "inside_timed_envelope": true,
            "full_contract_validation": true,
            "contract_sha256": contract_sha256,
            "validation_ns": validation_ns,
        },
        "artifact": artifact()?,
    }))
}

fn phase_metrics(
    phases: ComparatorPhaseEvidence,
    native_ns: u64,
    contract_build_validation_ns: u64,
    validation_ns: u64,
) -> Value {
    json!({
        "syntax_parse": phases.syntax_parse_ns,
        "rdf_to_owl_mapping": phases.rdf_mapping_ns,
        "result_encode": phases.result_encode_ns,
        "arena_construction": phases.arena_construction_ns,
        "freeze": phases.freeze_ns,
        "native_call": native_ns,
        "common_prepare": phases.common_prepare_ns,
        "common_contract": contract_build_validation_ns.saturating_sub(validation_ns),
        "contract_validation": validation_ns,
    })
}

fn prepare_file(
    source: &[u8],
    source_sha256: &str,
    format: &str,
) -> Result<TempInput, RunnerError> {
    let directory = std::env::temp_dir();
    let extension = if format == "rdfxml" { "owl" } else { "ofn" };
    for _ in 0..16 {
        let counter = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let name = format!(
            "pyowl-core-direct-{}-{counter}-{}.{extension}",
            process::id(),
            &source_sha256[..16],
        );
        let path = directory.join(name);
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut stream) => {
                stream.write_all(source)?;
                stream.flush()?;
                return Ok(TempInput { path });
            }
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error.into()),
        }
    }
    Err(RunnerError::new(
        "could not allocate a collision-free prepared input file",
    ))
}

fn request_identity(request: &AdapterRequest) -> Value {
    json!({
        "corpus_id": if request.corpus_id.is_empty() { "invalid-request" } else { &request.corpus_id },
        "source_sha256": if is_sha256(&request.source_sha256) { request.source_sha256.as_str() } else { "0000000000000000000000000000000000000000000000000000000000000000" },
        "options_sha256": if is_sha256(&request.options_sha256) { request.options_sha256.as_str() } else { "0000000000000000000000000000000000000000000000000000000000000000" },
        "input_mode": if matches!(request.input_mode.as_str(), "resident-bytes" | "file") { request.input_mode.as_str() } else { "resident-bytes" },
        "process_mode": if matches!(request.process_mode.as_str(), "fresh-process" | "steady-process") { request.process_mode.as_str() } else { "fresh-process" },
    })
}

fn fallback_identity() -> Value {
    json!({
        "corpus_id": "invalid-request",
        "source_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
        "options_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
        "input_mode": "resident-bytes",
        "process_mode": "fresh-process",
    })
}

fn status_result(request: &ValidatedRequest, status: &str, reason: &str) -> Value {
    let identity = json!({
        "corpus_id": request.corpus_id,
        "source_sha256": request.source_sha256,
        "options_sha256": request.options_sha256,
        "input_mode": request.input_mode,
        "process_mode": request.process_mode,
    });
    fallback_status_result(&identity, status, reason)
}

fn fallback_status_result(identity: &Value, status: &str, reason: &str) -> Value {
    json!({
        "schema": ADAPTER_RESULT_SCHEMA,
        "lane": LANE,
        "implementation": IMPLEMENTATION,
        "boundary": BOUNDARY,
        "status": status,
        "reason": bounded_reason(reason),
        "corpus_id": identity["corpus_id"],
        "source_sha256": identity["source_sha256"],
        "options_sha256": identity["options_sha256"],
        "input_mode": identity["input_mode"],
        "process_mode": identity["process_mode"],
        "contract": Value::Null,
        "raw_inventory": Value::Null,
        "metrics": {},
        "timed_validation": Value::Null,
        "artifact": artifact().unwrap_or_else(|_| json!({
            "pin_state": "runtime-captured",
            "version": ENGINE_VERSION,
            "revision": ENGINE_REVISION,
            "artifact": ENGINE_ARTIFACT,
            "artifact_sha256": sha256_hex(NATIVE_CARGO_LOCK),
            "features": FEATURES,
            "allocator": ALLOCATOR,
            "thread_ceiling": THREAD_CEILING,
            "runner_revision": RUNNER_REVISION,
            "runner_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
        })),
    })
}

fn artifact() -> Result<Value, RunnerError> {
    serde_json::to_value(Artifact {
        pin_state: "runtime-captured",
        version: ENGINE_VERSION,
        revision: ENGINE_REVISION,
        artifact: ENGINE_ARTIFACT,
        artifact_sha256: sha256_hex(NATIVE_CARGO_LOCK),
        features: FEATURES.to_vec(),
        allocator: ALLOCATOR,
        thread_ceiling: THREAD_CEILING,
        runner_revision: RUNNER_REVISION,
        runner_sha256: runner_sha256()?,
    })
    .map_err(|_| RunnerError::new("could not serialize artifact evidence"))
}

fn runner_sha256() -> Result<String, RunnerError> {
    let executable = std::env::current_exe()
        .map_err(|_| RunnerError::new("could not resolve comparator executable"))?;
    let mut stream = File::open(executable)
        .map_err(|_| RunnerError::new("could not open comparator executable"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let count = stream
            .read(&mut buffer)
            .map_err(|_| RunnerError::new("could not hash comparator executable"))?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(hex_digest(hasher.finalize().as_slice()))
}

fn verify_environment() -> Result<String, RunnerError> {
    for (name, expected) in [
        ("PYOWL_CORE_COMPARATOR_LANE", LANE),
        ("PYOWL_CORE_COMPARATOR_IMPLEMENTATION", IMPLEMENTATION),
        ("PYOWL_CORE_COMPARATOR_BOUNDARY", BOUNDARY),
        ("RAYON_NUM_THREADS", "1"),
    ] {
        if std::env::var(name).ok().as_deref() != Some(expected) {
            return Err(RunnerError::new(format!(
                "runner environment {name} differs from pin"
            )));
        }
    }
    let lock = std::str::from_utf8(NATIVE_CARGO_LOCK)
        .map_err(|_| RunnerError::new("embedded native Cargo.lock is not UTF-8"))?;
    if !lock.contains(NATIVE_LOCK_STANZA) {
        return Err(RunnerError::new(
            "embedded Cargo.lock differs from the native engine pin",
        ));
    }
    let protocol_mode = std::env::var("PYOWL_CORE_COMPARATOR_PROTOCOL_MODE")
        .map_err(|_| RunnerError::new("runner protocol mode is missing"))?;
    if !matches!(protocol_mode.as_str(), "fresh" | "persistent") {
        return Err(RunnerError::new("runner protocol mode is unsupported"));
    }
    runner_sha256()?;
    Ok(protocol_mode)
}

fn fresh_main() -> Result<(), RunnerError> {
    let mut body = Vec::new();
    io::stdin()
        .lock()
        .take((MAX_REQUEST_BYTES + 1) as u64)
        .read_to_end(&mut body)?;
    let result = if body.len() > MAX_REQUEST_BYTES {
        fallback_status_result(
            &fallback_identity(),
            "error",
            "adapter request exceeds size limit",
        )
    } else {
        match serde_json::from_slice::<AdapterRequest>(&body) {
            Ok(request) => run_request(request, "fresh"),
            Err(_) => fallback_status_result(
                &fallback_identity(),
                "error",
                "adapter request is not valid strict schema-v2 JSON",
            ),
        }
    };
    let payload = serde_json::to_vec(&result)
        .map_err(|_| RunnerError::new("could not serialize adapter result"))?;
    io::stdout().lock().write_all(&payload)?;
    io::stdout().lock().flush()?;
    Ok(())
}

fn persistent_main() -> Result<(), RunnerError> {
    let pid = u64::from(process::id());
    write_frame(&json!({
        "schema": PERSISTENT_HANDSHAKE_SCHEMA,
        "protocol": PERSISTENT_PROTOCOL_SCHEMA,
        "lane": LANE,
        "implementation": IMPLEMENTATION,
        "boundary": BOUNDARY,
        "pid": pid,
        "request_schema": ADAPTER_REQUEST_SCHEMA,
        "result_schema": ADAPTER_RESULT_SCHEMA,
        "fresh_ontology_per_request": true,
        "artifact": artifact()?,
    }))?;
    let mut input = BufReader::new(io::stdin().lock());
    let mut instance_counter = 0_u64;
    let mut expected_sequence = 0_u64;
    loop {
        let payload = read_frame(&mut input)?;
        if let Ok(shutdown) = serde_json::from_slice::<PersistentShutdown>(&payload) {
            if shutdown.schema != PERSISTENT_SHUTDOWN_SCHEMA
                || shutdown.protocol != PERSISTENT_PROTOCOL_SCHEMA
                || shutdown.sequence != expected_sequence
            {
                return Err(RunnerError::new("persistent shutdown protocol differs"));
            }
            write_frame(&json!({
                "schema": PERSISTENT_SHUTDOWN_ACK_SCHEMA,
                "protocol": PERSISTENT_PROTOCOL_SCHEMA,
                "sequence": shutdown.sequence,
                "pid": pid,
            }))?;
            return Ok(());
        }
        let envelope: PersistentRequest = serde_json::from_slice(&payload)
            .map_err(|_| RunnerError::new("persistent request fields differ"))?;
        if envelope.schema != PERSISTENT_REQUEST_SCHEMA
            || envelope.protocol != PERSISTENT_PROTOCOL_SCHEMA
            || envelope.sequence != expected_sequence
        {
            return Err(RunnerError::new("persistent request protocol differs"));
        }
        let result = run_request(envelope.request, "persistent");
        let instance_preimage = format!("{pid}:{instance_counter}:{}", envelope.sequence);
        write_frame(&json!({
            "schema": PERSISTENT_RESPONSE_SCHEMA,
            "protocol": PERSISTENT_PROTOCOL_SCHEMA,
            "sequence": envelope.sequence,
            "ontology_instance_id": sha256_hex(instance_preimage.as_bytes()),
            "result": result,
        }))?;
        instance_counter = instance_counter
            .checked_add(1)
            .ok_or_else(|| RunnerError::new("persistent ontology counter is exhausted"))?;
        expected_sequence = expected_sequence
            .checked_add(1)
            .ok_or_else(|| RunnerError::new("persistent request sequence is exhausted"))?;
    }
}

fn read_frame<R: Read>(input: &mut R) -> Result<Vec<u8>, RunnerError> {
    let mut header = Vec::with_capacity(MAX_FRAME_HEADER_BYTES);
    loop {
        let mut byte = [0_u8; 1];
        input.read_exact(&mut byte).map_err(|error| {
            if error.kind() == io::ErrorKind::UnexpectedEof {
                RunnerError::new("persistent runner stdin closed")
            } else {
                error.into()
            }
        })?;
        header.push(byte[0]);
        if byte[0] == b'\n' {
            break;
        }
        if header.len() >= MAX_FRAME_HEADER_BYTES {
            return Err(RunnerError::new("persistent frame header is invalid"));
        }
    }
    let digits = &header[..header.len() - 1];
    if digits.is_empty()
        || (digits.len() > 1 && digits[0] == b'0')
        || digits.iter().any(|value| !value.is_ascii_digit())
    {
        return Err(RunnerError::new("persistent frame length is noncanonical"));
    }
    let length: usize = std::str::from_utf8(digits)
        .map_err(|_| RunnerError::new("persistent frame length is invalid"))?
        .parse()
        .map_err(|_| RunnerError::new("persistent frame length is invalid"))?;
    if length == 0 || length > MAX_REQUEST_BYTES {
        return Err(RunnerError::new("persistent frame length exceeds limit"));
    }
    let mut payload = vec![0_u8; length];
    input
        .read_exact(&mut payload)
        .map_err(|_| RunnerError::new("persistent frame is truncated"))?;
    let mut newline = [0_u8; 1];
    input
        .read_exact(&mut newline)
        .map_err(|_| RunnerError::new("persistent frame is truncated"))?;
    if newline[0] != b'\n' {
        return Err(RunnerError::new("persistent frame is truncated"));
    }
    Ok(payload)
}

fn write_frame(value: &Value) -> Result<(), RunnerError> {
    let payload = serde_json::to_vec(value)
        .map_err(|_| RunnerError::new("could not serialize persistent frame"))?;
    let mut output = io::stdout().lock();
    writeln!(output, "{}", payload.len())?;
    output.write_all(&payload)?;
    output.write_all(b"\n")?;
    output.flush()?;
    Ok(())
}

fn sha256_hex(payload: &[u8]) -> String {
    hex_digest(Sha256::digest(payload).as_slice())
}

fn hex_digest(payload: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(payload.len() * 2);
    for byte in payload {
        output.push(char::from(HEX[usize::from(byte >> 4)]));
        output.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    output
}

fn is_sha256(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn elapsed_ns(started: Instant) -> u64 {
    u64::try_from(started.elapsed().as_nanos()).unwrap_or(u64::MAX)
}

fn checked_add(left: u64, right: u64, name: &str) -> Result<u64, RunnerError> {
    left.checked_add(right)
        .ok_or_else(|| RunnerError::new(format!("{name} exceeds u64")))
}

fn usize_to_u64(value: usize, name: &str) -> Result<u64, RunnerError> {
    u64::try_from(value).map_err(|_| RunnerError::new(format!("{name} exceeds u64")))
}

#[cfg(unix)]
fn usage() -> Result<libc::rusage, RunnerError> {
    let mut value = std::mem::MaybeUninit::<libc::rusage>::uninit();
    // SAFETY: getrusage initializes the pointed-to rusage on success.
    let status = unsafe { libc::getrusage(libc::RUSAGE_SELF, value.as_mut_ptr()) };
    if status != 0 {
        return Err(RunnerError::new("getrusage failed"));
    }
    // SAFETY: a zero getrusage status guarantees initialization.
    Ok(unsafe { value.assume_init() })
}

#[cfg(unix)]
fn cpu_time_ns() -> Result<u64, RunnerError> {
    let value = usage()?;
    let user_seconds = u64::try_from(value.ru_utime.tv_sec)
        .map_err(|_| RunnerError::new("negative user CPU time"))?;
    let user_micros = u64::try_from(value.ru_utime.tv_usec)
        .map_err(|_| RunnerError::new("negative user CPU time"))?;
    let system_seconds = u64::try_from(value.ru_stime.tv_sec)
        .map_err(|_| RunnerError::new("negative system CPU time"))?;
    let system_micros = u64::try_from(value.ru_stime.tv_usec)
        .map_err(|_| RunnerError::new("negative system CPU time"))?;
    let user = user_seconds
        .checked_mul(1_000_000_000)
        .and_then(|value| value.checked_add(user_micros.saturating_mul(1_000)))
        .ok_or_else(|| RunnerError::new("user CPU time exceeds u64"))?;
    let system = system_seconds
        .checked_mul(1_000_000_000)
        .and_then(|value| value.checked_add(system_micros.saturating_mul(1_000)))
        .ok_or_else(|| RunnerError::new("system CPU time exceeds u64"))?;
    checked_add(user, system, "CPU time")
}

#[cfg(target_os = "macos")]
fn rss_peak_bytes() -> Result<u64, RunnerError> {
    u64::try_from(usage()?.ru_maxrss).map_err(|_| RunnerError::new("negative peak RSS"))
}

#[cfg(all(unix, not(target_os = "macos")))]
fn rss_peak_bytes() -> Result<u64, RunnerError> {
    u64::try_from(usage()?.ru_maxrss)
        .map_err(|_| RunnerError::new("negative peak RSS"))?
        .checked_mul(1024)
        .ok_or_else(|| RunnerError::new("peak RSS exceeds u64"))
}

#[cfg(not(unix))]
fn cpu_time_ns() -> Result<u64, RunnerError> {
    Err(RunnerError::new(
        "the pinned direct Rust runner requires Unix getrusage",
    ))
}

#[cfg(not(unix))]
fn rss_peak_bytes() -> Result<u64, RunnerError> {
    Err(RunnerError::new(
        "the pinned direct Rust runner requires Unix getrusage",
    ))
}

fn bounded_reason(reason: &str) -> String {
    reason
        .chars()
        .filter(|character| !character.is_control())
        .take(MAX_REASON_CHARS)
        .collect()
}

fn safe_reason(error: &RunnerError) -> String {
    let rendered = bounded_reason(&error.to_string());
    if rendered.is_empty() {
        "external comparator failed".to_owned()
    } else {
        rendered
    }
}

fn run() -> Result<(), RunnerError> {
    panic::set_hook(Box::new(|_| {}));
    let protocol_mode = verify_environment()?;
    match protocol_mode.as_str() {
        "fresh" => fresh_main(),
        "persistent" => persistent_main(),
        _ => Err(RunnerError::new("runner protocol mode is unsupported")),
    }
}

fn main() {
    if let Err(error) = run() {
        let _ = writeln!(io::stderr().lock(), "{}", safe_reason(&error));
        process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn embedded_lock_binds_the_native_engine() {
        let lock = std::str::from_utf8(NATIVE_CARGO_LOCK).expect("UTF-8 lock");
        assert!(lock.contains(NATIVE_LOCK_STANZA));
        assert_eq!(sha256_hex(NATIVE_CARGO_LOCK).len(), 64);
    }

    #[test]
    fn exact_semantic_options_are_stable() {
        for format in ["functional", "owlxml", "rdfxml", "turtle"] {
            let options = expected_options(format);
            let encoded = serde_json::to_vec(&json!({
                "collect_provenance": options.collect_provenance,
                "deterministic": options.deterministic,
                "format": options.format,
                "imports": options.imports,
                "limits": serde_json::to_value(&options.limits).unwrap_or(Value::Null),
                "offline": options.offline,
                "preserve_source_map": options.preserve_source_map,
                "validate_owl2_dl": options.validate_owl2_dl,
            }))
            .expect("options JSON");
            assert_eq!(
                sha256_hex(&encoded),
                expected_options_sha256(format).unwrap()
            );
        }
    }
}
