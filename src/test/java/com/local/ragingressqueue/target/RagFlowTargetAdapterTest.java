package com.local.ragingressqueue.target;

import com.local.ragingressqueue.core.DocumentPayload;
import com.local.ragingressqueue.core.IngestJob;
import com.local.ragingressqueue.core.TargetPressure;
import com.local.ragingressqueue.core.validation.ContentHashVerifier;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class RagFlowTargetAdapterTest {
    @Test
    void disabledAdapterReportsClosedPressureAndDoesNotClaimDelivery() {
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(false);

        assertThat(adapter.checkPressure("ragflow-transcript-memory")).isEqualTo(TargetPressure.CLOSED);
        assertThat(adapter.deliver(validJob(), "ragflow-transcript-memory").delivered()).isFalse();
    }

    @Test
    void statusSnapshotDoesNotExposeTargetPrivateReference() {
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(false);

        TargetStatusSnapshot snapshot = adapter.getStatus(validJob(), "ragflow-transcript-memory");

        assertThat(snapshot.redactedTargetRef()).isEqualTo("redacted");
        assertThat(snapshot.toString())
            .doesNotContain("dataset_id")
            .doesNotContain("document_id")
            .doesNotContain("/Users/");
    }

    @Test
    void enabledAdapterWithoutDatasetStaysClosed() {
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of(),
            new FakeRagFlowGateway()
        );

        assertThat(adapter.checkPressure("ragflow-transcript-memory")).isEqualTo(TargetPressure.CLOSED);
        assertThat(adapter.deliver(validJob(), "ragflow-transcript-memory").delivered()).isFalse();
    }

    @Test
    void enabledAdapterUploadsMetadataAndRequestsParse() {
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway
        );

        DeliveryResult result = adapter.deliver(validJob(), "ragflow-transcript-memory");

        assertThat(result.delivered()).isTrue();
        assertThat(result.targetRef()).isEqualTo("redacted");
        assertThat(gateway.uploadDatasetId).isEqualTo("ds_1");
        assertThat(gateway.uploadFilename).isEqualTo("chunk.md");
        assertThat(gateway.metadata).containsEntry("project", "workspace-ragflow-advisor");
        assertThat(gateway.metadata).containsEntry("provider", "codex");
        assertThat(gateway.metadata).containsKey("content_hash_prefix");
        assertThat(gateway.parseRequested).isTrue();
    }

    @Test
    void enabledAdapterReturnsFailedWhenRagFlowRejectsUpload() {
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        gateway.failUpload = true;
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway
        );

        DeliveryResult result = adapter.deliver(validJob(), "ragflow-transcript-memory");

        assertThat(result.delivered()).isFalse();
        assertThat(result.error()).isEqualTo("ragflow delivery failed");
        assertThat(result.toString()).doesNotContain("token").doesNotContain("doc_1");
    }

    private IngestJob validJob() {
        String body = """
            ---
            schema_version: agent_knowledge_document.v2
            result_type: conversation_chunk
            ---
            redacted body
            """;
        return new IngestJob(
            Map.of("provider", "codex", "project", "workspace-ragflow-advisor"),
            new DocumentPayload(
                "redacted_rag_ready_document",
                "redaction.v2",
                "chunk.md",
                "text/markdown",
                body,
                Map.of("schema_version", "agent_knowledge_document.v2", "result_type", "conversation_chunk")
            ),
            ContentHashVerifier.sha256Hex(body),
            "ragflow-transcript-memory",
            "conversation_chunk",
            null
        );
    }

    private static final class FakeRagFlowGateway implements RagFlowGateway {
        private boolean failUpload;
        private String uploadDatasetId;
        private String uploadFilename;
        private Map<String, String> metadata = new HashMap<>();
        private boolean parseRequested;

        @Override
        public RagFlowDocumentRef uploadDocument(
            String baseUrl,
            String apiKey,
            String datasetId,
            DocumentPayload payload
        ) {
            if (failUpload) {
                throw new RagFlowDeliveryException("boom");
            }
            uploadDatasetId = datasetId;
            uploadFilename = payload.filename();
            return new RagFlowDocumentRef("doc_1", "UNSTART");
        }

        @Override
        public void updateMetadata(String baseUrl, String apiKey, String datasetId, String documentId, Map<String, String> metadata) {
            this.metadata = metadata;
        }

        @Override
        public void requestParse(String baseUrl, String apiKey, String datasetId, String documentId) {
            parseRequested = true;
        }
    }
}
