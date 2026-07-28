//! Pinned development-only Horned-OWL raw comparator runner.
//!
//! The process implements both the one-shot adapter protocol and the audited
//! persistent lifecycle.  Parsing and all Horned-owned readiness indexes are
//! built inside the measured envelope; transport decoding and prepared-file
//! creation are deliberately outside it.

mod canonical;
mod common;

use std::collections::{BTreeSet, HashMap, HashSet};
use std::fs::{File, OpenOptions};
use std::io::{self, BufRead, BufReader, Cursor, Read, Write};
use std::panic::{self, AssertUnwindSafe};
use std::path::PathBuf;
use std::process;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Instant;

use base64::Engine as _;
use horned_owl::io::{self as horned_io, ParserConfiguration, RDFParserConfiguration};
use horned_owl::model::{
    AnnotationProperty, Class, ComponentKind, DataProperty, Datatype, Kinded, NamedIndividual,
    ObjectProperty, RcStr,
};
use horned_owl::ontology::iri_mapped::RcIRIMappedOntology;
use horned_owl::visitor::immutable::{Visit, Walk};
use oxrdf::{NamedOrBlankNode, Term, Triple};
use oxrdfio::{RdfFormat, RdfParser, RdfSerializer};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const IMPLEMENTATION: &str = "horned-owl";
const ENGINE_VERSION: &str = "1.4.0";
const ENGINE_REVISION: &str = "crates.io horned-owl 1.4.0 (2026-01-09)";
const ENGINE_ARTIFACT: &str = "crates.io horned-owl-1.4.0.crate";
const ENGINE_SHA256: &str = "877f6118b6f5823bb135d04e36fe2c2d3a2b4493feca8ac09b5fa6e91b9fff9e";
const ALLOCATOR: &str = "Rust system allocator";
const THREAD_CEILING: u64 = 1;
const RAW_RUNNER_REVISION: &str = "pyowl-core-horned-raw-runner-v2";
const COMMON_RUNNER_REVISION: &str = "pyowl-core-horned-common-runner-v3";
const RUNNER_FEATURES: &[&str] = &["default", "independent-common-contract-v1"];

const ADAPTER_REQUEST_SCHEMA: &str = "pyowl-core/comparator-adapter-request/v2";
const ADAPTER_RESULT_SCHEMA: &str = "pyowl-core/comparator-adapter-result/v1";
const TIMED_VALIDATION_SCHEMA: &str = "pyowl-core/comparator-timed-validation/v1";
const RAW_INVENTORY_SCHEMA: &str = "pyowl-core/comparator-raw-inventory/v1";
const RAW_INVENTORY_DOMAIN: &[u8] = b"pyowl-core:comparator-raw-inventory:v1\0";
const PERSISTENT_PROTOCOL_SCHEMA: &str = "pyowl-core/comparator-persistent-runner/v1";
const PERSISTENT_HANDSHAKE_SCHEMA: &str = "pyowl-core/comparator-persistent-handshake/v1";
const PERSISTENT_REQUEST_SCHEMA: &str = "pyowl-core/comparator-persistent-request/v1";
const PERSISTENT_RESPONSE_SCHEMA: &str = "pyowl-core/comparator-persistent-response/v1";
const PERSISTENT_SHUTDOWN_SCHEMA: &str = "pyowl-core/comparator-persistent-shutdown/v1";
const PERSISTENT_SHUTDOWN_ACK_SCHEMA: &str = "pyowl-core/comparator-persistent-shutdown-ack/v1";

const MAX_REQUEST_BYTES: usize = 512 * 1024 * 1024;
const MAX_FRAME_HEADER_BYTES: usize = 32;
const MAX_REASON_CHARS: usize = 1_000;
const DOCUMENT_IRI_PREFIX: &str = "urn:pyowl-core:comparator-source:sha256:";
const CARGO_LOCK: &str = include_str!("../Cargo.lock");
const HORNED_LOCK_STANZA: &str = concat!(
    "name = \"horned-owl\"\n",
    "version = \"1.4.0\"\n",
    "source = \"registry+https://github.com/rust-lang/crates.io-index\"\n",
    "checksum = \"877f6118b6f5823bb135d04e36fe2c2d3a2b4493feca8ac09b5fa6e91b9fff9e\"",
);

static TEMP_COUNTER: AtomicU64 = AtomicU64::new(0);

struct FunctionalEdit {
    start: usize,
    end: usize,
    replacement: &'static [u8],
}

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
    expected_artifact_sha256: String,
    expected_features: Vec<String>,
    expected_allocator: String,
    expected_thread_ceiling: u64,
    expected_runner_revision: String,
    expected_runner_sha256: String,
}

#[derive(Debug, Deserialize, PartialEq)]
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

#[derive(Debug, Deserialize, PartialEq)]
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

#[derive(Clone, Copy, Debug)]
enum Format {
    Functional,
    OwlXml,
    RdfXml,
    Turtle,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum Lane {
    Raw,
    Common,
}

impl Lane {
    fn parse(value: &str) -> Result<Self, RunnerError> {
        match value {
            "horned-owl-raw" => Ok(Self::Raw),
            "horned-owl-common" => Ok(Self::Common),
            _ => Err(RunnerError::new("runner lane is unsupported")),
        }
    }

    fn id(self) -> &'static str {
        match self {
            Self::Raw => "horned-owl-raw",
            Self::Common => "horned-owl-common",
        }
    }

    fn boundary(self) -> &'static str {
        match self {
            Self::Raw => "horned-model-ready",
            Self::Common => "common-contract-ready",
        }
    }

    fn features(self) -> &'static [&'static str] {
        RUNNER_FEATURES
    }

    fn runner_revision(self) -> &'static str {
        match self {
            Self::Raw => RAW_RUNNER_REVISION,
            Self::Common => COMMON_RUNNER_REVISION,
        }
    }
}

impl Format {
    fn parse(value: &str) -> Result<Self, RunnerError> {
        match value {
            "functional" => Ok(Self::Functional),
            "owlxml" => Ok(Self::OwlXml),
            "rdfxml" => Ok(Self::RdfXml),
            "turtle" => Ok(Self::Turtle),
            _ => Err(RunnerError::new("adapter format is unsupported")),
        }
    }

    fn extension(self) -> &'static str {
        match self {
            Self::Functional => "ofn",
            Self::OwlXml => "owx",
            Self::RdfXml => "rdf",
            Self::Turtle => "ttl",
        }
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Functional => "functional",
            Self::OwlXml => "owlxml",
            Self::RdfXml => "rdfxml",
            Self::Turtle => "turtle",
        }
    }
}

#[derive(Debug)]
struct ValidatedRequest {
    corpus_id: String,
    source: Vec<u8>,
    source_sha256: String,
    document_iri: String,
    format: Format,
    options_sha256: String,
    input_mode: String,
    process_mode: String,
    max_canonical_work: u64,
    max_terms: u64,
}

#[derive(Debug, Serialize)]
struct Artifact {
    pin_state: &'static str,
    version: &'static str,
    revision: &'static str,
    artifact: &'static str,
    artifact_sha256: &'static str,
    features: Vec<&'static str>,
    allocator: &'static str,
    thread_ceiling: u64,
    runner_revision: &'static str,
    runner_sha256: String,
}

#[derive(Debug, Serialize)]
struct RawInventory {
    schema: &'static str,
    model_kind: &'static str,
    axiom_count: u64,
    annotation_count: u64,
    import_count: u64,
    entity_count: u64,
    diagnostic_count: u64,
    inventory_sha256: String,
}

#[derive(Default)]
struct SignatureInventory {
    entities: BTreeSet<(u8, String)>,
}

impl SignatureInventory {
    fn insert<A: horned_owl::model::ForIRI>(&mut self, kind: u8, iri: &horned_owl::model::IRI<A>) {
        self.entities.insert((kind, iri.to_string()));
    }
}

impl Visit<RcStr> for SignatureInventory {
    fn visit_class(&mut self, value: &Class<RcStr>) {
        self.insert(0, &value.0);
    }

    fn visit_datatype(&mut self, value: &Datatype<RcStr>) {
        self.insert(1, &value.0);
    }

    fn visit_object_property(&mut self, value: &ObjectProperty<RcStr>) {
        self.insert(2, &value.0);
    }

    fn visit_data_property(&mut self, value: &DataProperty<RcStr>) {
        self.insert(3, &value.0);
    }

    fn visit_annotation_property(&mut self, value: &AnnotationProperty<RcStr>) {
        self.insert(4, &value.0);
    }

    fn visit_named_individual(&mut self, value: &NamedIndividual<RcStr>) {
        self.insert(5, &value.0);
    }
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

fn expected_options_sha256(format: Format) -> &'static str {
    match format {
        Format::Functional => "a68176678f9e39941cd6258b3b7181355afbbf751c89e43cc69e516aed82d24c",
        Format::OwlXml => "a24b7713aa79cad899ffe819abc25ac9e53f8b9657b2e22507b1745073a8253e",
        Format::RdfXml => "fdfc954b7b8f0253c8e90ee4542170f506ca069ac6bd93744ac0ceabf04f8d2f",
        Format::Turtle => "6ad540e139870561dc6d37919e52c6534a494441e40a80fad8ab0f2e7a0f169b",
    }
}

fn validate_request(
    request: AdapterRequest,
    protocol_mode: &str,
    lane: Lane,
) -> Result<ValidatedRequest, RunnerError> {
    let runner_sha256 = runner_sha256()?;
    let expected_scalars = [
        ("schema", request.schema.as_str(), ADAPTER_REQUEST_SCHEMA),
        ("lane", request.lane.as_str(), lane.id()),
        (
            "implementation",
            request.implementation.as_str(),
            IMPLEMENTATION,
        ),
        ("boundary", request.boundary.as_str(), lane.boundary()),
        (
            "expected_artifact_sha256",
            request.expected_artifact_sha256.as_str(),
            ENGINE_SHA256,
        ),
        (
            "expected_allocator",
            request.expected_allocator.as_str(),
            ALLOCATOR,
        ),
        (
            "expected_runner_revision",
            request.expected_runner_revision.as_str(),
            lane.runner_revision(),
        ),
        (
            "expected_runner_sha256",
            request.expected_runner_sha256.as_str(),
            runner_sha256.as_str(),
        ),
    ];
    for (name, observed, expected) in expected_scalars {
        if observed != expected {
            return Err(RunnerError::new(format!(
                "adapter request {name} differs from runner pin"
            )));
        }
    }
    if request
        .expected_features
        .iter()
        .map(String::as_str)
        .collect::<Vec<_>>()
        != lane.features()
    {
        return Err(RunnerError::new(
            "adapter request features differ from runner pin",
        ));
    }
    if request.expected_thread_ceiling != THREAD_CEILING {
        return Err(RunnerError::new(
            "adapter request thread ceiling differs from runner pin",
        ));
    }
    if request.corpus_id.is_empty() {
        return Err(RunnerError::new("adapter corpus_id must be nonempty"));
    }
    if !is_sha256(&request.source_sha256) || !is_sha256(&request.options_sha256) {
        return Err(RunnerError::new(
            "adapter digests must be lowercase SHA-256",
        ));
    }
    let expected_document_iri = format!("{DOCUMENT_IRI_PREFIX}{}", request.source_sha256);
    if request.document_iri != expected_document_iri {
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
    let format = Format::parse(&request.format)?;
    if request.options != expected_options(&request.format) {
        return Err(RunnerError::new(
            "Horned runner supports only exact comparator options",
        ));
    }
    if request.options_sha256 != expected_options_sha256(format) {
        return Err(RunnerError::new(
            "adapter options digest differs from semantic options",
        ));
    }
    let source = base64::engine::general_purpose::STANDARD
        .decode(request.source_b64.as_bytes())
        .map_err(|_| RunnerError::new("adapter source is not strict base64"))?;
    if base64::engine::general_purpose::STANDARD.encode(&source) != request.source_b64 {
        return Err(RunnerError::new("adapter source base64 is noncanonical"));
    }
    if sha256_hex(&source) != request.source_sha256 {
        return Err(RunnerError::new(
            "adapter source differs from pinned SHA-256",
        ));
    }
    Ok(ValidatedRequest {
        corpus_id: request.corpus_id,
        source,
        source_sha256: request.source_sha256,
        document_iri: request.document_iri,
        format,
        options_sha256: request.options_sha256,
        input_mode: request.input_mode,
        process_mode: request.process_mode,
        max_canonical_work: request.options.limits.max_canonical_work,
        max_terms: request.options.limits.max_terms,
    })
}

fn run_request(request: AdapterRequest, protocol_mode: &str, lane: Lane) -> Value {
    let fallback = request_identity(&request);
    match panic::catch_unwind(AssertUnwindSafe(|| {
        let validated = validate_request(request, protocol_mode, lane)?;
        if matches!(validated.format, Format::Turtle) {
            return Ok(status_result(
                &validated,
                lane,
                "ineligible",
                "horned-owl 1.4.0 exposes no Turtle reader in the pinned API",
            ));
        }
        if lane == Lane::Common
            && matches!(validated.format, Format::Functional)
            && functional_has_nested_annotations(&validated.source)
        {
            return Ok(status_result(
                &validated,
                lane,
                "ineligible",
                "horned-owl 1.4.0 discards nested Functional Syntax annotations",
            ));
        }
        if lane == Lane::Common
            && matches!(validated.format, Format::OwlXml)
            && owlxml_has_nested_annotations(&validated.source)
        {
            return Ok(status_result(
                &validated,
                lane,
                "ineligible",
                "horned-owl 1.4.0 cannot retain every nested OWL/XML annotation",
            ));
        }
        match lane {
            Lane::Raw => execute_raw(validated, lane),
            Lane::Common => execute_common(validated, lane),
        }
    })) {
        Ok(Ok(result)) => result,
        Ok(Err(error)) => fallback_status_result(&fallback, lane, "error", &safe_reason(&error)),
        Err(_) => fallback_status_result(
            &fallback,
            lane,
            "error",
            "Horned parser panicked while processing the bounded request",
        ),
    }
}

fn execute_raw(request: ValidatedRequest, lane: Lane) -> Result<Value, RunnerError> {
    let prepared = if request.input_mode == "file" {
        Some(prepare_file(
            &request.source,
            &request.source_sha256,
            request.format,
        )?)
    } else {
        None
    };
    let temporary_bytes = if prepared.is_some() {
        u64::try_from(request.source.len())
            .map_err(|_| RunnerError::new("source size exceeds unsigned 64-bit range"))?
    } else {
        0
    };

    let rss_before = rss_peak_bytes()?;
    let cpu_before = cpu_time_ns()?;
    let wall_started = Instant::now();
    let load_started = Instant::now();
    let rewrite_swrl = matches!(request.format, Format::Functional)
        && !functional_swrl_edits(&request.source)?.is_empty();
    let (ontology, diagnostic_count) = match prepared.as_ref() {
        Some(file) => {
            let stream = File::open(&file.path)?;
            parse_ontology(BufReader::new(stream), request.format, rewrite_swrl)?
        }
        None => parse_ontology(
            BufReader::new(Cursor::new(request.source.as_slice())),
            request.format,
            rewrite_swrl,
        )?,
    };
    let load_ns = elapsed_ns(load_started);

    let inventory_started = Instant::now();
    let inventory = build_inventory(&ontology, diagnostic_count)?;
    let inventory_ns = elapsed_ns(inventory_started);
    let object_count = ontology_object_count(&ontology, &inventory)?;
    let rss_after = rss_peak_bytes()?;
    let wall_ns = elapsed_ns(wall_started);
    let cpu_after = cpu_time_ns()?;
    let cpu_ns = cpu_after.saturating_sub(cpu_before);
    let rss_increment = rss_after.saturating_sub(rss_before);

    Ok(json!({
        "schema": ADAPTER_RESULT_SCHEMA,
        "lane": lane.id(),
        "implementation": IMPLEMENTATION,
        "boundary": lane.boundary(),
        "status": "ok",
        "reason": Value::Null,
        "corpus_id": request.corpus_id,
        "source_sha256": request.source_sha256,
        "options_sha256": request.options_sha256,
        "input_mode": request.input_mode,
        "process_mode": request.process_mode,
        "contract": Value::Null,
        "raw_inventory": inventory,
        "metrics": {
            "wall_ns": wall_ns,
            "cpu_ns": cpu_ns,
            "load_ns": load_ns,
            "rss_peak_before_bytes": rss_before,
            "rss_peak_after_bytes": rss_after,
            "rss_peak_increment_bytes": rss_increment,
            "temporary_bytes": temporary_bytes,
            "object_count": object_count,
            "phase_ns": {
                "horned_engine_load": load_ns,
                "raw_inventory": inventory_ns,
            },
        },
        "timed_validation": Value::Null,
        "artifact": artifact(lane)?,
    }))
}

fn execute_common(request: ValidatedRequest, lane: Lane) -> Result<Value, RunnerError> {
    let prepared = if request.input_mode == "file" {
        Some(prepare_file(
            &request.source,
            &request.source_sha256,
            request.format,
        )?)
    } else {
        None
    };
    let temporary_bytes = if prepared.is_some() {
        usize_to_u64(request.source.len(), "source size")?
    } else {
        0
    };
    let rss_before = rss_peak_bytes()?;
    let cpu_before = cpu_time_ns()?;
    let wall_started = Instant::now();
    let load_started = Instant::now();
    let rewrite_swrl = matches!(request.format, Format::Functional)
        && !functional_swrl_edits(&request.source)?.is_empty();
    let (ontology, diagnostic_count) = match prepared.as_ref() {
        Some(file) => {
            let stream = File::open(&file.path)?;
            parse_common_ontology(BufReader::new(stream), request.format, rewrite_swrl)?
        }
        None => parse_common_ontology(
            BufReader::new(Cursor::new(request.source.as_slice())),
            request.format,
            rewrite_swrl,
        )?,
    };
    let load_ns = elapsed_ns(load_started);
    if diagnostic_count != 0 {
        return Ok(status_result(
            &request,
            lane,
            "ineligible",
            "Horned RDF mapping is incomplete for the requested common contract",
        ));
    }
    let common_started = Instant::now();
    let built = common::build_common_contract(&ontology, &request, diagnostic_count)?;
    let common_adapter_ns = elapsed_ns(common_started);
    let contract = built.contract;
    let validation_ns = built.validation_ns;
    let contract_sha256 = contract["contract_sha256"]
        .as_str()
        .ok_or_else(|| RunnerError::new("common contract digest is missing"))?
        .to_owned();
    let object_count = usize_to_u64(
        common::object_count(&ontology, &contract),
        "common object count",
    )?;
    let rss_after = rss_peak_bytes()?;
    let wall_ns = elapsed_ns(wall_started);
    let cpu_after = cpu_time_ns()?;
    Ok(json!({
        "schema": ADAPTER_RESULT_SCHEMA,
        "lane": lane.id(),
        "implementation": IMPLEMENTATION,
        "boundary": lane.boundary(),
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
            "object_count": object_count,
            "phase_ns": {
                "horned_engine_load": load_ns,
                "common_contract": common_adapter_ns.saturating_sub(validation_ns),
                "contract_validation": validation_ns,
            },
        },
        "timed_validation": {
            "schema": TIMED_VALIDATION_SCHEMA,
            "inside_timed_envelope": true,
            "full_contract_validation": true,
            "contract_sha256": contract_sha256,
            "validation_ns": validation_ns,
        },
        "artifact": artifact(lane)?,
    }))
}

fn functional_has_nested_annotations(source: &[u8]) -> bool {
    let Ok(source) = std::str::from_utf8(source) else {
        return false;
    };
    let bytes = source.as_bytes();
    let mut stack = Vec::new();
    let mut offset = 0;
    while offset < bytes.len() {
        match bytes[offset] {
            b'#' => {
                offset += 1;
                while offset < bytes.len() && !matches!(bytes[offset], b'\r' | b'\n') {
                    offset += 1;
                }
            }
            b'<' => {
                offset += 1;
                while offset < bytes.len() {
                    match bytes[offset] {
                        b'\\' => offset = offset.saturating_add(2),
                        b'>' => {
                            offset += 1;
                            break;
                        }
                        _ => offset += 1,
                    }
                }
            }
            b'"' => {
                offset += 1;
                while offset < bytes.len() {
                    match bytes[offset] {
                        b'\\' => offset = offset.saturating_add(2),
                        b'"' => {
                            offset += 1;
                            break;
                        }
                        _ => offset += 1,
                    }
                }
            }
            b'(' => {
                stack.push(false);
                offset += 1;
            }
            b')' => {
                stack.pop();
                offset += 1;
            }
            byte if byte.is_ascii_alphabetic() => {
                let start = offset;
                offset += 1;
                while offset < bytes.len()
                    && (bytes[offset].is_ascii_alphanumeric()
                        || matches!(bytes[offset], b'_' | b'-'))
                {
                    offset += 1;
                }
                if &source[start..offset] != "Annotation" {
                    continue;
                }
                let mut lookahead = offset;
                loop {
                    while lookahead < bytes.len() && bytes[lookahead].is_ascii_whitespace() {
                        lookahead += 1;
                    }
                    if bytes.get(lookahead) != Some(&b'#') {
                        break;
                    }
                    while lookahead < bytes.len() && !matches!(bytes[lookahead], b'\r' | b'\n') {
                        lookahead += 1;
                    }
                }
                if bytes.get(lookahead) == Some(&b'(') {
                    if stack.iter().any(|is_annotation| *is_annotation) {
                        return true;
                    }
                    stack.push(true);
                    offset = lookahead + 1;
                }
            }
            _ => offset += 1,
        }
    }
    false
}

fn find_bytes(source: &[u8], start: usize, needle: &[u8]) -> Option<usize> {
    source
        .get(start..)?
        .windows(needle.len())
        .position(|window| window == needle)
        .and_then(|offset| start.checked_add(offset))
}

fn xml_markup_end(source: &[u8], mut offset: usize) -> usize {
    let mut quote = None;
    while offset < source.len() {
        match (quote, source[offset]) {
            (Some(selected), byte) if byte == selected => quote = None,
            (None, byte @ (b'\'' | b'"')) => quote = Some(byte),
            (None, b'>') => return offset + 1,
            _ => {}
        }
        offset += 1;
    }
    offset
}

fn owlxml_has_nested_annotations(source: &[u8]) -> bool {
    let mut annotation_depth = 0_u64;
    let mut offset = 0;
    while let Some(open) = find_bytes(source, offset, b"<") {
        if source.get(open..open.saturating_add(4)) == Some(b"<!--") {
            offset = find_bytes(source, open + 4, b"-->")
                .map_or(source.len(), |end| end.saturating_add(3));
            continue;
        }
        if source.get(open..open.saturating_add(9)) == Some(b"<![CDATA[") {
            offset = find_bytes(source, open + 9, b"]]>")
                .map_or(source.len(), |end| end.saturating_add(3));
            continue;
        }
        let end = xml_markup_end(source, open + 1);
        if end <= open + 1 || end > source.len() {
            break;
        }
        let mut name_start = open + 1;
        let closing = source.get(name_start) == Some(&b'/');
        if closing {
            name_start += 1;
        }
        if matches!(source.get(name_start), Some(b'!' | b'?')) {
            offset = end;
            continue;
        }
        let mut name_end = name_start;
        while name_end < end
            && !source[name_end].is_ascii_whitespace()
            && !matches!(source[name_end], b'/' | b'>')
        {
            name_end += 1;
        }
        let local_start = source[name_start..name_end]
            .iter()
            .rposition(|byte| *byte == b':')
            .map_or(name_start, |colon| name_start + colon + 1);
        if source.get(local_start..name_end) == Some(b"Annotation".as_slice()) {
            if closing {
                annotation_depth = annotation_depth.saturating_sub(1);
            } else {
                if annotation_depth > 0 {
                    return true;
                }
                let self_closing = source[open + 1..end - 1]
                    .iter()
                    .rev()
                    .find(|byte| !byte.is_ascii_whitespace())
                    == Some(&b'/');
                if !self_closing {
                    annotation_depth += 1;
                }
            }
        }
        offset = end;
    }
    false
}

fn skip_functional_trivia(source: &[u8], mut offset: usize) -> usize {
    loop {
        while offset < source.len() && source[offset].is_ascii_whitespace() {
            offset += 1;
        }
        if source.get(offset) != Some(&b'#') {
            return offset;
        }
        while offset < source.len() && !matches!(source[offset], b'\r' | b'\n') {
            offset += 1;
        }
    }
}

fn skip_functional_delimited(source: &[u8], mut offset: usize, terminal: u8) -> usize {
    offset += 1;
    while offset < source.len() {
        match source[offset] {
            b'\\' => offset = offset.saturating_add(2),
            byte if byte == terminal => return offset + 1,
            _ => offset += 1,
        }
    }
    offset
}

fn functional_matching_parenthesis(source: &[u8], open: usize) -> Option<usize> {
    if source.get(open) != Some(&b'(') {
        return None;
    }
    let mut depth = 0_u64;
    let mut offset = open;
    while offset < source.len() {
        match source[offset] {
            b'#' => offset = skip_functional_trivia(source, offset),
            b'<' => offset = skip_functional_delimited(source, offset, b'>'),
            b'"' => offset = skip_functional_delimited(source, offset, b'"'),
            b'(' => {
                depth = depth.checked_add(1)?;
                offset += 1;
            }
            b')' => {
                depth = depth.checked_sub(1)?;
                if depth == 0 {
                    return Some(offset);
                }
                offset += 1;
            }
            _ => offset += 1,
        }
    }
    None
}

fn functional_word_end(source: &[u8], start: usize) -> usize {
    let mut offset = start;
    while offset < source.len()
        && (source[offset].is_ascii_alphanumeric() || matches!(source[offset], b'_' | b'-'))
    {
        offset += 1;
    }
    offset
}

fn functional_swrl_edits(source: &[u8]) -> Result<Vec<FunctionalEdit>, RunnerError> {
    let mut edits = Vec::new();
    let mut offset = 0;
    while offset < source.len() {
        match source[offset] {
            b'#' => offset = skip_functional_trivia(source, offset),
            b'<' => offset = skip_functional_delimited(source, offset, b'>'),
            b'"' => offset = skip_functional_delimited(source, offset, b'"'),
            byte if byte.is_ascii_alphabetic() => {
                let start = offset;
                offset = functional_word_end(source, offset);
                if &source[start..offset] == b"SWRLRule" {
                    let outer_open = skip_functional_trivia(source, offset);
                    if source.get(outer_open) != Some(&b'(') {
                        continue;
                    }
                    let mut cursor = skip_functional_trivia(source, outer_open + 1);
                    loop {
                        let annotation_end = functional_word_end(source, cursor);
                        if source.get(cursor..annotation_end) != Some(b"Annotation".as_slice()) {
                            break;
                        }
                        let annotation_open = skip_functional_trivia(source, annotation_end);
                        if source.get(annotation_open) != Some(&b'(') {
                            break;
                        }
                        let annotation_close =
                            functional_matching_parenthesis(source, annotation_open).ok_or_else(
                                || RunnerError::new("Functional SWRL annotation is unterminated"),
                            )?;
                        cursor = skip_functional_trivia(source, annotation_close + 1);
                    }
                    let body_open = cursor;
                    let body_close = functional_matching_parenthesis(source, body_open)
                        .ok_or_else(|| RunnerError::new("Functional SWRL body is malformed"))?;
                    let head_open = skip_functional_trivia(source, body_close + 1);
                    let head_close = functional_matching_parenthesis(source, head_open)
                        .ok_or_else(|| RunnerError::new("Functional SWRL head is malformed"))?;
                    let outer_close = skip_functional_trivia(source, head_close + 1);
                    if source.get(outer_close) != Some(&b')') {
                        return Err(RunnerError::new(
                            "Functional SWRL rule has trailing structural content",
                        ));
                    }
                    edits.extend([
                        FunctionalEdit {
                            start,
                            end: offset,
                            replacement: b"DLSafeRule",
                        },
                        FunctionalEdit {
                            start: body_open,
                            end: body_open,
                            replacement: b"Body",
                        },
                        FunctionalEdit {
                            start: head_open,
                            end: head_open,
                            replacement: b"Head",
                        },
                    ]);
                    offset = outer_close + 1;
                }
            }
            _ => offset += 1,
        }
    }
    Ok(edits)
}

fn functional_swrl_source(source: &[u8]) -> Result<Option<Vec<u8>>, RunnerError> {
    let edits = functional_swrl_edits(source)?;
    if edits.is_empty() {
        return Ok(None);
    }
    let additional = edits
        .iter()
        .try_fold(0_usize, |total, edit| {
            total.checked_add(
                edit.replacement
                    .len()
                    .saturating_sub(edit.end.saturating_sub(edit.start)),
            )
        })
        .ok_or_else(|| RunnerError::new("Functional SWRL adaptation length overflows usize"))?;
    let mut output = Vec::with_capacity(source.len().saturating_add(additional));
    let mut retained = 0;
    for edit in edits {
        if edit.start < retained || edit.end < edit.start || edit.end > source.len() {
            return Err(RunnerError::new("Functional SWRL adaptations overlap"));
        }
        output.extend_from_slice(&source[retained..edit.start]);
        output.extend_from_slice(edit.replacement);
        retained = edit.end;
    }
    output.extend_from_slice(&source[retained..]);
    Ok(Some(output))
}

fn parse_ontology<R: BufRead>(
    mut reader: R,
    format: Format,
    rewrite_swrl: bool,
) -> Result<(RcIRIMappedOntology, u64), RunnerError> {
    match format {
        Format::Functional => {
            let parsed = if rewrite_swrl {
                let mut source = Vec::new();
                reader.read_to_end(&mut source)?;
                let adapted = functional_swrl_source(&source)?.ok_or_else(|| {
                    RunnerError::new("Functional SWRL adaptation was requested without a rule")
                })?;
                horned_io::ofn::reader::read(
                    BufReader::new(Cursor::new(adapted)),
                    ParserConfiguration::default(),
                )
            } else {
                horned_io::ofn::reader::read(reader, ParserConfiguration::default())
            };
            let (ontology, _): (RcIRIMappedOntology, _) = parsed.map_err(|error| {
                RunnerError::new(format!("Horned Functional parse failed: {error}"))
            })?;
            Ok((ontology, 0))
        }
        Format::OwlXml => {
            let (ontology, _): (RcIRIMappedOntology, _) =
                horned_io::owx::reader::read(&mut reader, ParserConfiguration::default()).map_err(
                    |error| RunnerError::new(format!("Horned OWL/XML parse failed: {error}")),
                )?;
            Ok((ontology, 0))
        }
        Format::RdfXml => {
            let config = ParserConfiguration {
                rdf: RDFParserConfiguration {
                    lax: true,
                    ..RDFParserConfiguration::default()
                },
                ..ParserConfiguration::default()
            };
            let (ontology, incomplete) = horned_io::rdf::reader::read(&mut reader, config)
                .map_err(|error| {
                    RunnerError::new(format!("Horned RDF/XML parse failed: {error}"))
                })?;
            let diagnostic_count = incomplete_count(&incomplete)?;
            let set_ontology: horned_owl::ontology::set::SetOntology<RcStr> = ontology.into();
            Ok((set_ontology.into(), diagnostic_count))
        }
        Format::Turtle => Err(RunnerError::new(
            "Horned Turtle requests must be rejected before parsing",
        )),
    }
}

const RDF_TYPE_IRI: &str = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type";
const OWL_AXIOM_IRI: &str = "http://www.w3.org/2002/07/owl#Axiom";
const OWL_ANNOTATION_IRI: &str = "http://www.w3.org/2002/07/owl#Annotation";
const OWL_ANNOTATED_SOURCE_IRI: &str = "http://www.w3.org/2002/07/owl#annotatedSource";
const OWL_ANNOTATED_PROPERTY_IRI: &str = "http://www.w3.org/2002/07/owl#annotatedProperty";
const OWL_ANNOTATED_TARGET_IRI: &str = "http://www.w3.org/2002/07/owl#annotatedTarget";

#[derive(Default)]
struct ReificationMetadata {
    kinds: HashSet<String>,
    source: Vec<Term>,
    property: Vec<Term>,
    target: Vec<Term>,
}

fn referenced_node(value: &Term) -> Option<NamedOrBlankNode> {
    match value {
        Term::NamedNode(value) => Some(value.clone().into()),
        Term::BlankNode(value) => Some(value.clone().into()),
        Term::Literal(_) => None,
    }
}

fn replacement_term(
    value: Term,
    replacements: &HashMap<NamedOrBlankNode, NamedOrBlankNode>,
) -> Term {
    let Some(mut node) = referenced_node(&value) else {
        return value;
    };
    while let Some(replacement) = replacements.get(&node) {
        node = replacement.clone();
    }
    match node {
        NamedOrBlankNode::NamedNode(value) => value.into(),
        NamedOrBlankNode::BlankNode(value) => value.into(),
    }
}

fn reification_key(
    metadata: &ReificationMetadata,
    replacements: &HashMap<NamedOrBlankNode, NamedOrBlankNode>,
) -> Option<String> {
    if metadata.kinds.len() != 1
        || metadata.source.len() != 1
        || metadata.property.len() != 1
        || metadata.target.len() != 1
    {
        return None;
    }
    let kind = metadata.kinds.iter().next()?;
    if kind != OWL_AXIOM_IRI {
        return None;
    }
    let source = replacement_term(metadata.source[0].clone(), replacements);
    let property = replacement_term(metadata.property[0].clone(), replacements);
    let target = replacement_term(metadata.target[0].clone(), replacements);
    Some(format!("{kind}\0{source}\0{property}\0{target}"))
}

// Horned 1.4 stores annotations by the reified main triple.  When several
// owl:Axiom resources describe that triple, randomized blank-node traversal
// makes the last resource overwrite the others.  Collapse equivalent
// resources before Horned sees them so its one map entry contains the union of
// their qualifier triples. References to the folded resources are redirected
// too, so nested annotations keep pointing at the surviving axiom resource.
fn coalesce_duplicate_rdf_reifications(source: &[u8]) -> Result<Option<Vec<u8>>, RunnerError> {
    let triples = RdfParser::from_format(RdfFormat::RdfXml)
        .for_reader(Cursor::new(source))
        .map(|quad| {
            quad.map(Triple::from)
                .map_err(|error| RunnerError::new(format!("RDF/XML preparse failed: {error}")))
        })
        .collect::<Result<Vec<_>, _>>()?;

    let mut metadata = HashMap::<NamedOrBlankNode, ReificationMetadata>::new();
    for triple in &triples {
        match triple.predicate.as_str() {
            RDF_TYPE_IRI => {
                if let Term::NamedNode(kind) = &triple.object {
                    if matches!(kind.as_str(), OWL_AXIOM_IRI | OWL_ANNOTATION_IRI) {
                        metadata
                            .entry(triple.subject.clone())
                            .or_default()
                            .kinds
                            .insert(kind.as_str().to_owned());
                    }
                }
            }
            OWL_ANNOTATED_SOURCE_IRI => {
                metadata
                    .entry(triple.subject.clone())
                    .or_default()
                    .source
                    .push(triple.object.clone());
            }
            OWL_ANNOTATED_PROPERTY_IRI => {
                metadata
                    .entry(triple.subject.clone())
                    .or_default()
                    .property
                    .push(triple.object.clone());
            }
            OWL_ANNOTATED_TARGET_IRI => {
                metadata
                    .entry(triple.subject.clone())
                    .or_default()
                    .target
                    .push(triple.object.clone());
            }
            _ => {}
        }
    }

    let mut replacements = HashMap::<NamedOrBlankNode, NamedOrBlankNode>::new();
    loop {
        let mut groups = HashMap::<String, Vec<NamedOrBlankNode>>::new();
        for (node, value) in &metadata {
            if let Some(key) = reification_key(value, &replacements) {
                groups.entry(key).or_default().push(node.clone());
            }
        }
        let mut changed = false;
        for nodes in groups.values_mut() {
            if nodes.len() < 2 {
                continue;
            }
            nodes.sort_by_key(ToString::to_string);
            let representative = nodes[0].clone();
            for node in &nodes[1..] {
                if replacements.insert(node.clone(), representative.clone())
                    != Some(representative.clone())
                {
                    changed = true;
                }
            }
        }
        if !changed {
            break;
        }
    }
    if replacements.is_empty() {
        return Ok(None);
    }

    let mut rewritten = Vec::with_capacity(triples.len());
    let mut seen = HashSet::with_capacity(triples.len());
    for triple in triples {
        let subject = match replacement_term(triple.subject.clone().into(), &replacements) {
            Term::NamedNode(value) => value.into(),
            Term::BlankNode(value) => value.into(),
            Term::Literal(_) => unreachable!("RDF triple subjects are resources"),
        };
        let object = replacement_term(triple.object, &replacements);
        let triple = Triple {
            subject,
            predicate: triple.predicate,
            object,
        };
        if seen.insert(triple.clone()) {
            rewritten.push(triple);
        }
    }
    let mut serializer = RdfSerializer::from_format(RdfFormat::NTriples).for_writer(Vec::new());
    for triple in &rewritten {
        serializer.serialize_triple(triple).map_err(|error| {
            RunnerError::new(format!("RDF reification rewrite failed: {error}"))
        })?;
    }
    serializer
        .finish()
        .map(Some)
        .map_err(|error| RunnerError::new(format!("RDF reification rewrite failed: {error}")))
}

fn parse_common_ontology<R: BufRead>(
    mut reader: R,
    format: Format,
    rewrite_swrl: bool,
) -> Result<(RcIRIMappedOntology, u64), RunnerError> {
    if !matches!(format, Format::RdfXml) {
        return parse_ontology(reader, format, rewrite_swrl);
    }
    let mut source = Vec::new();
    reader.read_to_end(&mut source)?;
    let Some(rewritten) = coalesce_duplicate_rdf_reifications(&source)? else {
        return parse_ontology(BufReader::new(Cursor::new(source)), format, rewrite_swrl);
    };
    let config = ParserConfiguration {
        rdf: RDFParserConfiguration {
            lax: true,
            format: Some(RdfFormat::NTriples),
        },
        ..ParserConfiguration::default()
    };
    let (ontology, incomplete) =
        horned_io::rdf::reader::read(&mut BufReader::new(Cursor::new(rewritten)), config).map_err(
            |error| RunnerError::new(format!("Horned rewritten RDF parse failed: {error}")),
        )?;
    let diagnostic_count = incomplete_count(&incomplete)?;
    let set_ontology: horned_owl::ontology::set::SetOntology<RcStr> = ontology.into();
    Ok((set_ontology.into(), diagnostic_count))
}

fn incomplete_count(
    incomplete: &horned_owl::io::rdf::reader::IncompleteParse<RcStr>,
) -> Result<u64, RunnerError> {
    let count = incomplete.simple.len()
        + incomplete.bnode.len()
        + incomplete.bnode_seq.len()
        + incomplete.class_expression.len()
        + incomplete.object_property_expression.len()
        + incomplete.data_range.len()
        + incomplete.ann_map.len()
        + incomplete.atom.len();
    u64::try_from(count).map_err(|_| RunnerError::new("RDF/XML diagnostic inventory exceeds u64"))
}

fn build_inventory(
    ontology: &RcIRIMappedOntology,
    diagnostic_count: u64,
) -> Result<RawInventory, RunnerError> {
    let mut axiom_count = 0_u64;
    let mut annotation_count = 0_u64;
    let mut import_count = 0_u64;
    let mut walk = Walk::new(SignatureInventory::default());

    for component in ontology.iter() {
        walk.annotated_component(component);
        annotation_count = checked_add(
            annotation_count,
            usize_to_u64(component.ann.len(), "annotation count")?,
            "annotation count",
        )?;
        match component.kind() {
            ComponentKind::OntologyID | ComponentKind::DocIRI => {}
            ComponentKind::OntologyAnnotation => {
                annotation_count = checked_add(annotation_count, 1, "annotation count")?;
            }
            ComponentKind::Import => {
                import_count = checked_add(import_count, 1, "import count")?;
            }
            _ => {
                axiom_count = checked_add(axiom_count, 1, "axiom count")?;
            }
        }
    }
    let signature = walk.into_visit();
    let entity_count = usize_to_u64(signature.entities.len(), "entity count")?;
    let digest = raw_inventory_digest(
        axiom_count,
        annotation_count,
        import_count,
        entity_count,
        diagnostic_count,
    );
    Ok(RawInventory {
        schema: RAW_INVENTORY_SCHEMA,
        model_kind: "horned-model-ready",
        axiom_count,
        annotation_count,
        import_count,
        entity_count,
        diagnostic_count,
        inventory_sha256: digest,
    })
}

fn ontology_object_count(
    ontology: &RcIRIMappedOntology,
    inventory: &RawInventory,
) -> Result<u64, RunnerError> {
    let components = usize_to_u64(ontology.iter().len(), "component count")?;
    checked_add(
        checked_add(components, inventory.annotation_count, "object count")?,
        inventory.entity_count,
        "object count",
    )
}

fn raw_inventory_digest(
    axiom_count: u64,
    annotation_count: u64,
    import_count: u64,
    entity_count: u64,
    diagnostic_count: u64,
) -> String {
    let payload = format!(
        concat!(
            "{{\"annotation_count\":{},",
            "\"axiom_count\":{},",
            "\"diagnostic_count\":{},",
            "\"entity_count\":{},",
            "\"import_count\":{},",
            "\"model_kind\":\"horned-model-ready\",",
            "\"schema\":\"pyowl-core/comparator-raw-inventory/v1\"}}"
        ),
        annotation_count, axiom_count, diagnostic_count, entity_count, import_count,
    );
    let mut hasher = Sha256::new();
    hasher.update(RAW_INVENTORY_DOMAIN);
    hasher.update(payload.as_bytes());
    hex_digest(hasher.finalize().as_slice())
}

fn prepare_file(
    source: &[u8],
    source_sha256: &str,
    format: Format,
) -> Result<TempInput, RunnerError> {
    let directory = std::env::temp_dir();
    for _ in 0..16 {
        let counter = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let name = format!(
            "pyowl-core-horned-{}-{counter}-{}.{}",
            process::id(),
            &source_sha256[..16],
            format.extension(),
        );
        let path = directory.join(name);
        match OpenOptions::new().write(true).create_new(true).open(&path) {
            Ok(mut stream) => {
                stream.write_all(source)?;
                stream.flush()?;
                drop(stream);
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

fn status_result(request: &ValidatedRequest, lane: Lane, status: &str, reason: &str) -> Value {
    let identity = json!({
        "corpus_id": request.corpus_id,
        "source_sha256": request.source_sha256,
        "options_sha256": request.options_sha256,
        "input_mode": request.input_mode,
        "process_mode": request.process_mode,
    });
    fallback_status_result(&identity, lane, status, reason)
}

fn fallback_status_result(identity: &Value, lane: Lane, status: &str, reason: &str) -> Value {
    json!({
        "schema": ADAPTER_RESULT_SCHEMA,
        "lane": lane.id(),
        "implementation": IMPLEMENTATION,
        "boundary": lane.boundary(),
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
        "artifact": artifact(lane).unwrap_or_else(|_| json!({
            "pin_state": "complete",
            "version": ENGINE_VERSION,
            "revision": ENGINE_REVISION,
            "artifact": ENGINE_ARTIFACT,
            "artifact_sha256": ENGINE_SHA256,
            "features": lane.features(),
            "allocator": ALLOCATOR,
            "thread_ceiling": THREAD_CEILING,
            "runner_revision": lane.runner_revision(),
            "runner_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
        })),
    })
}

fn artifact(lane: Lane) -> Result<Value, RunnerError> {
    serde_json::to_value(Artifact {
        pin_state: "complete",
        version: ENGINE_VERSION,
        revision: ENGINE_REVISION,
        artifact: ENGINE_ARTIFACT,
        artifact_sha256: ENGINE_SHA256,
        features: lane.features().to_vec(),
        allocator: ALLOCATOR,
        thread_ceiling: THREAD_CEILING,
        runner_revision: lane.runner_revision(),
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

fn verify_environment() -> Result<(String, Lane), RunnerError> {
    let lane = Lane::parse(
        &std::env::var("PYOWL_CORE_COMPARATOR_LANE")
            .map_err(|_| RunnerError::new("runner lane is missing"))?,
    )?;
    for (name, expected) in [
        ("PYOWL_CORE_COMPARATOR_IMPLEMENTATION", IMPLEMENTATION),
        ("PYOWL_CORE_COMPARATOR_BOUNDARY", lane.boundary()),
        ("RAYON_NUM_THREADS", "1"),
    ] {
        if std::env::var(name).ok().as_deref() != Some(expected) {
            return Err(RunnerError::new(format!(
                "runner environment {name} differs from pin"
            )));
        }
    }
    if !CARGO_LOCK.contains(HORNED_LOCK_STANZA) {
        return Err(RunnerError::new(
            "embedded Cargo.lock differs from the Horned engine pin",
        ));
    }
    let protocol_mode = std::env::var("PYOWL_CORE_COMPARATOR_PROTOCOL_MODE")
        .map_err(|_| RunnerError::new("runner protocol mode is missing"))?;
    if !matches!(protocol_mode.as_str(), "fresh" | "persistent") {
        return Err(RunnerError::new("runner protocol mode is unsupported"));
    }
    runner_sha256()?;
    Ok((protocol_mode, lane))
}

fn fresh_main(lane: Lane) -> Result<(), RunnerError> {
    let mut body = Vec::new();
    io::stdin()
        .lock()
        .take((MAX_REQUEST_BYTES + 1) as u64)
        .read_to_end(&mut body)?;
    let result = if body.len() > MAX_REQUEST_BYTES {
        fallback_status_result(
            &fallback_identity(),
            lane,
            "error",
            "adapter request exceeds size limit",
        )
    } else {
        match serde_json::from_slice::<AdapterRequest>(&body) {
            Ok(request) => run_request(request, "fresh", lane),
            Err(_) => fallback_status_result(
                &fallback_identity(),
                lane,
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

fn persistent_main(lane: Lane) -> Result<(), RunnerError> {
    let pid = u64::from(process::id());
    write_frame(&json!({
        "schema": PERSISTENT_HANDSHAKE_SCHEMA,
        "protocol": PERSISTENT_PROTOCOL_SCHEMA,
        "lane": lane.id(),
        "implementation": IMPLEMENTATION,
        "boundary": lane.boundary(),
        "pid": pid,
        "request_schema": ADAPTER_REQUEST_SCHEMA,
        "result_schema": ADAPTER_RESULT_SCHEMA,
        "fresh_ontology_per_request": true,
        "artifact": artifact(lane)?,
    }))?;
    let mut input = BufReader::new(io::stdin().lock());
    let mut instance_counter = 0_u64;
    let mut expected_sequence = 0_u64;
    loop {
        let payload = read_frame(&mut input)?;
        if let Ok(shutdown) = serde_json::from_slice::<PersistentShutdown>(&payload) {
            if shutdown.schema != PERSISTENT_SHUTDOWN_SCHEMA
                || shutdown.protocol != PERSISTENT_PROTOCOL_SCHEMA
            {
                return Err(RunnerError::new("persistent shutdown protocol differs"));
            }
            if shutdown.sequence != expected_sequence {
                return Err(RunnerError::new(
                    "persistent shutdown sequence is nonmonotonic",
                ));
            }
            write_frame(&json!({
                "schema": PERSISTENT_SHUTDOWN_ACK_SCHEMA,
                "protocol": PERSISTENT_PROTOCOL_SCHEMA,
                "sequence": shutdown.sequence,
                "pid": pid,
            }))?;
            return Ok(());
        }
        // Deserialize the original bytes directly into the strict schema.
        // Serde's derived struct visitor rejects duplicate and unknown fields;
        // parsing through Value first would silently collapse duplicates.
        let envelope: PersistentRequest = serde_json::from_slice(&payload)
            .map_err(|_| RunnerError::new("persistent request fields differ"))?;
        if envelope.schema != PERSISTENT_REQUEST_SCHEMA
            || envelope.protocol != PERSISTENT_PROTOCOL_SCHEMA
        {
            return Err(RunnerError::new("persistent request protocol differs"));
        }
        if envelope.sequence != expected_sequence {
            return Err(RunnerError::new(
                "persistent request sequence is nonmonotonic",
            ));
        }
        let result = run_request(envelope.request, "persistent", lane);
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
    let raw_length = std::str::from_utf8(digits)
        .map_err(|_| RunnerError::new("persistent frame length is invalid"))?;
    let length: usize = raw_length
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
    let mut hasher = Sha256::new();
    hasher.update(payload);
    hex_digest(hasher.finalize().as_slice())
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
            .all(|character| character.is_ascii_digit() || (b'a'..=b'f').contains(&character))
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
    // SAFETY: getrusage initializes the pointed-to rusage on success.  The
    // pointer is valid for writes and RUSAGE_SELF is supported on Unix.
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
        "the pinned Horned runner requires Unix getrusage",
    ))
}

#[cfg(not(unix))]
fn rss_peak_bytes() -> Result<u64, RunnerError> {
    Err(RunnerError::new(
        "the pinned Horned runner requires Unix getrusage",
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
    let (protocol_mode, lane) = verify_environment()?;
    match protocol_mode.as_str() {
        "fresh" => fresh_main(lane),
        "persistent" => persistent_main(lane),
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
    fn raw_digest_matches_python_canonical_preimage() {
        assert_eq!(
            raw_inventory_digest(3, 5, 7, 11, 13),
            "3a92780d017f14e0524c295dc4f12583fa223c924c29ed431b53d64da79d00aa"
        );
    }

    #[test]
    fn embedded_lock_binds_exact_horned_crate() {
        assert!(CARGO_LOCK.contains(HORNED_LOCK_STANZA));
    }

    #[test]
    fn strict_digest_validation_rejects_uppercase() {
        assert!(is_sha256(&"a".repeat(64)));
        assert!(!is_sha256(&"A".repeat(64)));
    }

    #[test]
    fn functional_nested_annotation_detection_is_syntax_aware() {
        assert!(functional_has_nested_annotations(
            br#"Ontology(Annotation(Annotation(<urn:q> "nested") <urn:p> "outer"))"#,
        ));
        assert!(functional_has_nested_annotations(
            br#"Ontology(SubClassOf(Annotation(Annotation(<urn:q> <urn:v>) <urn:p> <urn:v>) <urn:A> <urn:B>))"#,
        ));
        assert!(!functional_has_nested_annotations(
            br#"Ontology(AnnotationAssertion(Annotation(<urn:q> "v") <urn:p> <urn:s> "Annotation(ignored)"))"#,
        ));
        assert!(!functional_has_nested_annotations(
            b"Ontology(# Annotation(Annotation(<urn:q> <urn:v>) <urn:p> <urn:v>)\n)",
        ));
        assert!(!functional_has_nested_annotations(
            b"Ontology(Declaration(Class(<urn:Annotation(Annotation)>)))",
        ));
    }

    #[test]
    fn functional_swrl_adaptation_only_rewrites_syntax_tokens() {
        let source = br#"Ontology(
            # SWRLRule((ClassAtom(<urn:A> Variable(<urn:x>)))())
            AnnotationAssertion(<urn:p> <urn:s> "SWRLRule(ignored)")
            AnnotationAssertion(<urn:p> <urn:s> <urn:SWRLRule(ignored)>)
            SWRLRule((ClassAtom(<urn:A> Variable(<urn:x>)))())
        )"#;
        let adapted = functional_swrl_source(source).unwrap().unwrap();
        assert_eq!(
            adapted
                .windows(b"DLSafeRule".len())
                .filter(|window| *window == b"DLSafeRule")
                .count(),
            1
        );
        assert_eq!(
            adapted
                .windows(b"SWRLRule".len())
                .filter(|window| *window == b"SWRLRule")
                .count(),
            3
        );
        assert!(functional_swrl_source(b"Ontology()").unwrap().is_none());
    }

    #[test]
    fn owlxml_nested_annotation_detection_ignores_non_markup_content() {
        assert!(owlxml_has_nested_annotations(
            br#"<Ontology><Annotation><Annotation/></Annotation></Ontology>"#,
        ));
        assert!(owlxml_has_nested_annotations(
            br#"<owl:Ontology><owl:Annotation><owl:Annotation></owl:Annotation></owl:Annotation></owl:Ontology>"#,
        ));
        assert!(!owlxml_has_nested_annotations(
            br#"<Ontology><!-- <Annotation><Annotation/></Annotation> --><Annotation><Literal>&lt;Annotation&gt;</Literal></Annotation></Ontology>"#,
        ));
        assert!(!owlxml_has_nested_annotations(
            br#"<Ontology><AnnotationProperty IRI="urn:Annotation"/><Annotation data="&lt;Annotation&gt;"/></Ontology>"#,
        ));
    }
}
