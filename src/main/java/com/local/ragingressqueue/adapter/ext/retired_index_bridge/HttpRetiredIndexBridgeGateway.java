package com.local.ragingressqueue.adapter.ext.retired_index_bridge;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Iterator;
import java.util.List;
import java.util.Map;
import java.util.UUID;

@Component
@Profile("retired-index-bridge")
class HttpRetiredIndexBridgeGateway implements RetiredIndexBridgeGateway {
    private static final Logger LOGGER = LoggerFactory.getLogger(HttpRetiredIndexBridgeGateway.class);
    private static final Duration TIMEOUT = Duration.ofSeconds(30);
    private static final int PRESSURE_SAMPLE_SIZE = 200;
    private static final int CONTENT_HASH_LOOKUP_PAGE_SIZE = 30;
    // Safety valve for the keyword-lookup pagination loop. A 12-hex content-hash fragment realistically
    // yields 0-1 hits, so this bound is never reached in practice; it only prevents an unbounded loop
    // if RetiredIndexBridge ever reports a pathological total. 100 pages * 30 = 3000 candidate documents.
    private static final int CONTENT_HASH_LOOKUP_MAX_PAGES = 100;

    private final HttpClient httpClient;
    private final ObjectMapper objectMapper;

    HttpRetiredIndexBridgeGateway() {
        this(HttpClient.newBuilder().connectTimeout(TIMEOUT).build(), new ObjectMapper());
    }

    HttpRetiredIndexBridgeGateway(HttpClient httpClient, ObjectMapper objectMapper) {
        this.httpClient = httpClient;
        this.objectMapper = objectMapper;
    }

    @Override
    public RetiredIndexBridgeDocumentRef uploadDocument(String baseUrl, String apiKey, String datasetId, DocumentPayload payload) {
        String boundary = "rag-ingress-" + UUID.randomUUID().toString().replace("-", "");
        byte[] body = multipartBody(boundary, payload);
        JsonNode data = request(
            "POST",
            baseUrl + "/api/v1/datasets/" + path(datasetId) + "/documents",
            apiKey,
            "multipart/form-data; boundary=" + boundary,
            body
        );
        JsonNode document = data.isArray() ? data.path(0) : data;
        String documentId = text(document, "id");
        if (documentId.isEmpty()) {
            documentId = text(document, "document_id");
        }
        if (documentId.isEmpty()) {
            throw new RetiredIndexBridgeDeliveryException("retired_index_bridge upload response missing document id");
        }
        return new RetiredIndexBridgeDocumentRef(documentId, text(document, "run"));
    }

    @Override
    public void updateMetadata(String baseUrl, String apiKey, String datasetId, String documentId, Map<String, String> metadata) {
        requestJson(
            "PATCH",
            baseUrl + "/api/v1/datasets/" + path(datasetId) + "/documents/" + path(documentId),
            apiKey,
            Map.of("meta_fields", metadata)
        );
    }

    @Override
    public void requestParse(String baseUrl, String apiKey, String datasetId, String documentId) {
        Map<String, Object> payload = Map.of("document_ids", new String[] {documentId});
        try {
            requestJson("POST", baseUrl + "/api/v1/datasets/" + path(datasetId) + "/documents/parse", apiKey, payload);
        } catch (RetiredIndexBridgeDeliveryException firstFailure) {
            requestJson("POST", baseUrl + "/api/v1/datasets/" + path(datasetId) + "/chunks", apiKey, payload);
        }
    }

    @Override
    public boolean findByContentHash(String baseUrl, String apiKey, String datasetId, String contentHashFragment) {
        if (contentHashFragment == null || contentHashFragment.isBlank()) {
            return false;
        }
        // Page through the keyword result set: the real name match can land beyond the first page when
        // the fragment yields more hits than one page holds. Stopping at page 1 would falsely report
        // absent and re-upload a duplicate, breaking the findByContentHash contract. A short (or empty)
        // page is the end-of-results signal, independent of the server's reported total, so the common
        // case (0-1 hits) stays a single GET while a miscounted total cannot hide a last-page match.
        for (int page = 1; page <= CONTENT_HASH_LOOKUP_MAX_PAGES; page++) {
            JsonNode data = request(
                "GET",
                baseUrl + "/api/v1/datasets/" + path(datasetId) + "/documents?page=" + page
                    + "&page_size=" + CONTENT_HASH_LOOKUP_PAGE_SIZE
                    + "&keywords=" + URLEncoder.encode(contentHashFragment, StandardCharsets.UTF_8),
                apiKey,
                "",
                null
            );
            JsonNode docs = data.path("docs");
            if (!docs.isArray()) {
                docs = data;
            }
            if (!docs.isArray()) {
                // A malformed (non-array) document list must not be read as "no match": that would
                // silently skip dedup. Fail the lookup so the delivery is retried.
                throw new RetiredIndexBridgeDeliveryException("retired_index_bridge document list response missing docs");
            }
            if (matchesContentHashFragment(docs, contentHashFragment)) {
                return true;
            }
            if (docs.size() < CONTENT_HASH_LOOKUP_PAGE_SIZE) {
                return false;
            }
        }
        // Safety valve reached without a definitive end-of-results. Returning "not found" here would let
        // deliver() upload a duplicate when the match may simply be beyond the cap, so fail closed: the
        // delivery is reported failed and retried, preserving dedup, and the anomaly is surfaced loudly.
        throw new RetiredIndexBridgeDeliveryException(
            "retired_index_bridge content_hash lookup exceeded the " + CONTENT_HASH_LOOKUP_MAX_PAGES
                + "-page scan limit before results were exhausted");
    }

    static boolean matchesContentHashFragment(JsonNode docs, String contentHashFragment) {
        if (contentHashFragment == null || contentHashFragment.isBlank() || docs == null || !docs.isArray()) {
            return false;
        }
        // The fragment is always written as the token immediately before the final extension (see
        // RetiredIndexBridgeTargetAdapter#payloadWithHashInFilename), so match the base name's suffix exactly. A
        // looser contains-check could match the fragment inside an unrelated part of the name (e.g. an
        // original "-<frag>." earlier in the name) and skip a genuine new upload (data loss).
        String suffixToken = "-" + contentHashFragment;
        for (JsonNode doc : docs) {
            String name = text(doc, "name");
            if (name.isEmpty()) {
                continue;
            }
            int lastDot = name.lastIndexOf('.');
            String baseName = lastDot > 0 ? name.substring(0, lastDot) : name;
            if (baseName.endsWith(suffixToken)) {
                return true;
            }
        }
        return false;
    }

    @Override
    public List<RetiredIndexBridgeDocumentSummary> listDocumentsByKeyword(String baseUrl, String apiKey, String datasetId, String keyword) {
        // Keyed by document id so an overlapping/duplicated page from the server cannot surface the same
        // document twice. Insertion order is preserved for stable, testable results.
        java.util.LinkedHashMap<String, RetiredIndexBridgeDocumentSummary> matches = new java.util.LinkedHashMap<>();
        if (keyword == null || keyword.isBlank()) {
            return new ArrayList<>(matches.values());
        }
        // Same bounded, short-page-terminated pagination as findByContentHash so a multi-page keyword
        // result is fully scanned without relying on the server's reported total.
        for (int page = 1; page <= CONTENT_HASH_LOOKUP_MAX_PAGES; page++) {
            JsonNode data = request(
                "GET",
                baseUrl + "/api/v1/datasets/" + path(datasetId) + "/documents?page=" + page
                    + "&page_size=" + CONTENT_HASH_LOOKUP_PAGE_SIZE
                    + "&keywords=" + URLEncoder.encode(keyword, StandardCharsets.UTF_8),
                apiKey,
                "",
                null
            );
            JsonNode docs = data.path("docs");
            if (!docs.isArray()) {
                docs = data;
            }
            if (!docs.isArray()) {
                throw new RetiredIndexBridgeDeliveryException("retired_index_bridge document list response missing docs");
            }
            for (JsonNode doc : docs) {
                String id = text(doc, "id");
                if (id.isEmpty()) {
                    id = text(doc, "document_id");
                }
                String name = text(doc, "name");
                if (!id.isEmpty()) {
                    matches.putIfAbsent(id, new RetiredIndexBridgeDocumentSummary(id, name));
                }
            }
            if (docs.size() < CONTENT_HASH_LOOKUP_PAGE_SIZE) {
                return new ArrayList<>(matches.values());
            }
        }
        // The result set is truncated: supersede only sees the first pages, so prior versions beyond the
        // scan limit will not be retired (stale versions may linger; this is not data loss).
        LOGGER.warn("RetiredIndexBridge keyword document listing hit the {}-page scan limit; results may be incomplete and "
            + "prior versions beyond the limit will not be superseded", CONTENT_HASH_LOOKUP_MAX_PAGES);
        return new ArrayList<>(matches.values());
    }

    @Override
    public void deleteDocuments(String baseUrl, String apiKey, String datasetId, Collection<String> documentIds) {
        if (documentIds == null || documentIds.isEmpty()) {
            return;
        }
        // RetiredIndexBridge deletes by id list: DELETE /datasets/{id}/documents with {"ids": [...]}. Note this
        // differs from the parse endpoint, which uses "document_ids".
        requestJson("DELETE", baseUrl + "/api/v1/datasets/" + path(datasetId) + "/documents", apiKey,
            Map.of("ids", List.copyOf(documentIds)));
    }

    @Override
    public RetiredIndexBridgePressureSnapshot pressureSnapshot(String baseUrl, String apiKey, String datasetId) {
        RetiredIndexBridgePressureSnapshot recent = pressureSnapshotForQuery(baseUrl, apiKey, datasetId, "page=1&page_size=" + PRESSURE_SAMPLE_SIZE);
        RetiredIndexBridgePressureSnapshot running = pressureSnapshotForQuery(baseUrl, apiKey, datasetId, "page=1&page_size=1&run=RUNNING");
        RetiredIndexBridgePressureSnapshot unstart = pressureSnapshotForQuery(baseUrl, apiKey, datasetId, "page=1&page_size=1&run=UNSTART");
        return new RetiredIndexBridgePressureSnapshot(
            Math.max(recent.running(), running.total()),
            Math.max(recent.unstart(), unstart.total()),
            recent.failed(),
            recent.done(),
            recent.sampled(),
            recent.total()
        );
    }

    private RetiredIndexBridgePressureSnapshot pressureSnapshotForQuery(String baseUrl, String apiKey, String datasetId, String query) {
        JsonNode data = request(
            "GET",
            baseUrl + "/api/v1/datasets/" + path(datasetId) + "/documents?" + query,
            apiKey,
            "",
            null
        );
        int total = data.path("total").asInt(0);
        JsonNode documents = data.path("docs");
        if (!documents.isArray()) {
            documents = data;
        }
        if (!documents.isArray()) {
            throw new RetiredIndexBridgeDeliveryException("retired_index_bridge document list response missing docs");
        }

        int running = 0;
        int unstart = 0;
        int failed = 0;
        int done = 0;
        int sampled = 0;
        for (Iterator<JsonNode> iterator = documents.elements(); iterator.hasNext();) {
            sampled++;
            String run = text(iterator.next(), "run");
            if ("RUNNING".equals(run)) {
                running++;
            } else if ("UNSTART".equals(run)) {
                unstart++;
            } else if ("FAIL".equals(run) || "FAILED".equals(run) || "CANCEL".equals(run)) {
                failed++;
            } else if ("DONE".equals(run)) {
                done++;
            }
        }
        if (total < sampled) {
            total = sampled;
        }
        return new RetiredIndexBridgePressureSnapshot(running, unstart, failed, done, sampled, total);
    }

    private void requestJson(String method, String url, String apiKey, Map<String, ?> payload) {
        try {
            byte[] body = objectMapper.writeValueAsBytes(payload);
            request(method, url, apiKey, "application/json", body);
        } catch (IOException error) {
            throw new RetiredIndexBridgeDeliveryException("retired_index_bridge request serialization failed", error);
        }
    }

    private JsonNode request(String method, String url, String apiKey, String contentType, byte[] body) {
        try {
            HttpRequest.Builder builder = HttpRequest.newBuilder(URI.create(url))
                .timeout(TIMEOUT)
                .method(
                    method,
                    body == null ? HttpRequest.BodyPublishers.noBody() : HttpRequest.BodyPublishers.ofByteArray(body)
                )
                .header("Authorization", "Bearer " + apiKey);
            if (contentType != null && !contentType.isBlank()) {
                builder.header("Content-Type", contentType);
            }
            HttpRequest request = builder.build();
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if (response.statusCode() >= 400) {
                throw new RetiredIndexBridgeDeliveryException("retired_index_bridge http request failed");
            }
            JsonNode root = objectMapper.readTree(response.body().isBlank() ? "{}" : response.body());
            if (root.path("code").asInt(0) != 0) {
                throw new RetiredIndexBridgeDeliveryException("retired_index_bridge api request failed");
            }
            return root.path("data");
        } catch (IllegalArgumentException error) {
            throw new RetiredIndexBridgeDeliveryException("retired_index_bridge http request invalid", error);
        } catch (IOException error) {
            throw new RetiredIndexBridgeDeliveryException("retired_index_bridge http request failed", error);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new RetiredIndexBridgeDeliveryException("retired_index_bridge http request interrupted", error);
        }
    }

    private static byte[] multipartBody(String boundary, DocumentPayload payload) {
        String filename = safeFilename(payload.filename());
        String contentType = payload.contentType() == null || payload.contentType().isBlank()
            ? "text/markdown"
            : payload.contentType();
        String body = "--" + boundary + "\r\n"
            + "Content-Disposition: form-data; name=\"file\"; filename=\"" + filename + "\"\r\n"
            + "Content-Type: " + contentType + "\r\n\r\n"
            + payload.body() + "\r\n"
            + "--" + boundary + "--\r\n";
        return body.getBytes(StandardCharsets.UTF_8);
    }

    private static String safeFilename(String filename) {
        if (filename == null || filename.isBlank()) {
            return "rag-ingress-document.md";
        }
        return sanitizeDocumentName(filename);
    }

    // Package-private so the adapter can canonicalize a name the same way the multipart upload does.
    // RetiredIndexBridge stores the sanitized name, so supersede's exact-name matching must use this same form,
    // otherwise a filename containing one of these characters would never match its prior versions.
    static String sanitizeDocumentName(String filename) {
        return filename.replace('"', '_').replace('\r', '_').replace('\n', '_');
    }

    private static String path(String value) {
        return URLEncoder.encode(value, StandardCharsets.UTF_8).replace("+", "%20");
    }

    private static String text(JsonNode node, String field) {
        JsonNode value = node.path(field);
        return value.isTextual() ? value.asText() : "";
    }
}
