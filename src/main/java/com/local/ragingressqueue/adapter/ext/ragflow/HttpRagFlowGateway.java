package com.local.ragingressqueue.adapter.ext.ragflow;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.local.ragingressqueue.ingest.domain.DocumentPayload;
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
import java.util.Iterator;
import java.util.Map;
import java.util.UUID;

@Component
@Profile({"api", "worker"})
class HttpRagFlowGateway implements RagFlowGateway {
    private static final Duration TIMEOUT = Duration.ofSeconds(30);
    private static final int PRESSURE_SAMPLE_SIZE = 200;
    private static final int CONTENT_HASH_LOOKUP_PAGE_SIZE = 30;

    private final HttpClient httpClient;
    private final ObjectMapper objectMapper;

    HttpRagFlowGateway() {
        this(HttpClient.newBuilder().connectTimeout(TIMEOUT).build(), new ObjectMapper());
    }

    HttpRagFlowGateway(HttpClient httpClient, ObjectMapper objectMapper) {
        this.httpClient = httpClient;
        this.objectMapper = objectMapper;
    }

    @Override
    public RagFlowDocumentRef uploadDocument(String baseUrl, String apiKey, String datasetId, DocumentPayload payload) {
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
            throw new RagFlowDeliveryException("ragflow upload response missing document id");
        }
        return new RagFlowDocumentRef(documentId, text(document, "run"));
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
        } catch (RagFlowDeliveryException firstFailure) {
            requestJson("POST", baseUrl + "/api/v1/datasets/" + path(datasetId) + "/chunks", apiKey, payload);
        }
    }

    @Override
    public boolean findByContentHash(String baseUrl, String apiKey, String datasetId, String contentHashFragment) {
        if (contentHashFragment == null || contentHashFragment.isBlank()) {
            return false;
        }
        JsonNode data = request(
            "GET",
            baseUrl + "/api/v1/datasets/" + path(datasetId) + "/documents?page=1&page_size=" + CONTENT_HASH_LOOKUP_PAGE_SIZE
                + "&keywords=" + URLEncoder.encode(contentHashFragment, StandardCharsets.UTF_8),
            apiKey,
            "",
            null
        );
        JsonNode docs = data.path("docs");
        if (!docs.isArray()) {
            docs = data;
        }
        // Require the fragment to appear as the delimited hash-suffix token in a document name, not
        // merely as a keyword hit (which can match the body or a longer token). A false positive here
        // would skip a genuine new upload, so we match conservatively on the name we ourselves write.
        return matchesContentHashFragment(docs, contentHashFragment);
    }

    static boolean matchesContentHashFragment(JsonNode docs, String contentHashFragment) {
        if (contentHashFragment == null || contentHashFragment.isBlank() || docs == null || !docs.isArray()) {
            return false;
        }
        String dotToken = "-" + contentHashFragment + ".";
        String endToken = "-" + contentHashFragment;
        for (JsonNode doc : docs) {
            String name = text(doc, "name");
            if (name.contains(dotToken) || name.endsWith(endToken)) {
                return true;
            }
        }
        return false;
    }

    @Override
    public RagFlowPressureSnapshot pressureSnapshot(String baseUrl, String apiKey, String datasetId) {
        RagFlowPressureSnapshot recent = pressureSnapshotForQuery(baseUrl, apiKey, datasetId, "page=1&page_size=" + PRESSURE_SAMPLE_SIZE);
        RagFlowPressureSnapshot running = pressureSnapshotForQuery(baseUrl, apiKey, datasetId, "page=1&page_size=1&run=RUNNING");
        RagFlowPressureSnapshot unstart = pressureSnapshotForQuery(baseUrl, apiKey, datasetId, "page=1&page_size=1&run=UNSTART");
        return new RagFlowPressureSnapshot(
            Math.max(recent.running(), running.total()),
            Math.max(recent.unstart(), unstart.total()),
            recent.failed(),
            recent.done(),
            recent.sampled(),
            recent.total()
        );
    }

    private RagFlowPressureSnapshot pressureSnapshotForQuery(String baseUrl, String apiKey, String datasetId, String query) {
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
            throw new RagFlowDeliveryException("ragflow document list response missing docs");
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
        return new RagFlowPressureSnapshot(running, unstart, failed, done, sampled, total);
    }

    private void requestJson(String method, String url, String apiKey, Map<String, ?> payload) {
        try {
            byte[] body = objectMapper.writeValueAsBytes(payload);
            request(method, url, apiKey, "application/json", body);
        } catch (IOException error) {
            throw new RagFlowDeliveryException("ragflow request serialization failed", error);
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
                throw new RagFlowDeliveryException("ragflow http request failed");
            }
            JsonNode root = objectMapper.readTree(response.body().isBlank() ? "{}" : response.body());
            if (root.path("code").asInt(0) != 0) {
                throw new RagFlowDeliveryException("ragflow api request failed");
            }
            return root.path("data");
        } catch (IllegalArgumentException error) {
            throw new RagFlowDeliveryException("ragflow http request invalid", error);
        } catch (IOException error) {
            throw new RagFlowDeliveryException("ragflow http request failed", error);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new RagFlowDeliveryException("ragflow http request interrupted", error);
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
