package com.local.ragingressqueue.ingest.api;

import com.local.ragingressqueue.ingest.domain.validation.ContentHashVerifier;
import com.local.ragingressqueue.queue.port.IngestPublisher;
import com.local.ragingressqueue.queue.port.PublishResult;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.header;
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
    void healthzHasStableJsonForNonJsonAcceptWithoutPublishing() throws Exception {
        mockMvc.perform(get("/healthz").accept(MediaType.TEXT_PLAIN))
            .andExpect(status().isOk())
            .andExpect(header().string("Content-Type", "application/json"))
            .andExpect(header().string("Cache-Control", "no-store"))
            .andExpect(jsonPath("$.status").value("ok"));
        assertThat(publisher.publishCount).isZero();
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
    void nullSourceFieldsReturnBadRequestWithoutPublishing() throws Exception {
        for (String field : new String[] {"provider", "project"}) {
            String request = validRequest(null).replace("\"" + field + "\":\"" +
                (field.equals("provider") ? "codex" : "workspace-index-advisor") + "\"",
                "\"" + field + "\":null");
            mockMvc.perform(post("/v1/ingest/enqueue")
                    .contentType(MediaType.APPLICATION_JSON)
                    .content(request))
                .andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.accepted").value(false));
        }
        assertThat(publisher.publishCount).isZero();
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
    void sessionMemoryTargetProfileIsAccepted() throws Exception {
        mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(validSessionMemoryRequest()))
            .andExpect(status().isAccepted())
            .andExpect(jsonPath("$.accepted").value(true));

        assertThat(publisher.publishCount).isEqualTo(1);
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
        // G2 secrets-only POST guard: an actual secret in metadata is still rejected
        // (benign public terms like documentId are now cleaned by the worker, not here).
        String response = mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON)
                .content(validRequest(null).replace("\"result_type\":\"conversation_chunk\"", "\"authHeader\":\"Bearer ghp_ABCdef0123456789xyzQQ\"")))
            .andExpect(status().isBadRequest())
            .andReturn()
            .getResponse()
            .getContentAsString();

        assertThat(response).doesNotContain("ghp_ABCdef0123456789xyzQQ");
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
            .andExpect(header().string("Cache-Control", "no-store"))
            .andExpect(jsonPath("$.status").value("ok"))
            .andExpect(jsonPath("$.component").value("ingress-api"));
        assertThat(publisher.publishCount).isZero();
    }

    @Test
    void statusPreventsStaleCachingWithoutPublishing() throws Exception {
        mockMvc.perform(get("/status"))
            .andExpect(status().isOk())
            .andExpect(header().string("Cache-Control", "no-store"))
            .andExpect(header().string("Content-Type", "application/json"))
            .andExpect(jsonPath("$.externalStatus").value("not_configured"))
            .andExpect(jsonPath("$.queue.pending").value(0));
        assertThat(publisher.publishCount).isZero();
    }

    @Test
    void statusReturnsRedactedQueueAndTargetSummary() throws Exception {
        String response = mockMvc.perform(get("/status"))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.externalStatus").value("not_configured"))
            .andExpect(jsonPath("$.queue.pending").value(0))
            .andExpect(jsonPath("$.target.pressure").value("CLOSED"))
            .andExpect(jsonPath("$.target.reason").value("not_configured"))
            .andReturn()
            .getResponse()
            .getContentAsString();

        assertThat(response)
            .doesNotContain("dataset_id")
            .doesNotContain("document_id")
            .doesNotContain("Bearer")
            .doesNotContain("/Users/");
    }

    @Test
    void validValidationReturnsOkWithoutPublish() throws Exception {
        mockMvc.perform(post("/v1/ingest/validate")
                .contentType(MediaType.APPLICATION_JSON).content(validRequest(null)))
            .andExpect(status().isOk())
            .andExpect(jsonPath("$.status").value("valid"))
            .andExpect(jsonPath("$.errors").isEmpty())
            .andExpect(jsonPath("$.jobId").doesNotExist())
            .andExpect(jsonPath("$.accepted").doesNotExist());
        assertThat(publisher.publishCount).isZero();
    }

    @Test
    void invalidValidationMatchesEnqueueStaticErrors() throws Exception {
        for (String invalid : new String[] {
            validRequest(null).replace("rag_ingress_enqueue.v1", "unsupported_schema"),
            validRequest(null).replace("\"kind\": \"conversation_chunk\"", "\"kind\": \"unknown_kind\""),
            validRequest(null).replace("index-transcript-memory", "unsupported_profile"),
            validRequest(null).replace("\"contentHash\":", "\"missingHash\":")
        }) {
            var submitted = mockMvc.perform(post("/v1/ingest/enqueue")
                .contentType(MediaType.APPLICATION_JSON).content(invalid)).andExpect(status().isBadRequest())
                .andReturn().getResponse().getContentAsString();
            var checked = mockMvc.perform(post("/v1/ingest/validate")
                .contentType(MediaType.APPLICATION_JSON).content(invalid)).andExpect(status().isBadRequest())
                .andExpect(jsonPath("$.status").value("rejected")).andReturn().getResponse().getContentAsString();
            assertThat(checked).contains(submitted.contains("schemaVersion must")
                ? "schemaVersion must be rag_ingress_enqueue.v1" : "request rejected");
        }
        assertThat(publisher.publishCount).isZero();
    }

    @Test
    void reservedDocumentReferenceValidationReturnsUnprocessableEntity() throws Exception {
        mockMvc.perform(post("/v1/ingest/validate").contentType(MediaType.APPLICATION_JSON)
                .content(validRequest(null).replace("redacted_rag_ready_document", "redacted_document_ref")))
            .andExpect(status().isUnprocessableEntity())
            .andExpect(jsonPath("$.status").value("unsupported_payload"))
            .andExpect(jsonPath("$.errors[0]").value("redacted_document_ref is reserved but disabled"));
        assertThat(publisher.publishCount).isZero();
    }

    @Test
    void validationNeverCallsIdempotencyStore() throws Exception {
        var store = new com.local.ragingressqueue.ingest.service.IdempotencyStore() {
            @Override public boolean conflicts(String key, String hash) {
                throw new AssertionError("validation must not access mutable store");
            }
        };
        var isolated = MockMvcBuilders.standaloneSetup(new IngressController(publisher,
            new com.local.ragingressqueue.status.service.StatusService(),
            new com.local.ragingressqueue.ingest.domain.validation.IngestJobValidator(),
            new com.local.ragingressqueue.ingest.domain.validation.RedactionGuard(), store)).build();
        for (String input : new String[] {validRequest(null), validRequestWithoutSource(),
            validRequest(null).replace("redacted_rag_ready_document", "redacted_document_ref")}) {
            isolated.perform(post("/v1/ingest/validate").contentType(MediaType.APPLICATION_JSON).content(input));
        }
        String checked = validRequest("\"idempotencyKey\":\"preflight-key\",");
        mockMvc.perform(post("/v1/ingest/validate").contentType(MediaType.APPLICATION_JSON).content(checked))
            .andExpect(status().isOk());
        mockMvc.perform(post("/v1/ingest/enqueue").contentType(MediaType.APPLICATION_JSON)
                .content(validRequestWithBody("\"idempotencyKey\":\"preflight-key\",", body() + "changed")))
            .andExpect(status().isAccepted());
    }

    @Test
    void missingOrNullSourceValidationReturnsBadRequest() throws Exception {
        for (String input : new String[] {validRequestWithoutSource(),
            validRequest(null).replace("\"provider\":\"codex\"", "\"provider\":null"),
            validRequest(null).replace("\"project\":\"workspace-index-advisor\"", "\"project\":null")}) {
            mockMvc.perform(post("/v1/ingest/validate").contentType(MediaType.APPLICATION_JSON).content(input))
                .andExpect(status().isBadRequest()).andExpect(jsonPath("$.status").value("rejected"));
        }
        assertThat(publisher.publishCount).isZero();
    }

    @Test
    void sensitiveValidationErrorsNeverEchoInput() throws Exception {
        String syntheticBody = body() + " Bearer synthetic.nonsecret.fixture";
        for (String input : new String[] {validRequestWithBody("\"idempotencyKey\":\"private-fixture-key\",", syntheticBody),
            validRequest(null).replace("\"result_type\":\"conversation_chunk\"",
                "\"authHeader\":\"Bearer synthetic.nonsecret.fixture\"")}) {
            String response = mockMvc.perform(post("/v1/ingest/validate").contentType(MediaType.APPLICATION_JSON)
                    .content(input)).andExpect(status().isBadRequest()).andReturn().getResponse().getContentAsString();
            assertThat(response).doesNotContain("synthetic", "redacted body", "codex", "workspace-index-advisor",
                "private-fixture-key", "contentHash", "payload", "source", "stack", "jobId");
        }
        assertThat(publisher.publishCount).isZero();
    }

    @Test
    void malformedJsonValidationDoesNotEchoRequest() throws Exception {
        for (String input : new String[] {"{\"source\":\"synthetic-private-marker", "null", ""}) {
            String response = mockMvc.perform(post("/v1/ingest/validate").contentType(MediaType.APPLICATION_JSON)
                .content(input)).andExpect(status().isBadRequest()).andReturn().getResponse().getContentAsString();
            assertThat(response).doesNotContain("synthetic-private-marker", "source", "exception", "stack");
        }
        assertThat(publisher.publishCount).isZero();
    }

    @Test
    void validationIsIndependentOfQueueAndDatabaseAvailability() throws Exception {
        IngestPublisher unavailable = job -> { throw new AssertionError("queue access forbidden"); };
        var unavailableStatus = new com.local.ragingressqueue.status.service.StatusService() {
            @Override public java.util.Map<String, Object> currentStatus() {
                throw new AssertionError("status/backend access forbidden");
            }
        };
        var isolated = MockMvcBuilders.standaloneSetup(new IngressController(unavailable, unavailableStatus)).build();
        isolated.perform(post("/v1/ingest/validate").contentType(MediaType.APPLICATION_JSON).content(validRequest(null)))
            .andExpect(status().isOk()).andExpect(jsonPath("$.status").value("valid"));
    }

    @Test
    void enqueueContractRemainsUnchanged() throws Exception {
        String first = validRequest("\"idempotencyKey\":\"enqueue-regression\",");
        mockMvc.perform(post("/v1/ingest/enqueue").contentType(MediaType.APPLICATION_JSON).content(first))
            .andExpect(status().isAccepted()).andExpect(jsonPath("$.accepted").value(true));
        mockMvc.perform(post("/v1/ingest/validate").contentType(MediaType.APPLICATION_JSON)
                .content(validRequestWithBody("\"idempotencyKey\":\"enqueue-regression\",", body() + "changed")))
            .andExpect(status().isOk());
        mockMvc.perform(post("/v1/ingest/enqueue").contentType(MediaType.APPLICATION_JSON)
                .content(validRequestWithBody("\"idempotencyKey\":\"enqueue-regression\",", body() + "changed")))
            .andExpect(status().isConflict());
        publisher.nextResult = PublishResult.failed("synthetic offline");
        mockMvc.perform(post("/v1/ingest/enqueue").contentType(MediaType.APPLICATION_JSON).content(validRequest(null)))
            .andExpect(status().isServiceUnavailable());
    }

    @Test
    void validationRouteUsesExistingIngressProtection() throws Exception {
        // Application-level scope is shared; actual gateway policy needs separate deployment evidence.
        assertThat(IngressController.class.getAnnotation(org.springframework.context.annotation.Profile.class).value())
            .containsExactly("api");
        var mapping = IngressController.class.getMethod("validate", com.local.ragingressqueue.ingest.dto.EnqueueRequest.class)
            .getAnnotation(org.springframework.web.bind.annotation.PostMapping.class);
        assertThat(mapping.value()).containsExactly("/v1/ingest/validate");
    }

    private String validRequest(String optionalField) {
        return validRequestWithBody(optionalField, body());
    }

    private String validRequestWithBody(String optionalField, String body) {
        return """
            {
              "schemaVersion": "rag_ingress_enqueue.v1",
              %s
              "source": {"type":"local_pc","provider":"codex","project":"workspace-index-advisor"},
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
              "targetProfile": "index-transcript-memory",
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
              "targetProfile": "index-transcript-memory",
              "kind": "conversation_chunk"
            }
            """.formatted(jsonString(body), contentHash(body));
    }

    private String validSessionMemoryRequest() {
        String body = """
            ---
            schema_version: agent_knowledge_document.v2
            result_type: session_summary
            ---
            redacted session summary
            """;
        return """
            {
              "schemaVersion": "rag_ingress_enqueue.v1",
              "source": {"type":"local_pc","provider":"codex","project":"workspace-index-advisor"},
              "payload": {
                "kind": "redacted_rag_ready_document",
                "redactionVersion": "redaction.v2",
                "document": {
                  "filename": "session-summary.md",
                  "contentType": "text/markdown",
                  "body": %s,
                  "metadata": {"schema_version":"agent_knowledge_document.v2","result_type":"session_summary"}
                }
              },
              "contentHash": "%s",
              "targetProfile": "index-session-memory",
              "kind": "session_summary"
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
        public PublishResult publish(com.local.ragingressqueue.ingest.domain.IngestJob job) {
            publishCount++;
            return nextResult;
        }
    }
}
