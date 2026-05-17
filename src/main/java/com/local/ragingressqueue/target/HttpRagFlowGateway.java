package com.local.ragingressqueue.target;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.local.ragingressqueue.core.DocumentPayload;
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
import java.util.Map;
import java.util.UUID;

@Component
@Profile("worker")
class HttpRagFlowGateway implements RagFlowGateway {
    private static final Duration TIMEOUT = Duration.ofSeconds(30);

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

    private void requestJson(String method, String url, String apiKey, Map<String, ?> payload) {
        try {
            byte[] body = objectMapper.writeValueAsBytes(payload);
            request(method, url, apiKey, "application/json", body);
        } catch (IOException error) {
            throw new RagFlowDeliveryException("ragflow request serialization failed", error);
        }
    }

    private JsonNode request(String method, String url, String apiKey, String contentType, byte[] body) {
        HttpRequest request = HttpRequest.newBuilder(URI.create(url))
            .timeout(TIMEOUT)
            .method(method, HttpRequest.BodyPublishers.ofByteArray(body))
            .header("Authorization", "Bearer " + apiKey)
            .header("Content-Type", contentType)
            .build();
        try {
            HttpResponse<String> response = httpClient.send(request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if (response.statusCode() >= 400) {
                throw new RagFlowDeliveryException("ragflow http request failed");
            }
            JsonNode root = objectMapper.readTree(response.body().isBlank() ? "{}" : response.body());
            if (root.path("code").asInt(0) != 0) {
                throw new RagFlowDeliveryException("ragflow api request failed");
            }
            return root.path("data");
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
