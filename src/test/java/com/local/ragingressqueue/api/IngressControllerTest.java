package com.local.ragingressqueue.api;

import com.local.ragingressqueue.core.validation.ContentHashVerifier;
import com.local.ragingressqueue.queue.IngestPublisher;
import com.local.ragingressqueue.queue.PublishResult;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

class IngressControllerTest {
    private FakePublisher publisher;
    private MockMvc mockMvc;

    @BeforeEach
    void setUp() {
        publisher = new FakePublisher();
        mockMvc = MockMvcBuilders.standaloneSetup(IngressController.createForTests(publisher)).build();
    }

    @Test
    void validEnqueueReturnsAcceptedQueuedResponse() throws Exception {
        mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(validRequest(null)))
            .andExpect(status().isAccepted())
            .andExpect(jsonPath("$.accepted").value(true))
            .andExpect(jsonPath("$.status").value("queued"))
            .andExpect(jsonPath("$.jobId").exists());

        assertThat(publisher.publishCount).isEqualTo(1);
    }

    @Test
    void missingSourceReturnsBadRequest() throws Exception {
        mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(validRequestWithoutSource()))
            .andExpect(status().isBadRequest())
            .andExpect(jsonPath("$.accepted").value(false));
    }

    @Test
    void explicitIdempotencyKeyIsAccepted() throws Exception {
        mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(validRequest("\"idempotencyKey\":\"stable-key\",")))
            .andExpect(status().isAccepted())
            .andExpect(jsonPath("$.accepted").value(true));
    }

    @Test
    void sameIdempotencyKeyWithDifferentContentHashReturnsConflict() throws Exception {
        String first = validRequest("\"idempotencyKey\":\"stable-key\",");
        String second = validRequestWithBody("\"idempotencyKey\":\"stable-key\",", body() + "\nchanged");

        mockMvc.perform(post("/v1/ingest/enqueue").contentType(MediaType.APPLICATION_JSON).content(first))
            .andExpect(status().isAccepted());
        mockMvc.perform(post("/v1/ingest/enqueue").contentType(MediaType.APPLICATION_JSON).content(second))
            .andExpect(status().isConflict())
            .andExpect(jsonPath("$.accepted").value(false));
    }

    @Test
    void privateLocatorPayloadReturnsBadRequest() throws Exception {
        mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(validRequest(null).replace("redacted_rag_ready_document", "private_locator")))
            .andExpect(status().isBadRequest());
    }

    @Test
    void reservedDocumentRefReturnsUnprocessableEntity() throws Exception {
        mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(validRequest(null).replace("redacted_rag_ready_document", "redacted_document_ref")))
            .andExpect(status().isUnprocessableEntity());
    }

    @Test
    void bearerTokenInPayloadReturnsBadRequestWithoutEcho() throws Exception {
        String bodyWithBearer = body().replace("redacted body", "redacted body Bearer abc.def.ghi");
        String response = mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(validRequestWithBody(null, bodyWithBearer)))
            .andExpect(status().isBadRequest())
            .andReturn()
            .getResponse()
            .getContentAsString();

        assertThat(response).doesNotContain("abc.def.ghi");
    }

    @Test
    void forbiddenMetadataReturnsBadRequestWithoutEcho() throws Exception {
        String response = mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(validRequest(null).replace("\"result_type\":\"conversation_chunk\"", "\"documentId\":\"raw-document-123\"")))
            .andExpect(status().isBadRequest())
            .andReturn()
            .getResponse()
            .getContentAsString();

        assertThat(response).doesNotContain("raw-document-123");
    }

    @Test
    void unknownKindReturnsBadRequestBeforePublish() throws Exception {
        mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(validRequest(null).replace("\"kind\": \"conversation_chunk\"", "\"kind\": \"unexpected_kind\"")))
            .andExpect(status().isBadRequest())
            .andExpect(jsonPath("$.accepted").value(false));

        assertThat(publisher.publishCount).isZero();
    }

    @Test
    void invalidRequestDoesNotReserveIdempotencyKey() throws Exception {
        String bad = validRequest("\"idempotencyKey\":\"retry-key\",")
            .replace("redacted body", "redacted body Bearer abc.def.ghi");
        String good = validRequest("\"idempotencyKey\":\"retry-key\",");

        mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(bad))
            .andExpect(status().isBadRequest());
        mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(good))
            .andExpect(status().isAccepted());
    }

    @Test
    void publishFailureReturnsServiceUnavailable() throws Exception {
        publisher.nextResult = PublishResult.failed("nats unavailable");

        mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(validRequest(null)))
            .andExpect(status().isServiceUnavailable())
            .andExpect(jsonPath("$.accepted").value(false));
    }

    @Test
    void healthzReturnsApiStatus() throws Exception {
        mockMvc.perform(get("/healthz"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.status").value("ok"))
            .andExpect(jsonPath("$.component").value("ingress-api"));
    }

    @Test
    void statusReturnsRedactedQueueAndTargetSummary() throws Exception {
        String response = mockMvc.perform(get("/status"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.queue.pending").value(0))
            .andExpect(jsonPath("$.target.pressure").value("CLOSED"))
            .andReturn()
            .getResponse()
            .getContentAsString();

        assertThat(response)
            .doesNotContain("dataset_id")
            .doesNotContain("document_id")
            .doesNotContain("Bearer")
            .doesNotContain("/Users/");
    }

    private String validRequest(String optionalField) {
        return validRequestWithBody(optionalField, body());
    }

    private String validRequestWithBody(String optionalField, String body) {
        return """
            {
              "schemaVersion": "rag_ingress_enqueue.v1",
              %s
              "source": {"type":"local_pc","provider":"codex","project":"workspace-ragflow-advisor"},
              "payload": {
                "kind": "redacted_rag_ready_document",
                "redactionVersion": "redaction.v2",
                "document": {
                  "filename": "chunk.md",
                  "contentType": "text/markdown",
                  "body": %s,
                  "metadata": {"schema_version":"agent_knowledge_document.v2","result_type":"conversation_chunk"}
                }
              },
              "contentHash": "%s",
              "targetProfile": "ragflow-transcript-memory",
              "kind": "conversation_chunk"
            }
            """.formatted(optionalField == null ? "" : optionalField, jsonString(body), contentHash(body));
    }

    private String validRequestWithoutSource() {
        String body = body();
        return """
            {
              "schemaVersion": "rag_ingress_enqueue.v1",
              "payload": {
                "kind": "redacted_rag_ready_document",
                "redactionVersion": "redaction.v2",
                "document": {
                  "filename": "chunk.md",
                  "contentType": "text/markdown",
                  "body": %s,
                  "metadata": {"schema_version":"agent_knowledge_document.v2","result_type":"conversation_chunk"}
                }
              },
              "contentHash": "%s",
              "targetProfile": "ragflow-transcript-memory",
              "kind": "conversation_chunk"
            }
            """.formatted(jsonString(body), contentHash(body));
    }

    private String body() {
        return """
            ---
            schema_version: agent_knowledge_document.v2
            result_type: conversation_chunk
            ---
            redacted body
            """;
    }

    private String contentHash(String body) {
        return ContentHashVerifier.sha256Hex(body);
    }

    private String jsonString(String value) {
        return "\"" + value
            .replace("\\", "\\\\")
            .replace("\"", "\\\"")
            .replace("\n", "\\n") + "\"";
    }

    private static final class FakePublisher implements IngestPublisher {
        private PublishResult nextResult = PublishResult.accepted("job-test-id");
        private int publishCount;

        @Override
        public PublishResult publish(com.local.ragingressqueue.core.IngestJob job) {
            publishCount++;
            return nextResult;
        }
    }
}
