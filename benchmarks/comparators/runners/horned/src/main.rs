//! Pinned development-only Horned-OWL raw comparator runner.
//!
//! The process implements both the one-shot adapter protocol and the audited
//! persistent lifecycle.  Parsing and all Horned-owned readiness indexes are
//! built inside the measured envelope; transport decoding and prepared-file
//! creation are deliberately outside it.

use std::collections::BTreeSet;
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
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

const LANE: &str = "horned-owl-raw";
const IMPLEMENTATION: &str = "horned-owl";
const BOUNDARY: &str = "horned-model-ready";
const ENGINE_VERSION: &str = "1.4.0";
const ENGINE_REVISION: &str = "crates.io horned-owl 1.4.0 (2026-01-09)";
const ENGINE_ARTIFACT: &str = "crates.io horned-owl-1.4.0.crate";
const ENGINE_SHA256: &str = "877f6118b6f5823bb135d04e36fe2c2d3a2b4493feca8ac09b5fa6e91b9fff9e";
const ALLOCATOR: &str = "Rust system allocator";
const THREAD_CEILING: u64 = 1;
const RUNNER_REVISION: &str = "pyowl-core-horned-raw-runner-v1";

const ADAPTER_REQUEST_SCHEMA: &str = "pyowl-core/comparator-adapter-request/v2";
const ADAPTER_RESULT_SCHEMA: &str = "pyowl-core/comparator-adapter-result/v1";
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
}

#[derive(Debug)]
struct ValidatedRequest {
    corpus_id: String,
    source: Vec<u8>,
    source_sha256: String,
    format: Format,
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
    artifact_sha256: &'static str,
    features: [&'static str; 1],
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
) -> Result<ValidatedRequest, RunnerError> {
    let runner_sha256 = runner_sha256()?;
    let expected_scalars = [
        ("schema", request.schema.as_str(), ADAPTER_REQUEST_SCHEMA),
        ("lane", request.lane.as_str(), LANE),
        (
            "implementation",
            request.implementation.as_str(),
            IMPLEMENTATION,
        ),
        ("boundary", request.boundary.as_str(), BOUNDARY),
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
            RUNNER_REVISION,
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
    if request.expected_features != ["default"] {
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
        format,
        options_sha256: request.options_sha256,
        input_mode: request.input_mode,
        process_mode: request.process_mode,
    })
}

fn run_request(request: AdapterRequest, protocol_mode: &str) -> Value {
    let fallback = request_identity(&request);
    match panic::catch_unwind(AssertUnwindSafe(|| {
        let validated = validate_request(request, protocol_mode)?;
        if matches!(validated.format, Format::Turtle) {
            return Ok(status_result(
                &validated,
                "ineligible",
                "horned-owl 1.4.0 exposes no Turtle reader in the pinned API",
            ));
        }
        execute_raw(validated)
    })) {
        Ok(Ok(result)) => result,
        Ok(Err(error)) => fallback_status_result(&fallback, "error", &safe_reason(&error)),
        Err(_) => fallback_status_result(
            &fallback,
            "error",
            "Horned parser panicked while processing the bounded request",
        ),
    }
}

fn execute_raw(request: ValidatedRequest) -> Result<Value, RunnerError> {
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
    let (ontology, diagnostic_count) = match prepared.as_ref() {
        Some(file) => {
            let stream = File::open(&file.path)?;
            parse_ontology(BufReader::new(stream), request.format)?
        }
        None => parse_ontology(
            BufReader::new(Cursor::new(request.source.as_slice())),
            request.format,
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
        "artifact": artifact()?,
    }))
}

fn parse_ontology<R: BufRead>(
    mut reader: R,
    format: Format,
) -> Result<(RcIRIMappedOntology, u64), RunnerError> {
    match format {
        Format::Functional => {
            let (ontology, _): (RcIRIMappedOntology, _) =
                horned_io::ofn::reader::read(reader, ParserConfiguration::default()).map_err(
                    |error| RunnerError::new(format!("Horned Functional parse failed: {error}")),
                )?;
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
        model_kind: BOUNDARY,
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
            "pin_state": "complete",
            "version": ENGINE_VERSION,
            "revision": ENGINE_REVISION,
            "artifact": ENGINE_ARTIFACT,
            "artifact_sha256": ENGINE_SHA256,
            "features": ["default"],
            "allocator": ALLOCATOR,
            "thread_ceiling": THREAD_CEILING,
            "runner_revision": RUNNER_REVISION,
            "runner_sha256": "0000000000000000000000000000000000000000000000000000000000000000",
        })),
    })
}

fn artifact() -> Result<Value, RunnerError> {
    serde_json::to_value(Artifact {
        pin_state: "complete",
        version: ENGINE_VERSION,
        revision: ENGINE_REVISION,
        artifact: ENGINE_ARTIFACT,
        artifact_sha256: ENGINE_SHA256,
        features: ["default"],
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
}
