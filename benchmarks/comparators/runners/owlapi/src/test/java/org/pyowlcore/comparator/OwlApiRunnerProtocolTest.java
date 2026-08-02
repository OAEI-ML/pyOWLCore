package org.pyowlcore.comparator;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertSame;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.math.BigInteger;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.atomic.AtomicBoolean;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;

import org.junit.jupiter.api.Test;

final class OwlApiRunnerProtocolTest {
    private static final String FRESH_PROTOCOL =
            "pyowl-core/comparator-fresh-runner/v1";
    private static final String FRESH_REQUEST =
            "pyowl-core/comparator-fresh-request/v1";
    private static final String FRESH_COMPLETED =
            "pyowl-core/comparator-fresh-completed/v1";
    private static final String FRESH_PUBLISH =
            "pyowl-core/comparator-fresh-publish/v1";
    private static final String FRESH_RESPONSE =
            "pyowl-core/comparator-fresh-response/v1";
    private static final String PROTOCOL =
            "pyowl-core/comparator-persistent-runner/v3";
    private static final String PERSISTENT_REQUEST =
            "pyowl-core/comparator-persistent-request/v3";
    private static final String EXECUTE =
            "pyowl-core/comparator-persistent-execute/v1";
    private static final String COMPLETED =
            "pyowl-core/comparator-persistent-completed/v1";
    private static final String PUBLISH =
            "pyowl-core/comparator-persistent-publish/v1";
    private static final String SHUTDOWN =
            "pyowl-core/comparator-persistent-shutdown/v3";
    private static final long SEQUENCE = 7;
    private static final long PID = 41;
    private static final String ONTOLOGY_INSTANCE_ID = "a".repeat(64);
    private static final ObjectMapper JSON = new ObjectMapper();

    @Test
    void capturesUsageOnlyAfterReadyResultConstruction() {
        Map<String, Object> metrics = new TreeMap<>();
        metrics.put("startup_to_ready_cpu_ns", 0L);
        Map<String, Object> result = new TreeMap<>();
        result.put("artifact", Map.of("runner_revision", "v5"));
        result.put("contract", Map.of("contract_sha256", ONTOLOGY_INSTANCE_ID));
        result.put("metrics", metrics);
        result.put("process_mode", "fresh-process");
        NativeUsage.Snapshot before = new NativeUsage.Snapshot(100, 1_000);
        AtomicBoolean sampled = new AtomicBoolean();

        Map<String, Object> observed = OwlApiRunner.finishReadyResultMetrics(
                result,
                metrics,
                before,
                System.nanoTime(),
                () -> {
                    assertEquals(metrics, result.get("metrics"));
                    assertEquals(
                            Map.of("runner_revision", "v5"),
                            result.get("artifact"));
                    assertEquals(
                            Map.of("contract_sha256", ONTOLOGY_INSTANCE_ID),
                            result.get("contract"));
                    assertEquals(
                            Long.valueOf(0),
                            metrics.get("startup_to_ready_cpu_ns"));
                    sampled.set(true);
                    return new NativeUsage.Snapshot(160, 1_400);
                });

        assertSame(result, observed);
        assertTrue(sampled.get());
        assertEquals(Long.valueOf(60), metrics.get("cpu_ns"));
        assertEquals(Long.valueOf(160), metrics.get("startup_to_ready_cpu_ns"));
        assertTrue(
                ((Long) metrics.get("startup_to_ready_cpu_ns"))
                        >= ((Long) metrics.get("cpu_ns")));
        assertEquals(Long.valueOf(1_000), metrics.get("rss_peak_before_bytes"));
        assertEquals(Long.valueOf(1_400), metrics.get("rss_peak_after_bytes"));
        assertEquals(Long.valueOf(400), metrics.get("rss_peak_increment_bytes"));
        assertTrue(((Long) metrics.get("wall_ns")) >= 0);
    }

    @Test
    void omitsStartupCpuFromPersistentReadyResults() {
        Map<String, Object> metrics = new TreeMap<>();
        Map<String, Object> result = new TreeMap<>();
        result.put("artifact", Map.of("runner_revision", "v5"));
        result.put("contract", Map.of("contract_sha256", ONTOLOGY_INSTANCE_ID));
        result.put("metrics", metrics);
        result.put("process_mode", "steady-process");

        OwlApiRunner.finishReadyResultMetrics(
                result,
                metrics,
                new NativeUsage.Snapshot(100, 1_000),
                System.nanoTime(),
                () -> new NativeUsage.Snapshot(160, 1_400));

        assertEquals(Long.valueOf(60), metrics.get("cpu_ns"));
        assertFalse(metrics.containsKey("startup_to_ready_cpu_ns"));
    }

    @Test
    void decodesValidAdapterRequestWithoutScalarCoercion() {
        OwlApiRunner.AdapterRequest request =
                assertDoesNotThrow(() -> OwlApiRunner.decodeAdapterRequest(validAdapterRequest()));

        assertEquals("pyowl-core/comparator-adapter-request/v2", request.schema);
        assertEquals(Long.valueOf(1), Long.valueOf(request.expectedThreadCeiling));
        assertEquals(List.of("isolated-java", "common-contract-v2"), request.expectedFeatures);
    }

    @Test
    void decodesRepresentativeCorpusStringAboveJacksonDefault() {
        String encodedSource = "a".repeat(20_100_000);
        byte[] payload = ("{\"source_b64\":\"" + encodedSource + "\"}")
                .getBytes(StandardCharsets.UTF_8);

        JsonNode decoded =
                assertDoesNotThrow(() -> OwlApiRunner.decodeJson(payload));

        assertEquals(encodedSource.length(), decoded.get("source_b64").textValue().length());
    }

    @Test
    void rejectsAdapterRequestScalarCoercion() {
        ObjectNode numericSchema = validAdapterRequest();
        numericSchema.put("schema", 1);
        assertThrows(
                IOException.class,
                () -> OwlApiRunner.decodeAdapterRequest(numericSchema));

        ObjectNode floatThreadCeiling = validAdapterRequest();
        floatThreadCeiling.put("expected_thread_ceiling", 1.0);
        assertThrows(
                IOException.class,
                () -> OwlApiRunner.decodeAdapterRequest(floatThreadCeiling));

        ObjectNode stringThreadCeiling = validAdapterRequest();
        stringThreadCeiling.put("expected_thread_ceiling", "1");
        assertThrows(
                IOException.class,
                () -> OwlApiRunner.decodeAdapterRequest(stringThreadCeiling));
    }

    @Test
    void acceptsExactFreshRequestPublishAndResponseFrames() {
        assertDoesNotThrow(() -> OwlApiRunner.validateFreshRequest(validFreshRequest()));
        assertEquals(
                "81a36d54d56bcb559f068c56c07fac0d8484b00fb9de41d6daf7a47c531f771b",
                OwlApiRunner.freshOntologyInstanceId(PID));

        Map<String, Object> completed =
                OwlApiRunner.freshCompletedFrame(0, PID, ONTOLOGY_INSTANCE_ID);
        assertEquals(
                Set.of("schema", "protocol", "sequence", "pid", "ontology_instance_id"),
                completed.keySet());
        assertEquals(FRESH_COMPLETED, completed.get("schema"));
        assertEquals(FRESH_PROTOCOL, completed.get("protocol"));
        assertEquals(Long.valueOf(0), completed.get("sequence"));
        assertEquals(Long.valueOf(PID), completed.get("pid"));
        assertEquals(ONTOLOGY_INSTANCE_ID, completed.get("ontology_instance_id"));

        assertDoesNotThrow(() -> OwlApiRunner.validateFreshPublish(
                validFreshPublish(), PID, ONTOLOGY_INSTANCE_ID));

        Map<String, Object> result = Map.of("status", "ok");
        Map<String, Object> response =
                OwlApiRunner.freshResponseFrame(0, ONTOLOGY_INSTANCE_ID, result);
        assertEquals(
                Set.of("schema", "protocol", "sequence", "ontology_instance_id", "result"),
                response.keySet());
        assertEquals(FRESH_RESPONSE, response.get("schema"));
        assertEquals(FRESH_PROTOCOL, response.get("protocol"));
        assertEquals(Long.valueOf(0), response.get("sequence"));
        assertEquals(ONTOLOGY_INSTANCE_ID, response.get("ontology_instance_id"));
        assertEquals(result, response.get("result"));
    }

    @Test
    void rejectsNonExactFreshRequestFrames() {
        List<JsonNode> invalid = new ArrayList<>();
        invalid.add(JSON.createArrayNode());
        invalid.add(changed(validFreshRequest(), "schema", "wrong"));
        invalid.add(changed(validFreshRequest(), "protocol", "wrong"));
        invalid.add(changed(validFreshRequest(), "sequence", 1));
        invalid.add(changed(validFreshRequest(), "sequence", -1));
        invalid.add(changed(validFreshRequest(), "sequence", 0.0));
        invalid.add(changed(validFreshRequest(), "sequence", true));
        invalid.add(changed(
                validFreshRequest(),
                "sequence",
                new BigInteger("18446744073709551615")));
        invalid.add(without(validFreshRequest(), "schema"));
        invalid.add(without(validFreshRequest(), "protocol"));
        invalid.add(without(validFreshRequest(), "sequence"));
        invalid.add(without(validFreshRequest(), "request"));
        ObjectNode nullRequest = validFreshRequest();
        nullRequest.putNull("request");
        invalid.add(nullRequest);
        ObjectNode scalarRequest = validFreshRequest();
        scalarRequest.put("request", "wrong");
        invalid.add(scalarRequest);
        ObjectNode extra = validFreshRequest();
        extra.put("extra", true);
        invalid.add(extra);

        for (JsonNode frame : invalid) {
            assertThrows(
                    IllegalArgumentException.class,
                    () -> OwlApiRunner.validateFreshRequest(frame));
        }
    }

    @Test
    void rejectsInvalidFreshCompletedAndResponseIdentity() {
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.freshOntologyInstanceId(-1));
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.freshCompletedFrame(
                        1, PID, ONTOLOGY_INSTANCE_ID));
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.freshCompletedFrame(
                        0, -1, ONTOLOGY_INSTANCE_ID));
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.freshCompletedFrame(0, PID, "not-a-token"));
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.freshResponseFrame(
                        1, ONTOLOGY_INSTANCE_ID, Map.of()));
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.freshResponseFrame(0, "not-a-token", Map.of()));
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.freshResponseFrame(0, ONTOLOGY_INSTANCE_ID, null));
    }

    @Test
    void rejectsAdversarialFreshPublishFrames() {
        List<JsonNode> invalid = new ArrayList<>();
        invalid.add(JSON.createArrayNode());
        invalid.add(changed(validFreshPublish(), "schema", "wrong"));
        invalid.add(changed(validFreshPublish(), "protocol", "wrong"));
        invalid.add(changed(validFreshPublish(), "sequence", 1));
        invalid.add(changed(validFreshPublish(), "sequence", -1));
        invalid.add(changed(validFreshPublish(), "sequence", 0.0));
        invalid.add(changed(validFreshPublish(), "sequence", true));
        invalid.add(changed(
                validFreshPublish(),
                "sequence",
                new BigInteger("18446744073709551615")));
        invalid.add(changed(validFreshPublish(), "pid", PID - 1));
        invalid.add(changed(validFreshPublish(), "pid", PID + 1));
        invalid.add(changed(validFreshPublish(), "pid", -1));
        invalid.add(changed(validFreshPublish(), "pid", 41.0));
        invalid.add(changed(validFreshPublish(), "pid", true));
        invalid.add(changed(
                validFreshPublish(),
                "pid",
                new BigInteger("18446744073709551615")));
        invalid.add(changed(
                validFreshPublish(), "ontology_instance_id", "b".repeat(64)));
        invalid.add(changed(validFreshPublish(), "ontology_instance_id", ""));
        invalid.add(changed(validFreshPublish(), "ontology_instance_id", 41L));
        invalid.add(changed(validFreshPublish(), "ontology_instance_id", true));
        ObjectNode nullToken = validFreshPublish();
        nullToken.putNull("ontology_instance_id");
        invalid.add(nullToken);
        for (String field : List.of(
                "schema", "protocol", "sequence", "pid", "ontology_instance_id")) {
            invalid.add(without(validFreshPublish(), field));
        }
        ObjectNode extra = validFreshPublish();
        extra.put("extra", true);
        invalid.add(extra);

        for (JsonNode frame : invalid) {
            assertThrows(
                    IllegalArgumentException.class,
                    () -> OwlApiRunner.validateFreshPublish(
                            frame, PID, ONTOLOGY_INSTANCE_ID));
        }
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.validateFreshPublish(
                        validFreshPublish(), PID, "not-a-token"));
    }

    @Test
    void freshResponseRequiresClosedInputWithoutTrailingBytes() {
        assertDoesNotThrow(() -> OwlApiRunner.requireFreshEndOfInput(
                new ByteArrayInputStream(new byte[0])));
        assertThrows(
                IOException.class,
                () -> OwlApiRunner.requireFreshEndOfInput(
                        new ByteArrayInputStream(new byte[] {'1'})));
        assertThrows(
                IOException.class,
                () -> OwlApiRunner.requireFreshEndOfInput(null));
    }

    @Test
    void acceptsExactRequestAndShutdownFrames() {
        assertDoesNotThrow(
                () -> OwlApiRunner.validatePersistentRequestEnvelope(
                        validPersistentRequest(), SEQUENCE));
        assertDoesNotThrow(
                () -> OwlApiRunner.validatePersistentShutdown(validShutdown(), SEQUENCE));
    }

    @Test
    void rejectsNonExactRequestFrames() {
        List<JsonNode> invalid = new ArrayList<>();
        invalid.add(JSON.createArrayNode());
        invalid.add(changed(validPersistentRequest(), "schema", "wrong"));
        invalid.add(changed(validPersistentRequest(), "protocol", "wrong"));
        invalid.add(changed(validPersistentRequest(), "sequence", SEQUENCE - 1));
        invalid.add(changed(validPersistentRequest(), "sequence", SEQUENCE + 1));
        invalid.add(changed(validPersistentRequest(), "sequence", -1));
        invalid.add(changed(validPersistentRequest(), "sequence", 7.0));
        invalid.add(changed(validPersistentRequest(), "sequence", true));
        invalid.add(changed(validPersistentRequest(),
                "sequence", new BigInteger("18446744073709551615")));
        invalid.add(without(validPersistentRequest(), "schema"));
        invalid.add(without(validPersistentRequest(), "protocol"));
        invalid.add(without(validPersistentRequest(), "sequence"));
        invalid.add(without(validPersistentRequest(), "request"));
        ObjectNode nullRequest = validPersistentRequest();
        nullRequest.putNull("request");
        invalid.add(nullRequest);
        ObjectNode arrayRequest = validPersistentRequest();
        arrayRequest.set("request", JSON.createArrayNode());
        invalid.add(arrayRequest);
        ObjectNode extra = validPersistentRequest();
        extra.put("extra", true);
        invalid.add(extra);

        for (JsonNode frame : invalid) {
            assertThrows(
                    IllegalArgumentException.class,
                    () -> OwlApiRunner.validatePersistentRequestEnvelope(frame, SEQUENCE));
        }
    }

    @Test
    void rejectsNonExactShutdownFrames() {
        List<JsonNode> invalid = new ArrayList<>();
        invalid.add(JSON.createArrayNode());
        invalid.add(changed(validShutdown(), "schema", "wrong"));
        invalid.add(changed(validShutdown(), "protocol", "wrong"));
        invalid.add(changed(validShutdown(), "sequence", SEQUENCE - 1));
        invalid.add(changed(validShutdown(), "sequence", SEQUENCE + 1));
        invalid.add(changed(validShutdown(), "sequence", -1));
        invalid.add(changed(validShutdown(), "sequence", 7.0));
        invalid.add(changed(validShutdown(), "sequence", true));
        invalid.add(changed(validShutdown(),
                "sequence", new BigInteger("18446744073709551615")));
        invalid.add(without(validShutdown(), "schema"));
        invalid.add(without(validShutdown(), "protocol"));
        invalid.add(without(validShutdown(), "sequence"));
        ObjectNode extra = validShutdown();
        extra.put("extra", true);
        invalid.add(extra);

        for (JsonNode frame : invalid) {
            assertThrows(
                    IllegalArgumentException.class,
                    () -> OwlApiRunner.validatePersistentShutdown(frame, SEQUENCE));
        }
    }

    @Test
    void acceptsExactExecuteFrame() {
        assertDoesNotThrow(
                () -> OwlApiRunner.validatePersistentExecute(validExecute(), SEQUENCE, PID));
    }

    @Test
    void rejectsNonExactExecuteFrames() {
        List<JsonNode> invalid = new ArrayList<>();
        invalid.add(JSON.createArrayNode());
        invalid.add(changed(validExecute(), "schema", "wrong"));
        invalid.add(changed(validExecute(), "protocol", "wrong"));
        invalid.add(changed(validExecute(), "sequence", SEQUENCE + 1));
        invalid.add(changed(validExecute(), "pid", PID + 1));
        invalid.add(changed(validExecute(), "sequence", -1));
        invalid.add(changed(validExecute(), "pid", -1));
        invalid.add(changed(validExecute(), "sequence", 7.0));
        invalid.add(changed(validExecute(), "pid", 41.0));
        invalid.add(changed(validExecute(), "sequence", true));
        invalid.add(changed(validExecute(), "pid", true));
        invalid.add(changed(validExecute(),
                "sequence", new BigInteger("18446744073709551615")));
        invalid.add(without(validExecute(), "sequence"));
        invalid.add(without(validExecute(), "pid"));
        ObjectNode extra = validExecute();
        extra.put("extra", true);
        invalid.add(extra);

        for (JsonNode frame : invalid) {
            assertThrows(
                    IllegalArgumentException.class,
                    () -> OwlApiRunner.validatePersistentExecute(frame, SEQUENCE, PID));
        }
    }

    @Test
    void emitsStrictCompletedFrameBoundToSequencePidAndToken() {
        Map<String, Object> frame =
                OwlApiRunner.persistentCompletedFrame(SEQUENCE, PID, ONTOLOGY_INSTANCE_ID);

        assertEquals(
                Set.of("schema", "protocol", "sequence", "pid", "ontology_instance_id"),
                frame.keySet());
        assertEquals(COMPLETED, frame.get("schema"));
        assertEquals(PROTOCOL, frame.get("protocol"));
        assertEquals(Long.valueOf(SEQUENCE), frame.get("sequence"));
        assertEquals(Long.valueOf(PID), frame.get("pid"));
        assertEquals(ONTOLOGY_INSTANCE_ID, frame.get("ontology_instance_id"));
    }

    @Test
    void rejectsInvalidCompletedFrameIdentity() {
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.persistentCompletedFrame(-1, PID, ONTOLOGY_INSTANCE_ID));
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.persistentCompletedFrame(SEQUENCE, -1, ONTOLOGY_INSTANCE_ID));
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.persistentCompletedFrame(SEQUENCE, PID, "not-a-token"));
    }

    @Test
    void acceptsExactPublishFrame() {
        assertDoesNotThrow(() -> OwlApiRunner.validatePersistentPublish(
                validPublish(), SEQUENCE, PID, ONTOLOGY_INSTANCE_ID));
    }

    @Test
    void rejectsPublishSequenceReplaysAndInvalidTypes() {
        assertInvalidPublish(changed(validPublish(), "sequence", SEQUENCE - 1));
        assertInvalidPublish(changed(validPublish(), "sequence", SEQUENCE + 1));
        assertInvalidPublish(changed(validPublish(), "sequence", -1));
        assertInvalidPublish(changed(validPublish(), "sequence", 7.0));
        assertInvalidPublish(changed(validPublish(), "sequence", true));
        assertInvalidPublish(changed(validPublish(),
                "sequence", new BigInteger("18446744073709551615")));
        assertInvalidPublish(without(validPublish(), "sequence"));
    }

    @Test
    void rejectsPublishPidMismatchesAndInvalidTypes() {
        assertInvalidPublish(changed(validPublish(), "pid", PID - 1));
        assertInvalidPublish(changed(validPublish(), "pid", PID + 1));
        assertInvalidPublish(changed(validPublish(), "pid", -1));
        assertInvalidPublish(changed(validPublish(), "pid", 41.0));
        assertInvalidPublish(changed(validPublish(), "pid", true));
        assertInvalidPublish(changed(validPublish(),
                "pid", new BigInteger("18446744073709551615")));
        assertInvalidPublish(without(validPublish(), "pid"));
    }

    @Test
    void rejectsPublishTokenMismatchesAndInvalidTypes() {
        assertInvalidPublish(changed(
                validPublish(), "ontology_instance_id", "b".repeat(64)));
        assertInvalidPublish(changed(validPublish(), "ontology_instance_id", ""));
        assertInvalidPublish(changed(validPublish(), "ontology_instance_id", 41L));
        assertInvalidPublish(changed(validPublish(), "ontology_instance_id", true));
        ObjectNode nullToken = validPublish();
        nullToken.putNull("ontology_instance_id");
        assertInvalidPublish(nullToken);
        assertInvalidPublish(without(validPublish(), "ontology_instance_id"));
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.validatePersistentPublish(
                        validPublish(), SEQUENCE, PID, "not-a-token"));
    }

    @Test
    void rejectsPublishSchemaProtocolAndExtraFields() {
        assertInvalidPublish(JSON.createArrayNode());
        assertInvalidPublish(changed(validPublish(), "schema", "wrong"));
        assertInvalidPublish(changed(validPublish(), "protocol", "wrong"));
        assertInvalidPublish(without(validPublish(), "schema"));
        assertInvalidPublish(without(validPublish(), "protocol"));
        ObjectNode extra = validPublish();
        extra.put("extra", true);
        assertInvalidPublish(extra);
    }

    private static ObjectNode validFreshRequest() {
        ObjectNode frame = JSON.createObjectNode();
        frame.put("schema", FRESH_REQUEST);
        frame.put("protocol", FRESH_PROTOCOL);
        frame.put("sequence", 0);
        frame.set("request", JSON.createObjectNode());
        return frame;
    }

    private static ObjectNode validFreshPublish() {
        ObjectNode frame = JSON.createObjectNode();
        frame.put("schema", FRESH_PUBLISH);
        frame.put("protocol", FRESH_PROTOCOL);
        frame.put("sequence", 0);
        frame.put("pid", PID);
        frame.put("ontology_instance_id", ONTOLOGY_INSTANCE_ID);
        return frame;
    }

    private static ObjectNode validAdapterRequest() {
        ObjectNode request = JSON.createObjectNode();
        request.put("schema", "pyowl-core/comparator-adapter-request/v2");
        request.put("lane", "owlapi-common");
        request.put("implementation", "owlapi");
        request.put("boundary", "common-contract-ready");
        request.put("corpus_id", "valid-common-request");
        request.put("source_b64", "");
        request.put("source_sha256", "a".repeat(64));
        request.put(
                "document_iri",
                "urn:pyowl-core:comparator-source:sha256:" + "a".repeat(64));
        request.put("format", "functional");
        request.put("options_sha256", "b".repeat(64));
        request.set("options", JSON.createObjectNode());
        request.put("input_mode", "resident-bytes");
        request.put("process_mode", "steady-process");
        request.put("expected_artifact_sha256", "c".repeat(64));
        request.putArray("expected_features")
                .add("isolated-java")
                .add("common-contract-v2");
        request.put("expected_allocator", "HotSpot G1GC");
        request.put("expected_thread_ceiling", 1);
        request.put(
                "expected_runner_revision",
                "pyowl-core-owlapi-common-runner-v7");
        request.put("expected_runner_sha256", "d".repeat(64));
        return request;
    }

    private static ObjectNode validPersistentRequest() {
        ObjectNode frame = JSON.createObjectNode();
        frame.put("schema", PERSISTENT_REQUEST);
        frame.put("protocol", PROTOCOL);
        frame.put("sequence", SEQUENCE);
        frame.set("request", JSON.createObjectNode());
        return frame;
    }

    private static ObjectNode validShutdown() {
        ObjectNode frame = JSON.createObjectNode();
        frame.put("schema", SHUTDOWN);
        frame.put("protocol", PROTOCOL);
        frame.put("sequence", SEQUENCE);
        return frame;
    }

    private static ObjectNode validExecute() {
        ObjectNode frame = JSON.createObjectNode();
        frame.put("schema", EXECUTE);
        frame.put("protocol", PROTOCOL);
        frame.put("sequence", SEQUENCE);
        frame.put("pid", PID);
        return frame;
    }

    private static ObjectNode validPublish() {
        ObjectNode frame = JSON.createObjectNode();
        frame.put("schema", PUBLISH);
        frame.put("protocol", PROTOCOL);
        frame.put("sequence", SEQUENCE);
        frame.put("pid", PID);
        frame.put("ontology_instance_id", ONTOLOGY_INSTANCE_ID);
        return frame;
    }

    private static void assertInvalidPublish(JsonNode frame) {
        assertThrows(
                IllegalArgumentException.class,
                () -> OwlApiRunner.validatePersistentPublish(
                        frame, SEQUENCE, PID, ONTOLOGY_INSTANCE_ID));
    }

    private static ObjectNode changed(ObjectNode frame, String field, Object value) {
        frame.set(field, JSON.valueToTree(value));
        return frame;
    }

    private static ObjectNode without(ObjectNode frame, String field) {
        frame.remove(field);
        return frame;
    }
}
