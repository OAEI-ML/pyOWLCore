package org.pyowlcore.comparator;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.EOFException;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.lang.management.ManagementFactory;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Supplier;

import com.fasterxml.jackson.annotation.JsonIgnoreProperties;
import com.fasterxml.jackson.core.JsonFactory;
import com.fasterxml.jackson.core.StreamReadConstraints;
import com.fasterxml.jackson.core.StreamReadFeature;
import com.fasterxml.jackson.databind.DeserializationFeature;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.MapperFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.PropertyNamingStrategies;
import com.fasterxml.jackson.databind.cfg.CoercionAction;
import com.fasterxml.jackson.databind.cfg.CoercionInputShape;
import com.fasterxml.jackson.databind.type.LogicalType;

import org.semanticweb.owlapi.apibinding.OWLManager;
import org.semanticweb.owlapi.formats.FunctionalSyntaxDocumentFormat;
import org.semanticweb.owlapi.formats.OWLXMLDocumentFormat;
import org.semanticweb.owlapi.formats.RDFXMLDocumentFormat;
import org.semanticweb.owlapi.formats.TurtleDocumentFormat;
import org.semanticweb.owlapi.io.StreamDocumentSource;
import org.semanticweb.owlapi.model.IRI;
import org.semanticweb.owlapi.model.MissingImportHandlingStrategy;
import org.semanticweb.owlapi.model.OWLDocumentFormat;
import org.semanticweb.owlapi.model.OWLOntology;
import org.semanticweb.owlapi.model.OWLOntologyLoaderConfiguration;
import org.semanticweb.owlapi.model.OWLOntologyManager;

/** Isolated, pinned OWLAPI common-contract comparator process. */
public final class OwlApiRunner {
    private static final String IMPLEMENTATION = "owlapi";
    private static final String LANE = "owlapi-common";
    private static final String BOUNDARY = "common-contract-ready";
    private static final String VERSION = "5.5.1";
    private static final String REVISION =
            "Maven Central net.sourceforge.owlapi:owlapi-distribution:5.5.1";
    private static final String ARTIFACT =
            "Maven Central net.sourceforge.owlapi:owlapi-distribution:5.5.1";
    private static final String ARTIFACT_SHA256 =
            "747b1a5269fee2992487dcde946f16dfbc14aa458d50854994a0485cf263ce07";
    private static final String ALLOCATOR = "HotSpot G1GC";
    private static final long THREAD_CEILING = 1;
    private static final String RUNNER_REVISION = "pyowl-core-owlapi-common-runner-v7";
    private static final List<String> FEATURES =
            List.of("isolated-java", "common-contract-v2");

    private static final String REQUEST_SCHEMA = "pyowl-core/comparator-adapter-request/v2";
    private static final String RESULT_SCHEMA = "pyowl-core/comparator-adapter-result/v1";
    private static final String VALIDATION_SCHEMA =
            "pyowl-core/comparator-timed-validation/v1";
    private static final String FRESH_PROTOCOL_SCHEMA =
            "pyowl-core/comparator-fresh-runner/v1";
    private static final String FRESH_REQUEST_SCHEMA =
            "pyowl-core/comparator-fresh-request/v1";
    private static final String FRESH_COMPLETED_SCHEMA =
            "pyowl-core/comparator-fresh-completed/v1";
    private static final String FRESH_PUBLISH_SCHEMA =
            "pyowl-core/comparator-fresh-publish/v1";
    private static final String FRESH_RESPONSE_SCHEMA =
            "pyowl-core/comparator-fresh-response/v1";
    private static final String PROTOCOL_SCHEMA =
            "pyowl-core/comparator-persistent-runner/v3";
    private static final String HANDSHAKE_SCHEMA =
            "pyowl-core/comparator-persistent-handshake/v3";
    private static final String PERSISTENT_REQUEST_SCHEMA =
            "pyowl-core/comparator-persistent-request/v3";
    private static final String PREPARED_SCHEMA =
            "pyowl-core/comparator-persistent-prepared/v1";
    private static final String EXECUTE_SCHEMA =
            "pyowl-core/comparator-persistent-execute/v1";
    private static final String COMPLETED_SCHEMA =
            "pyowl-core/comparator-persistent-completed/v1";
    private static final String PUBLISH_SCHEMA =
            "pyowl-core/comparator-persistent-publish/v1";
    private static final String PERSISTENT_RESPONSE_SCHEMA =
            "pyowl-core/comparator-persistent-response/v3";
    private static final String SHUTDOWN_SCHEMA =
            "pyowl-core/comparator-persistent-shutdown/v3";
    private static final String SHUTDOWN_ACK_SCHEMA =
            "pyowl-core/comparator-persistent-shutdown-ack/v3";
    private static final String DOCUMENT_IRI_PREFIX =
            "urn:pyowl-core:comparator-source:sha256:";
    private static final int MAX_REQUEST_BYTES = 512 * 1024 * 1024;
    private static final int MAX_REQUEST_FRAME_BYTES = MAX_REQUEST_BYTES + 64 * 1024;
    private static final int MAX_CONTROL_FRAME_BYTES = 64 * 1024;
    private static final int MAX_FRAME_HEADER_BYTES = 32;
    private static final int MAX_REASON_CHARS = 1_000;

    private static final ObjectMapper JSON = strictJsonMapper();
    private static final AtomicLong TEMP_COUNTER = new AtomicLong();

    private OwlApiRunner() {}

    private static ObjectMapper strictJsonMapper() {
        ObjectMapper mapper = new ObjectMapper(
                JsonFactory.builder()
                        .enable(StreamReadFeature.STRICT_DUPLICATE_DETECTION)
                        .streamReadConstraints(
                                StreamReadConstraints.builder()
                                        .maxStringLength(MAX_REQUEST_FRAME_BYTES)
                                        .build())
                        .build())
                .setPropertyNamingStrategy(PropertyNamingStrategies.SNAKE_CASE)
                .enable(DeserializationFeature.FAIL_ON_UNKNOWN_PROPERTIES)
                .enable(DeserializationFeature.FAIL_ON_NULL_FOR_PRIMITIVES)
                .enable(DeserializationFeature.FAIL_ON_TRAILING_TOKENS)
                .disable(DeserializationFeature.ACCEPT_FLOAT_AS_INT)
                .disable(MapperFeature.ALLOW_COERCION_OF_SCALARS);
        mapper.coercionConfigFor(LogicalType.Textual)
                .setCoercion(CoercionInputShape.Integer, CoercionAction.Fail)
                .setCoercion(CoercionInputShape.Float, CoercionAction.Fail)
                .setCoercion(CoercionInputShape.Boolean, CoercionAction.Fail);
        return mapper;
    }

    public static final class AdapterRequest {
        public String schema;
        public String lane;
        public String implementation;
        public String boundary;
        public String corpusId;
        public String sourceB64;
        public String sourceSha256;
        public String documentIri;
        public String format;
        public String optionsSha256;
        public JsonNode options;
        public String inputMode;
        public String processMode;
        public String expectedArtifactSha256;
        public List<String> expectedFeatures;
        public String expectedAllocator;
        public long expectedThreadCeiling;
        public String expectedRunnerRevision;
        public String expectedRunnerSha256;
    }

    private static final class PreparedExecution {
        final Map<String, Object> fallback;
        final ValidatedRequest request;
        final Map<String, Object> completed;

        private PreparedExecution(
                Map<String, Object> fallback,
                ValidatedRequest request,
                Map<String, Object> completed) {
            this.fallback = fallback;
            this.request = request;
            this.completed = completed;
        }

        static PreparedExecution ready(
                Map<String, Object> fallback, ValidatedRequest request) {
            return new PreparedExecution(fallback, request, null);
        }

        static PreparedExecution complete(Map<String, Object> completed) {
            return new PreparedExecution(null, null, completed);
        }
    }

    private static final class ValidatedRequest {
        final String corpusId;
        final byte[] source;
        final String sourceSha256;
        final String documentIri;
        final Format format;
        final String optionsSha256;
        final String inputMode;
        final String processMode;

        ValidatedRequest(AdapterRequest request, byte[] source, Format format) {
            corpusId = request.corpusId;
            this.source = source;
            sourceSha256 = request.sourceSha256;
            documentIri = request.documentIri;
            this.format = format;
            optionsSha256 = request.optionsSha256;
            inputMode = request.inputMode;
            processMode = request.processMode;
        }
    }

    private enum Format {
        FUNCTIONAL("functional"),
        OWLXML("owlxml"),
        RDFXML("rdfxml"),
        TURTLE("turtle");

        final String value;

        Format(String value) {
            this.value = value;
        }

        static Format parse(String value) {
            for (Format format : values()) {
                if (format.value.equals(value)) {
                    return format;
                }
            }
            throw new IllegalArgumentException("adapter format is unsupported");
        }

        OWLDocumentFormat documentFormat() {
            switch (this) {
                case FUNCTIONAL:
                    return new FunctionalSyntaxDocumentFormat();
                case OWLXML:
                    return new OWLXMLDocumentFormat();
                case RDFXML:
                    return new RDFXMLDocumentFormat();
                case TURTLE:
                    return new TurtleDocumentFormat();
                default:
                    throw new AssertionError(this);
            }
        }

        String optionsSha256() {
            switch (this) {
                case FUNCTIONAL:
                    return "a68176678f9e39941cd6258b3b7181355afbbf751c89e43cc69e516aed82d24c";
                case OWLXML:
                    return "a24b7713aa79cad899ffe819abc25ac9e53f8b9657b2e22507b1745073a8253e";
                case RDFXML:
                    return "fdfc954b7b8f0253c8e90ee4542170f506ca069ac6bd93744ac0ceabf04f8d2f";
                case TURTLE:
                    return "6ad540e139870561dc6d37919e52c6534a494441e40a80fad8ab0f2e7a0f169b";
                default:
                    throw new AssertionError(this);
            }
        }
    }

    private static ValidatedRequest validate(AdapterRequest request, String protocolMode) {
        requireEqual("schema", request.schema, REQUEST_SCHEMA);
        requireEqual("lane", request.lane, LANE);
        requireEqual("implementation", request.implementation, IMPLEMENTATION);
        requireEqual("boundary", request.boundary, BOUNDARY);
        requireEqual("expected_artifact_sha256", request.expectedArtifactSha256, ARTIFACT_SHA256);
        requireEqual("expected_allocator", request.expectedAllocator, ALLOCATOR);
        requireEqual("expected_runner_revision", request.expectedRunnerRevision, RUNNER_REVISION);
        requireEqual("expected_runner_sha256", request.expectedRunnerSha256, runnerSha256());
        if (!FEATURES.equals(request.expectedFeatures)) {
            throw new IllegalArgumentException("adapter request features differ from runner pin");
        }
        if (request.expectedThreadCeiling != THREAD_CEILING) {
            throw new IllegalArgumentException(
                    "adapter request thread ceiling differs from runner pin");
        }
        if (request.corpusId == null || request.corpusId.isEmpty()) {
            throw new IllegalArgumentException("adapter corpus_id must be nonempty");
        }
        if (!isSha256(request.sourceSha256) || !isSha256(request.optionsSha256)) {
            throw new IllegalArgumentException("adapter digests must be lowercase SHA-256");
        }
        requireEqual("document_iri", request.documentIri,
                DOCUMENT_IRI_PREFIX + request.sourceSha256);
        if (!Set.of("resident-bytes", "file").contains(request.inputMode)) {
            throw new IllegalArgumentException("adapter input mode is unsupported");
        }
        String expectedProcess = "fresh".equals(protocolMode)
                ? "fresh-process" : "steady-process";
        requireEqual("process_mode", request.processMode, expectedProcess);
        Format format = Format.parse(request.format);
        if (!format.optionsSha256().equals(request.optionsSha256)) {
            throw new IllegalArgumentException(
                    "adapter options digest differs from semantic options");
        }
        Object observedOptions = JSON.convertValue(request.options, Object.class);
        if (!Arrays.equals(
                CommonContract.canonicalJson(expectedOptions(format.value)),
                CommonContract.canonicalJson(observedOptions))) {
            throw new IllegalArgumentException(
                    "OWLAPI runner supports only exact comparator options");
        }
        byte[] source;
        try {
            source = Base64.getDecoder().decode(request.sourceB64);
        } catch (IllegalArgumentException error) {
            throw new IllegalArgumentException("adapter source is not strict base64");
        }
        if (!Base64.getEncoder().encodeToString(source).equals(request.sourceB64)) {
            throw new IllegalArgumentException("adapter source base64 is noncanonical");
        }
        if (!Canonical.hex(Canonical.sha256(source)).equals(request.sourceSha256)) {
            throw new IllegalArgumentException("adapter source differs from pinned SHA-256");
        }
        return new ValidatedRequest(request, source, format);
    }

    private static Map<String, Object> expectedOptions(String format) {
        Map<String, Object> limits = new TreeMap<>();
        limits.put("cancellation_check_interval", 4_096L);
        limits.put("deadline_seconds", null);
        limits.put("max_annotations", 100_000_000L);
        limits.put("max_axioms", 100_000_000L);
        limits.put("max_canonical_work", 1_000_000_000L);
        limits.put("max_catalog_rewrites", 128L);
        limits.put("max_composite_members", 1_024L);
        limits.put("max_concurrent_fetches", 8L);
        limits.put("max_decompressed_bytes", 8_589_934_592L);
        limits.put("max_delta_entries", 10_000_000L);
        limits.put("max_diagnostics", 10_000L);
        limits.put("max_disk_cache_bytes", 68_719_476_736L);
        limits.put("max_documents", 1_000L);
        limits.put("max_import_depth", 128L);
        limits.put("max_index_bytes", 17_179_869_184L);
        limits.put("max_index_rows", 500_000_000L);
        limits.put("max_iri_bytes", 1_048_576L);
        limits.put("max_literal_bytes", 67_108_864L);
        limits.put("max_memory_bytes", null);
        limits.put("max_nesting_depth", 512L);
        limits.put("max_origin_entries", 100_000_000L);
        limits.put("max_overlay_depth", 32L);
        limits.put("max_prefixes", 1_000_000L);
        limits.put("max_rdf_list_length", 10_000_000L);
        limits.put("max_redirects", 5L);
        limits.put("max_resolver_attempts", 10_000L);
        limits.put("max_rule_atoms", 10_000_000L);
        limits.put("max_sequence_arity", 10_000_000L);
        limits.put("max_source_bytes", 2_147_483_648L);
        limits.put("max_source_map_entries", 100_000_000L);
        limits.put("max_strings", 500_000_000L);
        limits.put("max_temporary_bytes", 17_179_869_184L);
        limits.put("max_terms", 500_000_000L);
        limits.put("max_total_source_bytes", 8_589_934_592L);
        limits.put("max_triples", 100_000_000L);
        limits.put("max_wire_bytes", 17_179_869_184L);
        limits.put("max_wire_rows", 500_000_000L);
        Map<String, Object> options = new TreeMap<>();
        options.put("collect_provenance", true);
        options.put("deterministic", true);
        options.put("format", format);
        options.put("imports", "record_unresolved");
        options.put("limits", limits);
        options.put("offline", true);
        options.put("preserve_source_map", false);
        options.put("validate_owl2_dl", false);
        return options;
    }

    private static Map<String, Object> runRequest(
            AdapterRequest request, String protocolMode) {
        return executePrepared(prepareRequest(request, protocolMode));
    }

    private static PreparedExecution prepareRequest(
            AdapterRequest request, String protocolMode) {
        Map<String, Object> fallback = requestIdentity(request);
        try {
            return PreparedExecution.ready(fallback, validate(request, protocolMode));
        } catch (RuntimeException error) {
            return PreparedExecution.complete(
                    fallbackStatus(fallback, "error", safeReason(error)));
        }
    }

    private static Map<String, Object> executePrepared(PreparedExecution prepared) {
        if (prepared.completed != null) {
            return prepared.completed;
        }
        try {
            return execute(prepared.request);
        } catch (ModelMapper.IneligibleException error) {
            return status(prepared.request, "ineligible", error.getMessage());
        } catch (RuntimeException | IOException error) {
            return fallbackStatus(prepared.fallback, "error", safeReason(error));
        }
    }

    private static Map<String, Object> execute(ValidatedRequest request) throws IOException {
        Path prepared = null;
        if ("file".equals(request.inputMode)) {
            prepared = prepareFile(request);
        }
        long temporaryBytes = prepared == null ? 0 : request.source.length;
        try {
            NativeUsage.Snapshot before = NativeUsage.snapshot();
            long wallStarted = System.nanoTime();
            long loadStarted = System.nanoTime();
            OWLOntology ontology = load(request, prepared);
            long loadNs = elapsed(loadStarted);
            long commonStarted = System.nanoTime();
            ModelMapper.MappedDocument mapped = new ModelMapper().map(
                    ontology, request.format.value);
            CommonContract.Build built = CommonContract.build(mapped,
                    new CommonContract.RequestContext(
                            request.corpusId, request.source, request.sourceSha256,
                            request.documentIri, request.format.value, request.optionsSha256));
            long commonNs = elapsed(commonStarted);
            long objectCount = Math.addExact(
                    ontology.getAxiomCount(),
                    Math.addExact(ontology.annotations().count(), mapped.signature.size()));
            Map<String, Object> phase = object(
                    "common_contract", Math.max(0L, commonNs - built.validationNs),
                    "contract_validation", built.validationNs,
                    "owlapi_engine_load", loadNs);
            Map<String, Object> metrics = object(
                    "common_adapter_ns", commonNs,
                    "cpu_ns", 0L,
                    "load_ns", loadNs,
                    "object_count", objectCount,
                    "phase_ns", phase,
                    "rss_peak_after_bytes", before.peakRssBytes,
                    "rss_peak_before_bytes", before.peakRssBytes,
                    "rss_peak_increment_bytes", 0L,
                    "temporary_bytes", temporaryBytes,
                    "wall_ns", 0L);
            if ("fresh-process".equals(request.processMode)) {
                metrics.put("startup_to_ready_cpu_ns", 0L);
            }
            Map<String, Object> validation = object(
                    "contract_sha256", built.contract.get("contract_sha256"),
                    "full_contract_validation", true,
                    "inside_timed_envelope", true,
                    "schema", VALIDATION_SCHEMA,
                    "validation_ns", built.validationNs);
            Map<String, Object> result = object(
                    "artifact", artifact(),
                    "boundary", BOUNDARY,
                    "contract", built.contract,
                    "corpus_id", request.corpusId,
                    "implementation", IMPLEMENTATION,
                    "input_mode", request.inputMode,
                    "lane", LANE,
                    "metrics", metrics,
                    "options_sha256", request.optionsSha256,
                    "process_mode", request.processMode,
                    "raw_inventory", null,
                    "reason", null,
                    "schema", RESULT_SCHEMA,
                    "source_sha256", request.sourceSha256,
                    "status", "ok",
                    "timed_validation", validation);
            return finishReadyResultMetrics(
                    result, metrics, before, wallStarted, NativeUsage::snapshot);
        } finally {
            if (prepared != null) {
                Files.deleteIfExists(prepared);
            }
        }
    }

    static Map<String, Object> finishReadyResultMetrics(
            Map<String, Object> result,
            Map<String, Object> metrics,
            NativeUsage.Snapshot before,
            long wallStarted,
            Supplier<NativeUsage.Snapshot> snapshot) {
        if (result == null
                || metrics == null
                || result.get("metrics") != metrics
                || !(result.get("artifact") instanceof Map)
                || !(result.get("contract") instanceof Map)
                || before == null
                || snapshot == null) {
            throw new IllegalArgumentException(
                    "ready result must be fully constructed before usage capture");
        }
        boolean freshProcess = "fresh-process".equals(result.get("process_mode"));
        if (freshProcess != metrics.containsKey("startup_to_ready_cpu_ns")) {
            throw new IllegalArgumentException(
                    "startup-to-ready CPU slot must be reserved before usage capture");
        }
        NativeUsage.Snapshot after = snapshot.get();
        if (after == null) {
            throw new IllegalArgumentException("ready result usage capture is missing");
        }
        long cpuNs = Math.max(0L, after.cpuNs - before.cpuNs);
        metrics.put("cpu_ns", cpuNs);
        if (freshProcess) {
            if (after.cpuNs < 0 || after.cpuNs < cpuNs) {
                throw new IllegalArgumentException(
                        "fresh startup-to-ready CPU evidence differs");
            }
            metrics.put("startup_to_ready_cpu_ns", after.cpuNs);
        }
        metrics.put("rss_peak_after_bytes", after.peakRssBytes);
        metrics.put("rss_peak_before_bytes", before.peakRssBytes);
        metrics.put(
                "rss_peak_increment_bytes",
                Math.max(0L, after.peakRssBytes - before.peakRssBytes));
        metrics.put("wall_ns", elapsed(wallStarted));
        return result;
    }

    private static OWLOntology load(ValidatedRequest request, Path prepared) throws IOException {
        OWLOntologyManager manager = OWLManager.createOWLOntologyManager();
        manager.getIRIMappers().add(importIri -> IRI.create(
                "file:/pyowl-core-owlapi-offline/" + Canonical.hex(
                        Canonical.sha256(importIri.toString().getBytes(StandardCharsets.UTF_8)))));
        OWLOntologyLoaderConfiguration configuration = new OWLOntologyLoaderConfiguration()
                .setMissingImportHandlingStrategy(MissingImportHandlingStrategy.SILENT)
                .setConnectionTimeout(1)
                .setRetriesToAttempt(0);
        try (InputStream input = prepared == null
                ? new ByteArrayInputStream(request.source)
                : Files.newInputStream(prepared, StandardOpenOption.READ)) {
            StreamDocumentSource source = new StreamDocumentSource(
                    input, IRI.create(request.documentIri), request.format.documentFormat(), null);
            return manager.loadOntologyFromOntologyDocument(source, configuration);
        } catch (Exception error) {
            throw new IOException("OWLAPI parse failed: " + safeReason(error), error);
        }
    }

    private static Path prepareFile(ValidatedRequest request) throws IOException {
        String prefix = "pyowl-core-owlapi-" + ProcessHandle.current().pid() + "-"
                + TEMP_COUNTER.getAndIncrement() + "-" + request.sourceSha256.substring(0, 16) + "-";
        Path path = Files.createTempFile(prefix, "." + request.format.value);
        boolean complete = false;
        try {
            Files.write(path, request.source, StandardOpenOption.TRUNCATE_EXISTING);
            complete = true;
            return path;
        } finally {
            if (!complete) {
                Files.deleteIfExists(path);
            }
        }
    }

    private static Map<String, Object> status(
            ValidatedRequest request, String status, String reason) {
        return fallbackStatus(object(
                "corpus_id", request.corpusId,
                "input_mode", request.inputMode,
                "options_sha256", request.optionsSha256,
                "process_mode", request.processMode,
                "source_sha256", request.sourceSha256), status, reason);
    }

    private static Map<String, Object> fallbackStatus(
            Map<String, Object> identity, String status, String reason) {
        return object(
                "artifact", artifact(),
                "boundary", BOUNDARY,
                "contract", null,
                "corpus_id", identity.get("corpus_id"),
                "implementation", IMPLEMENTATION,
                "input_mode", identity.get("input_mode"),
                "lane", LANE,
                "metrics", object(),
                "options_sha256", identity.get("options_sha256"),
                "process_mode", identity.get("process_mode"),
                "raw_inventory", null,
                "reason", boundedReason(reason),
                "schema", RESULT_SCHEMA,
                "source_sha256", identity.get("source_sha256"),
                "status", status,
                "timed_validation", null);
    }

    private static Map<String, Object> artifact() {
        return object(
                "allocator", ALLOCATOR,
                "artifact", ARTIFACT,
                "artifact_sha256", ARTIFACT_SHA256,
                "features", FEATURES,
                "pin_state", "complete",
                "revision", REVISION,
                "runner_revision", RUNNER_REVISION,
                "runner_sha256", runnerSha256(),
                "thread_ceiling", THREAD_CEILING,
                "version", VERSION);
    }

    private static String runnerSha256() {
        String value = System.getProperty("pyowl.runner.sha256");
        if (!isSha256(value)) {
            throw new IllegalStateException("launcher did not authenticate its SHA-256");
        }
        return value;
    }

    private static Map<String, Object> requestIdentity(AdapterRequest request) {
        if (request == null) {
            return fallbackIdentity();
        }
        return object(
                "corpus_id", request.corpusId == null || request.corpusId.isEmpty()
                        ? "invalid-request" : request.corpusId,
                "input_mode", ("resident-bytes".equals(request.inputMode)
                        || "file".equals(request.inputMode))
                        ? request.inputMode : "resident-bytes",
                "options_sha256", isSha256(request.optionsSha256)
                        ? request.optionsSha256 : zeroDigest(),
                "process_mode", ("fresh-process".equals(request.processMode)
                        || "steady-process".equals(request.processMode))
                        ? request.processMode : "fresh-process",
                "source_sha256", isSha256(request.sourceSha256)
                        ? request.sourceSha256 : zeroDigest());
    }

    private static Map<String, Object> fallbackIdentity() {
        return object(
                "corpus_id", "invalid-request",
                "input_mode", "resident-bytes",
                "options_sha256", zeroDigest(),
                "process_mode", "fresh-process",
                "source_sha256", zeroDigest());
    }

    private static void freshMain() throws IOException {
        long pid = ProcessHandle.current().pid();
        JsonNode request = decodeJson(readFrame(System.in, MAX_REQUEST_FRAME_BYTES));
        validateFreshRequest(request);
        Map<String, Object> result =
                runRequest(decodeAdapterRequest(request.get("request")), "fresh");
        String ontologyInstanceId = freshOntologyInstanceId(pid);
        writeFrame(freshCompletedFrame(0, pid, ontologyInstanceId));
        JsonNode publish = decodeJson(readFrame(System.in, MAX_CONTROL_FRAME_BYTES));
        validateFreshPublish(publish, pid, ontologyInstanceId);
        requireFreshEndOfInput(System.in);
        writeFrame(freshResponseFrame(0, ontologyInstanceId, result));
    }

    private static void persistentMain() throws IOException {
        long pid = ProcessHandle.current().pid();
        writeFrame(object(
                "artifact", artifact(),
                "boundary", BOUNDARY,
                "fresh_ontology_per_request", true,
                "implementation", IMPLEMENTATION,
                "lane", LANE,
                "pid", pid,
                "protocol", PROTOCOL_SCHEMA,
                "request_schema", REQUEST_SCHEMA,
                "prepared_schema", PREPARED_SCHEMA,
                "execute_schema", EXECUTE_SCHEMA,
                "completed_schema", COMPLETED_SCHEMA,
                "publish_schema", PUBLISH_SCHEMA,
                "result_schema", RESULT_SCHEMA,
                "schema", HANDSHAKE_SCHEMA));
        long expectedSequence = 0;
        long instanceCounter = 0;
        while (true) {
            byte[] payload = readFrame(System.in, MAX_REQUEST_FRAME_BYTES);
            JsonNode node = decodeJson(payload);
            if (SHUTDOWN_SCHEMA.equals(text(node, "schema"))) {
                validatePersistentShutdown(node, expectedSequence);
                writeFrame(object(
                        "pid", pid,
                        "protocol", PROTOCOL_SCHEMA,
                        "schema", SHUTDOWN_ACK_SCHEMA,
                        "sequence", expectedSequence));
                return;
            }
            validatePersistentRequestEnvelope(node, expectedSequence);
            long sequence = node.get("sequence").longValue();
            PreparedExecution prepared = prepareRequest(
                    decodeAdapterRequest(node.get("request")), "persistent");
            writeFrame(object(
                    "pid", pid,
                    "protocol", PROTOCOL_SCHEMA,
                    "schema", PREPARED_SCHEMA,
                    "sequence", sequence));
            JsonNode execute = decodeJson(readFrame(System.in, MAX_CONTROL_FRAME_BYTES));
            validatePersistentExecute(execute, sequence, pid);
            Map<String, Object> result = executePrepared(prepared);
            String instance = pid + ":" + instanceCounter + ":" + sequence;
            String ontologyInstanceId = Canonical.hex(Canonical.sha256(
                    instance.getBytes(StandardCharsets.UTF_8)));
            writeFrame(persistentCompletedFrame(sequence, pid, ontologyInstanceId));
            JsonNode publish = decodeJson(readFrame(System.in, MAX_CONTROL_FRAME_BYTES));
            validatePersistentPublish(publish, sequence, pid, ontologyInstanceId);
            writeFrame(object(
                    "ontology_instance_id", ontologyInstanceId,
                    "protocol", PROTOCOL_SCHEMA,
                    "result", result,
                    "schema", PERSISTENT_RESPONSE_SCHEMA,
                    "sequence", sequence));
            instanceCounter = Math.addExact(instanceCounter, 1);
            expectedSequence = Math.addExact(expectedSequence, 1);
        }
    }

    static AdapterRequest decodeAdapterRequest(JsonNode node) throws IOException {
        return JSON.treeToValue(node, AdapterRequest.class);
    }

    static JsonNode decodeJson(byte[] payload) throws IOException {
        return JSON.readTree(payload);
    }

    static String freshOntologyInstanceId(long pid) {
        if (pid < 0) {
            throw new IllegalArgumentException("fresh ontology PID differs");
        }
        String instance = pid + ":0:0";
        return Canonical.hex(Canonical.sha256(instance.getBytes(StandardCharsets.UTF_8)));
    }

    static void validateFreshRequest(JsonNode node) {
        if (node == null
                || !node.isObject()
                || node.size() != 4
                || !node.has("schema")
                || !node.has("protocol")
                || !node.has("sequence")
                || !node.has("request")
                || !node.get("request").isObject()) {
            throw new IllegalArgumentException(
                    "fresh request fields differ from schema v1");
        }
        requireEqual("fresh request schema", text(node, "schema"), FRESH_REQUEST_SCHEMA);
        requireEqual("fresh request protocol", text(node, "protocol"), FRESH_PROTOCOL_SCHEMA);
        requireUnsignedLong("fresh request sequence", node.get("sequence"), 0);
    }

    static Map<String, Object> freshCompletedFrame(
            long sequence, long pid, String ontologyInstanceId) {
        if (sequence != 0 || pid < 0 || !isSha256(ontologyInstanceId)) {
            throw new IllegalArgumentException("fresh completion identity differs");
        }
        return object(
                "ontology_instance_id", ontologyInstanceId,
                "pid", pid,
                "protocol", FRESH_PROTOCOL_SCHEMA,
                "schema", FRESH_COMPLETED_SCHEMA,
                "sequence", sequence);
    }

    static void validateFreshPublish(
            JsonNode node, long pid, String ontologyInstanceId) {
        if (node == null
                || !node.isObject()
                || node.size() != 5
                || !node.has("schema")
                || !node.has("protocol")
                || !node.has("sequence")
                || !node.has("pid")
                || !node.has("ontology_instance_id")) {
            throw new IllegalArgumentException(
                    "fresh publish fields differ from schema v1");
        }
        if (!isSha256(ontologyInstanceId)) {
            throw new IllegalArgumentException(
                    "fresh publish expected ontology instance id differs");
        }
        requireEqual("fresh publish schema", text(node, "schema"), FRESH_PUBLISH_SCHEMA);
        requireEqual("fresh publish protocol", text(node, "protocol"), FRESH_PROTOCOL_SCHEMA);
        requireUnsignedLong("fresh publish sequence", node.get("sequence"), 0);
        requireUnsignedLong("fresh publish pid", node.get("pid"), pid);
        requireEqual(
                "fresh publish ontology instance id",
                text(node, "ontology_instance_id"),
                ontologyInstanceId);
    }

    static void requireFreshEndOfInput(InputStream input) throws IOException {
        if (input == null || input.read() != -1) {
            throw new IOException("fresh input has trailing bytes");
        }
    }

    static Map<String, Object> freshResponseFrame(
            long sequence, String ontologyInstanceId, Map<String, Object> result) {
        if (sequence != 0 || !isSha256(ontologyInstanceId) || result == null) {
            throw new IllegalArgumentException("fresh response identity differs");
        }
        return object(
                "ontology_instance_id", ontologyInstanceId,
                "protocol", FRESH_PROTOCOL_SCHEMA,
                "result", result,
                "schema", FRESH_RESPONSE_SCHEMA,
                "sequence", sequence);
    }

    static void validatePersistentRequestEnvelope(JsonNode node, long sequence) {
        if (node == null
                || !node.isObject()
                || node.size() != 4
                || !node.has("schema")
                || !node.has("protocol")
                || !node.has("sequence")
                || !node.has("request")
                || !node.get("request").isObject()) {
            throw new IllegalArgumentException(
                    "persistent request fields differ from schema v3");
        }
        requireEqual(
                "persistent request schema",
                text(node, "schema"),
                PERSISTENT_REQUEST_SCHEMA);
        requireEqual(
                "persistent request protocol",
                text(node, "protocol"),
                PROTOCOL_SCHEMA);
        requireUnsignedLong("persistent request sequence", node.get("sequence"), sequence);
    }

    static void validatePersistentShutdown(JsonNode node, long sequence) {
        if (node == null
                || !node.isObject()
                || node.size() != 3
                || !node.has("schema")
                || !node.has("protocol")
                || !node.has("sequence")) {
            throw new IllegalArgumentException(
                    "persistent shutdown fields differ from schema v3");
        }
        requireEqual(
                "persistent shutdown schema",
                text(node, "schema"),
                SHUTDOWN_SCHEMA);
        requireEqual(
                "persistent shutdown protocol",
                text(node, "protocol"),
                PROTOCOL_SCHEMA);
        requireUnsignedLong("persistent shutdown sequence", node.get("sequence"), sequence);
    }

    static void validatePersistentExecute(JsonNode node, long sequence, long pid) {
        if (node == null
                || !node.isObject()
                || node.size() != 4
                || !node.has("schema")
                || !node.has("protocol")
                || !node.has("sequence")
                || !node.has("pid")) {
            throw new IllegalArgumentException(
                    "persistent execute fields differ from schema v1");
        }
        requireEqual("persistent execute schema", text(node, "schema"), EXECUTE_SCHEMA);
        requireEqual("persistent execute protocol", text(node, "protocol"), PROTOCOL_SCHEMA);
        requireUnsignedLong("persistent execute sequence", node.get("sequence"), sequence);
        requireUnsignedLong("persistent execute pid", node.get("pid"), pid);
    }

    static Map<String, Object> persistentCompletedFrame(
            long sequence, long pid, String ontologyInstanceId) {
        if (sequence < 0 || pid < 0 || !isSha256(ontologyInstanceId)) {
            throw new IllegalArgumentException("persistent completion identity differs");
        }
        return object(
                "ontology_instance_id", ontologyInstanceId,
                "pid", pid,
                "protocol", PROTOCOL_SCHEMA,
                "schema", COMPLETED_SCHEMA,
                "sequence", sequence);
    }

    static void validatePersistentPublish(
            JsonNode node, long sequence, long pid, String ontologyInstanceId) {
        if (node == null
                || !node.isObject()
                || node.size() != 5
                || !node.has("schema")
                || !node.has("protocol")
                || !node.has("sequence")
                || !node.has("pid")
                || !node.has("ontology_instance_id")) {
            throw new IllegalArgumentException(
                    "persistent publish fields differ from schema v1");
        }
        if (!isSha256(ontologyInstanceId)) {
            throw new IllegalArgumentException(
                    "persistent publish expected ontology instance id differs");
        }
        requireEqual("persistent publish schema", text(node, "schema"), PUBLISH_SCHEMA);
        requireEqual("persistent publish protocol", text(node, "protocol"), PROTOCOL_SCHEMA);
        requireUnsignedLong("persistent publish sequence", node.get("sequence"), sequence);
        requireUnsignedLong("persistent publish pid", node.get("pid"), pid);
        requireEqual(
                "persistent publish ontology instance id",
                text(node, "ontology_instance_id"),
                ontologyInstanceId);
    }

    private static void requireUnsignedLong(String name, JsonNode node, long expected) {
        if (expected < 0
                || node == null
                || !node.isIntegralNumber()
                || !node.canConvertToLong()
                || node.longValue() < 0
                || node.longValue() != expected) {
            throw new IllegalArgumentException(name + " differs");
        }
    }

    private static byte[] readFrame(InputStream input, int maximum) throws IOException {
        if (maximum < 1 || maximum > MAX_REQUEST_FRAME_BYTES) {
            throw new IllegalArgumentException("comparator frame limit is invalid");
        }
        ByteArrayOutputStream header = new ByteArrayOutputStream();
        while (true) {
            int value = input.read();
            if (value < 0) {
                throw new EOFException("persistent runner stdin closed");
            }
            if (value == '\n') {
                break;
            }
            if (header.size() >= MAX_FRAME_HEADER_BYTES - 1 || value < '0' || value > '9') {
                throw new IOException("persistent frame header is invalid");
            }
            header.write(value);
        }
        String digits = header.toString(StandardCharsets.US_ASCII);
        if (digits.isEmpty() || (digits.length() > 1 && digits.charAt(0) == '0')) {
            throw new IOException("persistent frame length is noncanonical");
        }
        int length;
        try {
            length = Integer.parseInt(digits);
        } catch (NumberFormatException error) {
            throw new IOException("persistent frame length is invalid", error);
        }
        if (length < 1 || length > maximum) {
            throw new IOException("comparator frame length exceeds limit");
        }
        byte[] payload = input.readNBytes(length);
        if (payload.length != length || input.read() != '\n') {
            throw new IOException("persistent frame is truncated");
        }
        return payload;
    }

    private static void writeFrame(Object value) throws IOException {
        byte[] payload = JSON.writeValueAsBytes(value);
        OutputStream output = System.out;
        output.write(Integer.toString(payload.length).getBytes(StandardCharsets.US_ASCII));
        output.write('\n');
        output.write(payload);
        output.write('\n');
        output.flush();
    }

    private static void verifyEnvironment(String protocolMode) {
        requireEqual("runner lane", System.getenv("PYOWL_CORE_COMPARATOR_LANE"), LANE);
        requireEqual("runner implementation",
                System.getenv("PYOWL_CORE_COMPARATOR_IMPLEMENTATION"), IMPLEMENTATION);
        requireEqual("runner boundary",
                System.getenv("PYOWL_CORE_COMPARATOR_BOUNDARY"), BOUNDARY);
        if (!Set.of("fresh", "persistent").contains(protocolMode)) {
            throw new IllegalArgumentException("runner protocol mode is unsupported");
        }
        for (String name : List.of(
                "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS", "OMP_NUM_THREADS",
                "OPENBLAS_NUM_THREADS", "RAYON_NUM_THREADS", "TOKIO_WORKER_THREADS")) {
            requireEqual("runner environment " + name, System.getenv(name), "1");
        }
        requireRuntime();
        runnerSha256();
    }

    private static void requireRuntime() {
        String version = System.getProperty("java.version", "");
        String vendor = System.getProperty("java.vendor", "");
        if (!version.startsWith("21.0.7")
                || !(vendor.contains("Adoptium") || vendor.contains("Temurin"))) {
            throw new IllegalStateException(
                    "OWLAPI runner requires Eclipse Temurin 21.0.7+6");
        }
        List<String> arguments = ManagementFactory.getRuntimeMXBean().getInputArguments();
        for (String required : List.of(
                "-Xms8g", "-Xmx8g", "-XX:+UseG1GC", "-XX:+AlwaysPreTouch",
                "-XX:ActiveProcessorCount=1")) {
            if (!arguments.contains(required)) {
                throw new IllegalStateException("JVM argument differs from pin: " + required);
            }
        }
    }

    private static void requireEqual(String name, Object observed, Object expected) {
        if (expected == null ? observed != null : !expected.equals(observed)) {
            throw new IllegalArgumentException(name + " differs from runner pin");
        }
    }

    private static String text(JsonNode node, String field) {
        JsonNode value = node == null ? null : node.get(field);
        return value == null || !value.isTextual() ? null : value.textValue();
    }

    private static boolean isSha256(String value) {
        return value != null && value.matches("[0-9a-f]{64}");
    }

    private static String zeroDigest() {
        return "0000000000000000000000000000000000000000000000000000000000000000";
    }

    private static long elapsed(long started) {
        return Math.max(0L, System.nanoTime() - started);
    }

    private static String boundedReason(String reason) {
        if (reason == null || reason.isEmpty()) {
            return "external comparator failed";
        }
        StringBuilder output = new StringBuilder();
        reason.codePoints()
                .filter(value -> !Character.isISOControl(value))
                .limit(MAX_REASON_CHARS)
                .forEach(output::appendCodePoint);
        return output.length() == 0 ? "external comparator failed" : output.toString();
    }

    private static String safeReason(Throwable error) {
        String message = error.getMessage();
        return boundedReason(message == null ? error.getClass().getSimpleName() : message);
    }

    private static Map<String, Object> object(Object... fields) {
        Map<String, Object> output = new TreeMap<>();
        if ((fields.length & 1) != 0) {
            throw new IllegalArgumentException("object fields must be key/value pairs");
        }
        for (int index = 0; index < fields.length; index += 2) {
            output.put((String) fields[index], fields[index + 1]);
        }
        return output;
    }

    private static void run() throws IOException {
        Thread.setDefaultUncaughtExceptionHandler((thread, error) -> {});
        String protocolMode = System.getenv("PYOWL_CORE_COMPARATOR_PROTOCOL_MODE");
        verifyEnvironment(protocolMode);
        if ("fresh".equals(protocolMode)) {
            freshMain();
        } else {
            persistentMain();
        }
    }

    public static void main(String[] arguments) {
        try {
            run();
        } catch (Throwable error) {
            System.err.println(safeReason(error));
            System.exit(1);
        }
    }
}
