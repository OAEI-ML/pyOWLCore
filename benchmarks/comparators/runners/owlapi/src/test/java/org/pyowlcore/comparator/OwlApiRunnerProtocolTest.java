package org.pyowlcore.comparator;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertThrows;

import java.math.BigInteger;
import java.util.ArrayList;
import java.util.List;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;

import org.junit.jupiter.api.Test;

final class OwlApiRunnerProtocolTest {
    private static final String PROTOCOL =
            "pyowl-core/comparator-persistent-runner/v2";
    private static final String EXECUTE =
            "pyowl-core/comparator-persistent-execute/v1";
    private static final long SEQUENCE = 7;
    private static final long PID = 41;
    private static final ObjectMapper JSON = new ObjectMapper();

    @Test
    void acceptsExactExecuteFrame() {
        assertDoesNotThrow(
                () -> OwlApiRunner.validatePersistentExecute(validExecute(), SEQUENCE, PID));
    }

    @Test
    void rejectsNonExactExecuteFrames() {
        List<JsonNode> invalid = new ArrayList<>();
        invalid.add(JSON.createArrayNode());
        invalid.add(changed("schema", "wrong"));
        invalid.add(changed("protocol", "wrong"));
        invalid.add(changed("sequence", SEQUENCE + 1));
        invalid.add(changed("pid", PID + 1));
        invalid.add(changed("sequence", -1));
        invalid.add(changed("pid", -1));
        invalid.add(changed("sequence", 7.0));
        invalid.add(changed("pid", 41.0));
        invalid.add(changed("sequence", true));
        invalid.add(changed("pid", true));
        invalid.add(changed("sequence", new BigInteger("18446744073709551615")));
        invalid.add(without("sequence"));
        invalid.add(without("pid"));
        ObjectNode extra = validExecute();
        extra.put("extra", true);
        invalid.add(extra);

        for (JsonNode frame : invalid) {
            assertThrows(
                    IllegalArgumentException.class,
                    () -> OwlApiRunner.validatePersistentExecute(frame, SEQUENCE, PID));
        }
    }

    private static ObjectNode validExecute() {
        ObjectNode frame = JSON.createObjectNode();
        frame.put("schema", EXECUTE);
        frame.put("protocol", PROTOCOL);
        frame.put("sequence", SEQUENCE);
        frame.put("pid", PID);
        return frame;
    }

    private static ObjectNode changed(String field, String value) {
        ObjectNode frame = validExecute();
        frame.put(field, value);
        return frame;
    }

    private static ObjectNode changed(String field, long value) {
        ObjectNode frame = validExecute();
        frame.put(field, value);
        return frame;
    }

    private static ObjectNode changed(String field, double value) {
        ObjectNode frame = validExecute();
        frame.put(field, value);
        return frame;
    }

    private static ObjectNode changed(String field, boolean value) {
        ObjectNode frame = validExecute();
        frame.put(field, value);
        return frame;
    }

    private static ObjectNode changed(String field, BigInteger value) {
        ObjectNode frame = validExecute();
        frame.put(field, value);
        return frame;
    }

    private static ObjectNode without(String field) {
        ObjectNode frame = validExecute();
        frame.remove(field);
        return frame;
    }
}
