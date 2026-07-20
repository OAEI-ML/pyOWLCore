package org.pyowlcore.comparator;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Comparator;
import java.util.List;

final class Canonical {
    static final long IRI = 1;
    static final long ENTITY = 2;
    static final long ANONYMOUS_INDIVIDUAL = 3;
    static final long LITERAL = 4;
    static final long ANNOTATION = 5;
    static final long OBJECT_INVERSE_OF = 10;
    static final long OBJECT_PROPERTY_CHAIN = 11;
    static final long FACET_RESTRICTION = 20;
    static final long DATA_INTERSECTION_OF = 21;
    static final long DATA_UNION_OF = 22;
    static final long DATA_COMPLEMENT_OF = 23;
    static final long DATA_ONE_OF = 24;
    static final long DATATYPE_RESTRICTION = 25;
    static final long OBJECT_INTERSECTION_OF = 30;
    static final long OBJECT_UNION_OF = 31;
    static final long OBJECT_COMPLEMENT_OF = 32;
    static final long OBJECT_ONE_OF = 33;
    static final long OBJECT_SOME_VALUES_FROM = 34;
    static final long OBJECT_ALL_VALUES_FROM = 35;
    static final long OBJECT_HAS_VALUE = 36;
    static final long OBJECT_HAS_SELF = 37;
    static final long OBJECT_MIN_CARDINALITY = 38;
    static final long OBJECT_MAX_CARDINALITY = 39;
    static final long OBJECT_EXACT_CARDINALITY = 40;
    static final long DATA_SOME_VALUES_FROM = 41;
    static final long DATA_ALL_VALUES_FROM = 42;
    static final long DATA_HAS_VALUE = 43;
    static final long DATA_MIN_CARDINALITY = 44;
    static final long DATA_MAX_CARDINALITY = 45;
    static final long DATA_EXACT_CARDINALITY = 46;
    static final long DECLARATION = 60;
    static final long SUB_CLASS_OF = 61;
    static final long EQUIVALENT_CLASSES = 62;
    static final long DISJOINT_CLASSES = 63;
    static final long DISJOINT_UNION = 64;
    static final long SUB_OBJECT_PROPERTY_OF = 70;
    static final long EQUIVALENT_OBJECT_PROPERTIES = 71;
    static final long DISJOINT_OBJECT_PROPERTIES = 72;
    static final long INVERSE_OBJECT_PROPERTIES = 73;
    static final long OBJECT_PROPERTY_DOMAIN = 74;
    static final long OBJECT_PROPERTY_RANGE = 75;
    static final long FUNCTIONAL_OBJECT_PROPERTY = 76;
    static final long INVERSE_FUNCTIONAL_OBJECT_PROPERTY = 77;
    static final long REFLEXIVE_OBJECT_PROPERTY = 78;
    static final long IRREFLEXIVE_OBJECT_PROPERTY = 79;
    static final long SYMMETRIC_OBJECT_PROPERTY = 80;
    static final long ASYMMETRIC_OBJECT_PROPERTY = 81;
    static final long TRANSITIVE_OBJECT_PROPERTY = 82;
    static final long SUB_DATA_PROPERTY_OF = 90;
    static final long EQUIVALENT_DATA_PROPERTIES = 91;
    static final long DISJOINT_DATA_PROPERTIES = 92;
    static final long DATA_PROPERTY_DOMAIN = 93;
    static final long DATA_PROPERTY_RANGE = 94;
    static final long FUNCTIONAL_DATA_PROPERTY = 95;
    static final long DATATYPE_DEFINITION = 100;
    static final long HAS_KEY = 101;
    static final long SAME_INDIVIDUAL = 110;
    static final long DIFFERENT_INDIVIDUALS = 111;
    static final long CLASS_ASSERTION = 112;
    static final long OBJECT_PROPERTY_ASSERTION = 113;
    static final long NEGATIVE_OBJECT_PROPERTY_ASSERTION = 114;
    static final long DATA_PROPERTY_ASSERTION = 115;
    static final long NEGATIVE_DATA_PROPERTY_ASSERTION = 116;
    static final long ANNOTATION_ASSERTION = 120;
    static final long SUB_ANNOTATION_PROPERTY_OF = 121;
    static final long ANNOTATION_PROPERTY_DOMAIN = 122;
    static final long ANNOTATION_PROPERTY_RANGE = 123;
    static final long VARIABLE = 140;
    static final long CLASS_ATOM = 141;
    static final long DATA_RANGE_ATOM = 142;
    static final long OBJECT_PROPERTY_ATOM = 143;
    static final long DATA_PROPERTY_ATOM = 144;
    static final long BUILT_IN_ATOM = 145;
    static final long SAME_INDIVIDUAL_ATOM = 146;
    static final long DIFFERENT_INDIVIDUALS_ATOM = 147;
    static final long SWRL_RULE = 148;

    private static final int NONE = 0;
    private static final int NODE = 1;
    private static final int TEXT = 2;
    private static final int BYTES = 3;
    private static final int INTEGER = 4;
    private static final int ENUM = 5;
    private static final int SET = 6;
    private static final int SEQUENCE = 7;
    private static final Comparator<byte[]> UNSIGNED_BYTES = Arrays::compareUnsigned;

    private Canonical() {}

    interface Field {
        void append(ByteArrayOutputStream output);
    }

    static Field none() {
        return output -> output.write(NONE);
    }

    static Field nodeField(byte[] value) {
        byte[] owned = value.clone();
        return output -> {
            output.write(NODE);
            frame(output, owned);
        };
    }

    static Field text(String value) {
        byte[] encoded = value.getBytes(StandardCharsets.UTF_8);
        return output -> {
            output.write(TEXT);
            frame(output, encoded);
        };
    }

    static Field bytes(byte[] value) {
        byte[] owned = value.clone();
        return output -> {
            output.write(BYTES);
            frame(output, owned);
        };
    }

    static Field integer(long value) {
        if (value < 0) {
            throw new IllegalArgumentException("canonical integer must be nonnegative");
        }
        return output -> {
            output.write(INTEGER);
            varint(output, value);
        };
    }

    static Field enumeration(String value) {
        byte[] encoded = value.getBytes(StandardCharsets.US_ASCII);
        return output -> {
            output.write(ENUM);
            frame(output, encoded);
        };
    }

    static Field set(Collection<byte[]> values) {
        List<byte[]> normalized = normalizeSet(values);
        return output -> {
            output.write(SET);
            varint(output, normalized.size());
            normalized.forEach(value -> frame(output, value));
        };
    }

    static Field sequence(Collection<byte[]> values) {
        List<byte[]> owned = copy(values);
        return output -> {
            output.write(SEQUENCE);
            varint(output, owned.size());
            owned.forEach(value -> {
                output.write(NODE);
                frame(output, value);
            });
        };
    }

    static byte[] node(long tag, Field... fields) {
        if (tag < 0) {
            throw new IllegalArgumentException("canonical tag must be nonnegative");
        }
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        varint(output, tag);
        Arrays.stream(fields).forEach(field -> field.append(output));
        return output.toByteArray();
    }

    static byte[] iri(String value) {
        return node(IRI, text(value));
    }

    static byte[] entity(String kind, String value) {
        return node(ENTITY, enumeration(kind), nodeField(iri(value)));
    }

    static byte[] structuralDigest(byte[] value) {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        output.writeBytes("pyowl-core:structural-value:v1\0".getBytes(StandardCharsets.UTF_8));
        varint(output, 1);
        output.writeBytes(value);
        return sha256(output.toByteArray());
    }

    static List<byte[]> normalizeSet(Collection<byte[]> values) {
        List<byte[]> sorted = copy(values);
        sorted.sort(UNSIGNED_BYTES);
        List<byte[]> output = new ArrayList<>(sorted.size());
        byte[] previous = null;
        for (byte[] value : sorted) {
            if (previous == null || !Arrays.equals(previous, value)) {
                output.add(value);
                previous = value;
            }
        }
        return output;
    }

    static void appendCollection(ByteArrayOutputStream output, Collection<byte[]> values) {
        varint(output, values.size());
        values.forEach(value -> frame(output, value));
    }

    static void frame(ByteArrayOutputStream output, byte[] value) {
        varint(output, value.length);
        output.writeBytes(value);
    }

    static byte[] frame(byte[] value) {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        frame(output, value);
        return output.toByteArray();
    }

    static void varint(ByteArrayOutputStream output, long value) {
        if (value < 0) {
            throw new IllegalArgumentException("canonical varint must be nonnegative");
        }
        do {
            int next = (int) (value & 0x7f);
            value >>>= 7;
            output.write(next | (value == 0 ? 0 : 0x80));
        } while (value != 0);
    }

    static byte[] sha256(byte[] value) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(value);
        } catch (NoSuchAlgorithmException impossible) {
            throw new IllegalStateException("SHA-256 is unavailable", impossible);
        }
    }

    static String hex(byte[] value) {
        char[] digits = "0123456789abcdef".toCharArray();
        char[] output = new char[value.length * 2];
        for (int index = 0; index < value.length; index++) {
            int octet = value[index] & 0xff;
            output[index * 2] = digits[octet >>> 4];
            output[index * 2 + 1] = digits[octet & 0x0f];
        }
        return new String(output);
    }

    private static List<byte[]> copy(Collection<byte[]> values) {
        List<byte[]> output = new ArrayList<>(values.size());
        values.forEach(value -> output.add(value.clone()));
        return output;
    }
}
