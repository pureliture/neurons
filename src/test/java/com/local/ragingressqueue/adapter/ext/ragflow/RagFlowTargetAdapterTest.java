package com.local.ragingressqueue.adapter.ext.ragflow;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.delivery.domain.DeliveryResult;
import com.local.ragingressqueue.delivery.domain.TargetPressure;
import com.local.ragingressqueue.ingest.domain.validation.ContentHashVerifier;
import com.local.ragingressqueue.common.IngestStatus;
import com.local.ragingressqueue.target.port.TargetStatusSnapshot;
import org.junit.jupiter.api.Test;

import java.util.HashMap;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class RagFlowTargetAdapterTest {
    @Test
    void disabledAdapterReportsClosedPressureAndDoesNotClaimDelivery() {
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(false);

        assertThat(adapter.pressureSnapshot("ragflow-transcript-memory").pressure()).isEqualTo(TargetPressure.CLOSED);
        assertThat(adapter.deliver(validJob(), "ragflow-transcript-memory").delivered()).isFalse();
    }

    @Test
    void statusSnapshotDoesNotExposeTargetPrivateReference() {
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(false);

        TargetStatusSnapshot snapshot = adapter.getStatus(validJob(), "ragflow-transcript-memory");

        // Generic backend-neutral status is surfaced on the snapshot; a disabled adapter fails closed.
        assertThat(snapshot.status()).isEqualTo(IngestStatus.FAILED);
        assertThat(snapshot.redactedTargetRef()).isEqualTo("redacted");
        assertThat(snapshot.toString())
            .doesNotContain("dataset_id")
            .doesNotContain("document_id")
            .doesNotContain("/Users/");
    }

    @Test
    void configuredAdapterSurfacesBackendNeutralStatusWithoutLeakingResourceIds() {
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            new FakeRagFlowGateway()
        );

        TargetStatusSnapshot snapshot = adapter.getStatus(validJob(), "ragflow-transcript-memory");

        assertThat(snapshot.status()).isEqualTo(IngestStatus.ACCEPTED);
        assertThat(snapshot.toString()).doesNotContain("ds_1").doesNotContain("token");
    }

    @Test
    void configuredAdapterFailsClosedForNullTargetProfile() {
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            new FakeRagFlowGateway()
        );

        assertThat(adapter.pressureSnapshot(null).pressure()).isEqualTo(TargetPressure.CLOSED);
        assertThat(adapter.getStatus(validJob(), null).status()).isEqualTo(IngestStatus.FAILED);
        assertThat(adapter.deliver(validJob(), null).delivered()).isFalse();
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

        assertThat(adapter.pressureSnapshot("ragflow-transcript-memory").pressure()).isEqualTo(TargetPressure.CLOSED);
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

        IngestJob job = validJob();
        DeliveryResult result = adapter.deliver(job, "ragflow-transcript-memory");

        String expectedFragment = job.contentHash().substring(7, 19);
        assertThat(result.delivered()).isTrue();
        assertThat(result.targetRef()).isEqualTo("redacted");
        assertThat(gateway.uploadDatasetId).isEqualTo("ds_1");
        assertThat(gateway.uploadFilename).isEqualTo("chunk-" + expectedFragment + ".md");
        assertThat(gateway.metadata).containsEntry("project", "workspace-ragflow-advisor");
        assertThat(gateway.metadata).containsEntry("provider", "codex");
        assertThat(gateway.metadata).containsKey("content_hash_prefix");
        assertThat(gateway.parseRequested).isTrue();
    }

    @Test
    void secondDeliveryWithSameContentHashIsSkippedAsAlreadyPresent() {
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway
        );

        IngestJob job = validJob();

        DeliveryResult first = adapter.deliver(job, "ragflow-transcript-memory");
        assertThat(first.delivered()).isTrue();
        assertThat(gateway.uploadCount).isEqualTo(1);

        DeliveryResult second = adapter.deliver(job, "ragflow-transcript-memory");
        assertThat(second.delivered()).isTrue();
        assertThat(gateway.uploadCount).isEqualTo(1);
    }

    @Test
    void twoJobsWithDifferentContentHashesAreBothUploaded() {
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway
        );

        IngestJob jobA = validJob();
        IngestJob jobB = jobB();

        DeliveryResult first = adapter.deliver(jobA, "ragflow-transcript-memory");
        DeliveryResult second = adapter.deliver(jobB, "ragflow-transcript-memory");

        assertThat(first.delivered()).isTrue();
        assertThat(second.delivered()).isTrue();
        assertThat(gateway.uploadCount).isEqualTo(2);
    }

    @Test
    void enabledAdapterRoutesSessionMemoryProfileToSessionDataset() {
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-session-memory", "ds_session"),
            gateway
        );

        DeliveryResult result = adapter.deliver(sessionMemoryJob(), "ragflow-session-memory");

        assertThat(result.delivered()).isTrue();
        assertThat(gateway.uploadDatasetId).isEqualTo("ds_session");
        assertThat(gateway.metadata).containsEntry("target_profile", "ragflow-session-memory");
        assertThat(gateway.metadata).containsEntry("kind", "session_summary");
    }

    @Test
    void enabledAdapterThrottlesWhenRagFlowRunningBacklogReachesLimit() {
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        gateway.pressureSnapshot = new RagFlowPressureSnapshot(20, 0, 0, 100, 120);
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway,
            new RagFlowPressurePolicy(20, 5, 100, 25)
        );

        assertThat(adapter.pressureSnapshot("ragflow-transcript-memory").pressure()).isEqualTo(TargetPressure.THROTTLED);
    }

    @Test
    void enabledAdapterClosesWhenRagFlowBacklogReachesHardLimit() {
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        gateway.pressureSnapshot = new RagFlowPressureSnapshot(100, 0, 0, 100, 200);
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway,
            new RagFlowPressurePolicy(20, 5, 100, 25)
        );

        assertThat(adapter.pressureSnapshot("ragflow-transcript-memory").pressure()).isEqualTo(TargetPressure.CLOSED);
    }

    @Test
    void enabledAdapterFailsClosedWhenRagFlowPressureCannotBeRead() {
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        gateway.failPressure = true;
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway,
            new RagFlowPressurePolicy(20, 5, 100, 25)
        );

        assertThat(adapter.pressureSnapshot("ragflow-transcript-memory").pressure()).isEqualTo(TargetPressure.CLOSED);
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

    private IngestJob sessionMemoryJob() {
        String body = """
            ---
            schema_version: agent_knowledge_document.v2
            result_type: session_summary
            ---
            redacted session summary
            """;
        return new IngestJob(
            Map.of("provider", "codex", "project", "workspace-ragflow-advisor"),
            new DocumentPayload(
                "redacted_rag_ready_document",
                "redaction.v2",
                "session-summary.md",
                "text/markdown",
                body,
                Map.of("schema_version", "agent_knowledge_document.v2", "result_type", "session_summary")
            ),
            ContentHashVerifier.sha256Hex(body),
            "ragflow-session-memory",
            "session_summary",
            null
        );
    }

    private IngestJob jobB() {
        String body = """
            ---
            schema_version: agent_knowledge_document.v2
            result_type: conversation_chunk
            ---
            different body content
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
        private boolean failPressure;
        private String uploadDatasetId;
        private String uploadFilename;
        private Map<String, String> metadata = new HashMap<>();
        private boolean parseRequested;
        private RagFlowPressureSnapshot pressureSnapshot = new RagFlowPressureSnapshot(0, 0, 0, 100, 100);
        private int uploadCount;
        private final java.util.Set<String> uploadedFragments = new java.util.HashSet<>();

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
            uploadCount++;
            uploadDatasetId = datasetId;
            uploadFilename = payload.filename();
            recordFragment(payload.filename());
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

        @Override
        public RagFlowPressureSnapshot pressureSnapshot(String baseUrl, String apiKey, String datasetId) {
            if (failPressure) {
                throw new RagFlowDeliveryException("boom");
            }
            return pressureSnapshot;
        }

        @Override
        public boolean findByContentHash(String baseUrl, String apiKey, String datasetId, String contentHashFragment) {
            return uploadedFragments.contains(contentHashFragment);
        }

        private void recordFragment(String filename) {
            if (filename == null) {
                return;
            }
            int dash = filename.lastIndexOf('-');
            int dot = filename.lastIndexOf('.');
            if (dash >= 0 && dot > dash) {
                uploadedFragments.add(filename.substring(dash + 1, dot));
            }
        }
    }
}
