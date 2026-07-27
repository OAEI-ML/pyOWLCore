package org.pyowlcore.comparator;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.Comparator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.NavigableMap;
import java.util.TreeMap;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.MapperFeature;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.SerializationFeature;

/** Builds and validates the exact backend-neutral comparator contract. */
final class CommonContract {
    private static final String SCHEMA = "pyowl-core/comparator-common-contract/v1";
    private static final byte[] RECORD_INVENTORY_DOMAIN =
            ascii("pyowl-core:comparator-record-inventory:v1\0");
    private static final Comparator<byte[]> UNSIGNED_BYTES = Arrays::compareUnsigned;
    private static final ObjectMapper JSON = new ObjectMapper()
            .enable(SerializationFeature.ORDER_MAP_ENTRIES_BY_KEYS)
            .enable(MapperFeature.SORT_PROPERTIES_ALPHABETICALLY);

    static final class RequestContext {
        final String corpusId;
        final byte[] source;
        final String sourceSha256;
        final String documentIri;
        final String format;
        final String optionsSha256;

        RequestContext(
                String corpusId,
                byte[] source,
                String sourceSha256,
                String documentIri,
                String format,
                String optionsSha256) {
            this.corpusId = corpusId;
            this.source = source;
            this.sourceSha256 = sourceSha256;
            this.documentIri = documentIri;
            this.format = format;
            this.optionsSha256 = optionsSha256;
        }
    }

    static final class Build {
        final Map<String, Object> contract;
        final long validationNs;

        Build(Map<String, Object> contract, long validationNs) {
            this.contract = contract;
            this.validationNs = validationNs;
        }
    }

    private CommonContract() {}

    static Build build(ModelMapper.MappedDocument document, RequestContext request) {
        byte[] documentPreimage = documentPreimage(document);
        byte[] documentFingerprint = Canonical.sha256(documentPreimage);
        String key = documentKey(document, documentFingerprint);
        byte[] manifest = manifest(document, key, documentFingerprint);
        byte[] structural = structuralPreimage(document, key, manifest);
        byte[] logical = logicalPreimage(document);
        byte[] signature = signaturePreimage(document);
        List<Object> diagnostics = diagnosticRows(document, request.documentIri);

        Map<String, Object> identity = identity(document, request, key, documentFingerprint);
        Map<String, Object> provenance = provenance(document, request, key);
        Map<String, Object> inventories = inventories(document, request, key, documentFingerprint);
        byte[] identityBytes = canonicalJson(identity);
        byte[] provenanceBytes = canonicalJson(provenance);
        byte[] diagnosticsBytes = canonicalJson(diagnostics);
        Map<String, Object> ledger = object(
                "diagnostic_count", diagnostics.size(),
                "diagnostics_bytes", diagnosticsBytes.length,
                "diagnostics_sha256", Canonical.hex(Canonical.sha256(diagnosticsBytes)),
                "identity_bytes", identityBytes.length,
                "identity_sha256", Canonical.hex(Canonical.sha256(identityBytes)),
                "inventories", inventories,
                "provenance_bytes", provenanceBytes.length,
                "provenance_sha256", Canonical.hex(Canonical.sha256(provenanceBytes)));
        Map<String, Object> fingerprints = object(
                "document", fingerprint(documentPreimage),
                "logical", fingerprint(logical),
                "signature", fingerprint(signature),
                "structural", fingerprint(structural));
        Map<String, Object> contract = object(
                "complete_import_closure", document.imports.isEmpty(),
                "corpus_id", request.corpusId,
                "diagnostics", diagnostics,
                "fingerprints", fingerprints,
                "identity", identity,
                "ledger", ledger,
                "model_schema", 1,
                "options_sha256", request.optionsSha256,
                "provenance", provenance,
                "root_document_key", key,
                "schema", SCHEMA,
                "source_sha256", request.sourceSha256);
        contract.put("contract_sha256", Canonical.hex(Canonical.sha256(canonicalJson(contract))));
        contract = sortedCopy(contract);
        long started = System.nanoTime();
        validate(contract);
        return new Build(contract, Math.max(0L, System.nanoTime() - started));
    }

    static byte[] canonicalJson(Object value) {
        try {
            return JSON.writeValueAsBytes(value);
        } catch (JsonProcessingException error) {
            throw new IllegalStateException("common contract could not be serialized", error);
        }
    }

    private static Map<String, Object> identity(
            ModelMapper.MappedDocument document,
            RequestContext request,
            String key,
            byte[] documentFingerprint) {
        List<Object> imports = new ArrayList<>();
        for (byte[] value : document.imports) {
            imports.add(object(
                    "import_iri", Canonical.hex(value),
                    "importing_document_key", key,
                    "resolved_document_key", null,
                    "resolver_name", "none",
                    "status", "unresolved"));
        }
        Map<String, Object> documentRow = object(
                "document_fingerprint", Canonical.hex(documentFingerprint),
                "document_iri", Canonical.hex(Canonical.iri(request.documentIri)),
                "document_key", key,
                "format", request.format,
                "ontology_iri", optionalIri(document.ontologyIri),
                "source_sha256", request.sourceSha256,
                "status", "root",
                "version_iri", optionalIri(document.versionIri));
        return object(
                "documents", List.of(documentRow),
                "import_policy", "record_unresolved",
                "imports", imports,
                "offline", true,
                "resolver_configuration_sha256", Canonical.hex(resolverConfigurationDigest()),
                "root_document_key", key);
    }

    private static Map<String, Object> provenance(
            ModelMapper.MappedDocument document, RequestContext request, String key) {
        List<Object> rows = originRows(
                document.provenanceRoots,
                key,
                "rdfxml".equals(request.format) || "turtle".equals(request.format));
        return object(
                "document_count", 1,
                "origin_entry_count", rows.size(),
                "origins", rows,
                "source_byte_count", request.source.length);
    }

    static List<Object> originRows(
            List<byte[]> roots, String documentKey, boolean canonicalOrdinals) {
        List<OriginOccurrence> occurrences = new ArrayList<>(roots.size());
        byte[] documentKeyBytes = utf8(documentKey);
        for (int priorOccurrence = 0; priorOccurrence < roots.size(); priorOccurrence++) {
            occurrences.add(new OriginOccurrence(
                    Canonical.structuralDigest(roots.get(priorOccurrence)),
                    documentKey,
                    documentKeyBytes,
                    priorOccurrence));
        }
        if (canonicalOrdinals) {
            occurrences.sort((left, right) -> {
                int digestOrder = UNSIGNED_BYTES.compare(left.digest, right.digest);
                if (digestOrder != 0) {
                    return digestOrder;
                }
                int documentOrder =
                        UNSIGNED_BYTES.compare(left.documentKeyBytes, right.documentKeyBytes);
                if (documentOrder != 0) {
                    return documentOrder;
                }
                return Integer.compare(left.priorOccurrence, right.priorOccurrence);
            });
        }

        NavigableMap<byte[], List<Object>> origins = new TreeMap<>(UNSIGNED_BYTES);
        for (int ordinal = 0; ordinal < occurrences.size(); ordinal++) {
            OriginOccurrence occurrence = occurrences.get(ordinal);
            origins.computeIfAbsent(occurrence.digest, ignored -> new ArrayList<>())
                    .add(object(
                            "document_key", occurrence.documentKey,
                            "occurrence", ordinal,
                            "span", null));
        }
        List<Object> rows = new ArrayList<>();
        for (Map.Entry<byte[], List<Object>> entry : origins.entrySet()) {
            rows.add(object(
                    "occurrences", entry.getValue(),
                    "structural_sha256", Canonical.hex(entry.getKey())));
        }
        return rows;
    }

    private static final class OriginOccurrence {
        final byte[] digest;
        final String documentKey;
        final byte[] documentKeyBytes;
        final int priorOccurrence;

        OriginOccurrence(
                byte[] digest,
                String documentKey,
                byte[] documentKeyBytes,
                int priorOccurrence) {
            this.digest = digest;
            this.documentKey = documentKey;
            this.documentKeyBytes = documentKeyBytes;
            this.priorOccurrence = priorOccurrence;
        }
    }

    private static Map<String, Object> inventories(
            ModelMapper.MappedDocument document,
            RequestContext request,
            String key,
            byte[] documentFingerprint) {
        ByteArrayOutputStream row = new ByteArrayOutputStream();
        Canonical.frame(row, utf8(key));
        row.writeBytes(hexDigest(request.sourceSha256));
        row.writeBytes(documentFingerprint);
        ByteArrayOutputStream transcript = new ByteArrayOutputStream();
        transcript.writeBytes(ascii("pyowl-core:comparator-document-inventory:v1\0"));
        Canonical.varint(transcript, 1);
        transcript.writeBytes(row.toByteArray());
        Map<String, Object> documents = object(
                "canonical_bytes", row.size(),
                "count", 1,
                "sha256", Canonical.hex(Canonical.sha256(transcript.toByteArray())),
                "transcript_bytes", transcript.size());
        return object(
                "axioms", recordInventory(document.axioms.stream()
                        .map(value -> value.value).collect(java.util.stream.Collectors.toList())),
                "documents", documents,
                "extensions", recordInventory(document.extensions.stream()
                        .map(value -> value.value).collect(java.util.stream.Collectors.toList())),
                "ontology_annotations", recordInventory(document.annotations),
                "signature", recordInventory(document.signature));
    }

    private static Map<String, Object> recordInventory(Collection<byte[]> input) {
        List<byte[]> values = Canonical.normalizeSet(input);
        long canonicalBytes = 0;
        ByteArrayOutputStream transcript = new ByteArrayOutputStream();
        transcript.writeBytes(RECORD_INVENTORY_DOMAIN);
        Canonical.varint(transcript, values.size());
        for (byte[] value : values) {
            canonicalBytes = Math.addExact(canonicalBytes, value.length);
            Canonical.frame(transcript, value);
        }
        return object(
                "canonical_bytes", canonicalBytes,
                "count", values.size(),
                "sha256", Canonical.hex(Canonical.sha256(transcript.toByteArray())),
                "transcript_bytes", transcript.size());
    }

    private static Map<String, Object> fingerprint(byte[] preimage) {
        String digest = Canonical.hex(Canonical.sha256(preimage));
        return object(
                "algorithm", "sha256",
                "digest", digest,
                "preimage_bytes", preimage.length,
                "preimage_sha256", digest,
                "schema", 1);
    }

    private static byte[] documentPreimage(ModelMapper.MappedDocument document) {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        output.writeBytes(ascii("pyowl-core:document-fingerprint:v1\0"));
        optionalIri(output, document.ontologyIri);
        optionalIri(output, document.versionIri);
        appendCollection(output, document.imports);
        appendCollection(output, document.annotations);
        appendCollection(output, document.axioms.stream()
                .map(value -> value.value).collect(java.util.stream.Collectors.toList()));
        appendCollection(output, document.extensions.stream()
                .map(value -> value.value).collect(java.util.stream.Collectors.toList()));
        return output.toByteArray();
    }

    private static String documentKey(
            ModelMapper.MappedDocument document, byte[] documentFingerprint) {
        ByteArrayOutputStream payload = new ByteArrayOutputStream();
        if (document.ontologyIri == null) {
            payload.writeBytes(ascii("anonymous"));
            payload.writeBytes(documentFingerprint);
        } else {
            payload.writeBytes(ascii("named"));
            Canonical.frame(payload, ascii(document.versionIri == null ? "ontology" : "version"));
            Canonical.frame(payload, utf8(document.ontologyIri));
            if (document.versionIri != null) {
                Canonical.frame(payload, utf8(document.versionIri));
            }
        }
        ByteArrayOutputStream preimage = new ByteArrayOutputStream();
        preimage.writeBytes(ascii("pyowl-core:document-key:v1\0"));
        preimage.writeBytes(payload.toByteArray());
        return "d1:" + Canonical.hex(Canonical.sha256(preimage.toByteArray()));
    }

    private static byte[] resolverConfigurationDigest() {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        output.writeBytes(ascii("pyowl-core:resolver-configuration:v1\0"));
        Canonical.frame(output, ascii("none"));
        return Canonical.sha256(output.toByteArray());
    }

    private static byte[] manifest(
            ModelMapper.MappedDocument document, String key, byte[] documentFingerprint) {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        output.writeBytes(ascii("pyowl-core:import-manifest:v1\0"));
        Canonical.frame(output, ascii("record_unresolved"));
        output.write(1);
        output.writeBytes(resolverConfigurationDigest());
        Canonical.varint(output, 1);
        Canonical.frame(output, utf8(key));
        optionalIri(output, document.ontologyIri);
        optionalIri(output, document.versionIri);
        output.writeBytes(documentFingerprint);
        Canonical.frame(output, ascii("root"));
        Canonical.varint(output, document.imports.size());
        for (byte[] importIri : document.imports) {
            Canonical.frame(output, utf8(key));
            Canonical.frame(output, importIri);
            Canonical.frame(output, ascii("unresolved"));
            optionalText(output, null);
            optionalText(output, "none");
            optionalText(output, "UNRESOLVED_IMPORT");
        }
        return output.toByteArray();
    }

    private static byte[] structuralPreimage(
            ModelMapper.MappedDocument document, String key, byte[] manifest) {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        output.writeBytes(ascii("pyowl-core:snapshot-structural:v1\0"));
        Canonical.frame(output, manifest);
        Canonical.frame(output, utf8(key));
        appendCollection(output, document.annotations);
        appendCollection(output, document.axioms.stream()
                .map(value -> value.value).collect(java.util.stream.Collectors.toList()));
        appendCollection(output, document.extensions.stream()
                .map(value -> value.value).collect(java.util.stream.Collectors.toList()));
        return output.toByteArray();
    }

    private static byte[] logicalPreimage(ModelMapper.MappedDocument document) {
        List<byte[]> axioms = Canonical.normalizeSet(document.axioms.stream()
                .filter(value -> value.logical != null)
                .map(value -> value.logical).collect(java.util.stream.Collectors.toList()));
        List<byte[]> extensions = Canonical.normalizeSet(document.extensions.stream()
                .map(value -> value.logical).collect(java.util.stream.Collectors.toList()));
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        output.writeBytes(ascii("pyowl-core:snapshot-logical:v1\0datatype-policy:owl2-v1\0"));
        appendCollection(output, axioms);
        Canonical.varint(output, extensions.size());
        for (byte[] value : extensions) {
            output.write('E');
            Canonical.frame(output, value);
        }
        return output.toByteArray();
    }

    private static byte[] signaturePreimage(ModelMapper.MappedDocument document) {
        ByteArrayOutputStream output = new ByteArrayOutputStream();
        output.writeBytes(ascii("pyowl-core:snapshot-signature:v1\0"));
        output.write(1);
        appendCollection(output, document.signature);
        return output.toByteArray();
    }

    private static List<Object> diagnosticRows(
            ModelMapper.MappedDocument document, String documentIri) {
        List<Object> output = new ArrayList<>();
        int retained = Math.min(document.imports.size(), 10_000);
        for (int index = 0; index < retained; index++) {
            String importIri = decodeIri(document.imports.get(index));
            output.add(object(
                    "code", "UNRESOLVED_IMPORT",
                    "details", object("import_iri", sanitizeIri(importIri), "resolver", "none"),
                    "document_iri", documentIri,
                    "import_chain", List.of(importIri),
                    "message", "import could not be resolved (not_found)",
                    "severity", "warning",
                    "source_span", null));
        }
        if (document.imports.size() > 10_000) {
            output = new ArrayList<>(output.subList(0, 9_999));
            output.add(object(
                    "code", "DIAGNOSTICS_SUPPRESSED",
                    "details", object("count", document.imports.size() - 9_999),
                    "document_iri", null,
                    "import_chain", List.of(),
                    "message", "additional import diagnostics were suppressed",
                    "severity", "warning",
                    "source_span", null));
        }
        return output;
    }

    private static String decodeIri(byte[] value) {
        if (value.length < 3 || value[0] != Canonical.IRI || value[1] != 2) {
            throw new IllegalStateException("canonical import IRI is malformed");
        }
        int[] decoded = decodeVarint(value, 2);
        int end = Math.addExact(decoded[1], decoded[0]);
        if (end != value.length) {
            throw new IllegalStateException("canonical import IRI has trailing bytes");
        }
        return new String(value, decoded[1], decoded[0], StandardCharsets.UTF_8);
    }

    private static int[] decodeVarint(byte[] value, int offset) {
        long result = 0;
        int shift = 0;
        while (offset < value.length && shift < 32) {
            int next = value[offset++] & 0xff;
            result |= (long) (next & 0x7f) << shift;
            if ((next & 0x80) == 0) {
                return new int[] {Math.toIntExact(result), offset};
            }
            shift += 7;
        }
        throw new IllegalStateException("canonical varint is malformed");
    }

    private static String sanitizeIri(String value) {
        try {
            java.net.URI uri = java.net.URI.create(value);
            String scheme = uri.getScheme();
            if (scheme == null || !(scheme.equalsIgnoreCase("http") || scheme.equalsIgnoreCase("https"))) {
                return truncate(value);
            }
            String host = uri.getHost();
            if (host == null) {
                return truncate(scheme.toLowerCase(java.util.Locale.ROOT) + ":///"
                        + (uri.getPath() == null ? "" : uri.getPath().replaceFirst("^/+", "")));
            }
            StringBuilder output = new StringBuilder();
            output.append(scheme.toLowerCase(java.util.Locale.ROOT)).append("://")
                    .append(host.toLowerCase(java.util.Locale.ROOT));
            if (uri.getPort() >= 0) {
                output.append(':').append(uri.getPort());
            }
            if (uri.getPath() != null) {
                output.append(uri.getPath());
            }
            return truncate(output.toString());
        } catch (IllegalArgumentException ignored) {
            return truncate(value);
        }
    }

    private static String truncate(String value) {
        int count = value.codePointCount(0, value.length());
        if (count <= 512) {
            return value;
        }
        int end = value.offsetByCodePoints(0, 509);
        return value.substring(0, end) + "...";
    }

    @SuppressWarnings("unchecked")
    private static void validate(Map<String, Object> contract) {
        if (!SCHEMA.equals(contract.get("schema")) || !Integer.valueOf(1).equals(contract.get("model_schema"))) {
            throw new IllegalStateException("common contract schema differs");
        }
        Object observed = contract.get("contract_sha256");
        Map<String, Object> unsigned = new TreeMap<>(contract);
        unsigned.remove("contract_sha256");
        String expected = Canonical.hex(Canonical.sha256(canonicalJson(unsigned)));
        if (!expected.equals(observed)) {
            throw new IllegalStateException("common contract digest validation failed");
        }
        Map<String, Object> ledger = (Map<String, Object>) contract.get("ledger");
        for (String name : List.of("identity", "provenance", "diagnostics")) {
            byte[] encoded = canonicalJson(contract.get(name));
            if (!Canonical.hex(Canonical.sha256(encoded)).equals(ledger.get(name + "_sha256"))
                    || !Integer.valueOf(encoded.length).equals(ledger.get(name + "_bytes"))) {
                throw new IllegalStateException("common " + name + " ledger differs");
            }
        }
        Map<String, Object> fingerprints = (Map<String, Object>) contract.get("fingerprints");
        for (String name : List.of("document", "structural", "logical", "signature")) {
            Map<String, Object> evidence = (Map<String, Object>) fingerprints.get(name);
            if (!"sha256".equals(evidence.get("algorithm"))
                    || !evidence.get("digest").equals(evidence.get("preimage_sha256"))
                    || !isSha256((String) evidence.get("digest"))) {
                throw new IllegalStateException("common " + name + " fingerprint differs");
            }
        }
    }

    private static boolean isSha256(String value) {
        return value != null && value.matches("[0-9a-f]{64}");
    }

    private static void appendCollection(ByteArrayOutputStream output, Collection<byte[]> values) {
        Canonical.varint(output, values.size());
        values.forEach(value -> Canonical.frame(output, value));
    }

    private static void optionalIri(ByteArrayOutputStream output, String value) {
        if (value == null) {
            output.write('0');
        } else {
            output.write('1');
            Canonical.frame(output, Canonical.iri(value));
        }
    }

    private static void optionalText(ByteArrayOutputStream output, String value) {
        if (value == null) {
            output.write('0');
        } else {
            output.write('1');
            Canonical.frame(output, utf8(value));
        }
    }

    private static Object optionalIri(String value) {
        return value == null ? null : Canonical.hex(Canonical.iri(value));
    }

    private static byte[] hexDigest(String value) {
        if (value == null || !value.matches("[0-9a-f]{64}")) {
            throw new IllegalArgumentException("expected lowercase SHA-256");
        }
        byte[] output = new byte[32];
        for (int index = 0; index < output.length; index++) {
            output[index] = (byte) Integer.parseInt(value.substring(index * 2, index * 2 + 2), 16);
        }
        return output;
    }

    private static byte[] ascii(String value) {
        return value.getBytes(StandardCharsets.US_ASCII);
    }

    private static byte[] utf8(String value) {
        return value.getBytes(StandardCharsets.UTF_8);
    }

    private static Map<String, Object> object(Object... fields) {
        if ((fields.length & 1) != 0) {
            throw new IllegalArgumentException("object fields must be key/value pairs");
        }
        Map<String, Object> output = new TreeMap<>();
        for (int index = 0; index < fields.length; index += 2) {
            output.put((String) fields[index], fields[index + 1]);
        }
        return output;
    }

    private static Map<String, Object> sortedCopy(Map<String, Object> value) {
        return new TreeMap<>(value);
    }
}
