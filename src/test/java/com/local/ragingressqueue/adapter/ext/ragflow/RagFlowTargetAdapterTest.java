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
    void reDeliverySkipsUploadEvenWhenSearchIndexStillReportsAbsent() {
        // Freshness race: RAGFlow's document search index may not yet reflect the just-uploaded
        // document, so findByContentHash reports absent. The recent-delivery cache must still dedup.
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        gateway.alwaysReportAbsent = true;
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway
        );

        IngestJob job = validJob();
        assertThat(adapter.deliver(job, "ragflow-transcript-memory").delivered()).isTrue();
        assertThat(adapter.deliver(job, "ragflow-transcript-memory").delivered()).isTrue();

        assertThat(gateway.uploadCount).isEqualTo(1);
    }

    @Test
    void cacheHitSkipsRedundantGatewayLookupOnReDelivery() {
        // Performance: a cached recent delivery must short-circuit before the extra RAGFlow GET.
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway
        );

        IngestJob job = validJob();
        adapter.deliver(job, "ragflow-transcript-memory");
        adapter.deliver(job, "ragflow-transcript-memory");

        assertThat(gateway.uploadCount).isEqualTo(1);
        assertThat(gateway.findByContentHashCallCount).isEqualTo(1);
    }

    @Test
    void dedupSkipsIdenticalReDeliveryForNonTranscriptKind() {
        // Dedup is adapter-shared across kinds: an identical session_summary re-delivery is a no-op,
        // while distinct content still uploads. Content_hash equality, not kind, governs the skip.
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-session-memory", "ds_session"),
            gateway
        );

        adapter.deliver(sessionMemoryJob(), "ragflow-session-memory");
        adapter.deliver(sessionMemoryJob(), "ragflow-session-memory");

        assertThat(gateway.uploadCount).isEqualTo(1);
    }

    @Test
    void retryAfterPostUploadFailureResumesMetadataAndParseWithoutReUploading() {
        // Partial-success window: the upload succeeds but a post-upload step (metadata) throws, so the
        // delivery is reported failed and the message is retried. The retry must (a) NOT create a
        // duplicate (no re-upload, because the cache remembers the uploaded document) and (b) resume
        // the still-missing metadata/parse steps against the uploaded document id instead of
        // short-circuiting to "delivered". Otherwise a document persists without its metadata/parse
        // side effects and is silently lost. findByContentHash stays absent (search index lag).
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        gateway.alwaysReportAbsent = true;
        gateway.failMetadata = true;
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway
        );

        IngestJob job = validJob();
        DeliveryResult first = adapter.deliver(job, "ragflow-transcript-memory");
        assertThat(first.delivered()).isFalse();
        assertThat(gateway.uploadCount).isEqualTo(1);
        assertThat(gateway.parseRequested).isFalse();

        // The transient post-upload failure clears before the retry.
        gateway.failMetadata = false;
        DeliveryResult retry = adapter.deliver(job, "ragflow-transcript-memory");

        assertThat(retry.delivered()).isTrue();
        assertThat(gateway.uploadCount).isEqualTo(1);
        assertThat(gateway.metadataDocumentId).isEqualTo("doc_1");
        assertThat(gateway.parseRequested).isTrue();
        assertThat(gateway.parseDocumentId).isEqualTo("doc_1");
    }

    @Test
    void finalizedReDeliveryShortCircuitsWithoutRepeatingMetadataOrParse() {
        // Once a delivery has fully completed (upload + metadata + parse), a later re-delivery must
        // short-circuit on the finalized cache entry without re-running any gateway step.
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway
        );

        IngestJob job = validJob();
        assertThat(adapter.deliver(job, "ragflow-transcript-memory").delivered()).isTrue();
        assertThat(adapter.deliver(job, "ragflow-transcript-memory").delivered()).isTrue();

        assertThat(gateway.uploadCount).isEqualTo(1);
        assertThat(gateway.metadataCallCount).isEqualTo(1);
        assertThat(gateway.parseCallCount).isEqualTo(1);
        assertThat(gateway.findByContentHashCallCount).isEqualTo(1);
    }

    @Test
    void retryAfterParseFailureResumesFromParseWithoutReapplyingMetadata() {
        // If only the parse step failed (metadata already succeeded), the retry must resume from parse
        // alone: it must not re-upload and must not re-apply metadata. Replaying metadata would be a
        // redundant RAGFlow call and contradicts resuming only the step that failed.
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        gateway.alwaysReportAbsent = true;
        gateway.failParse = true;
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway
        );

        IngestJob job = validJob();
        DeliveryResult first = adapter.deliver(job, "ragflow-transcript-memory");
        assertThat(first.delivered()).isFalse();
        assertThat(gateway.metadataCallCount).isEqualTo(1);

        gateway.failParse = false;
        DeliveryResult retry = adapter.deliver(job, "ragflow-transcript-memory");

        assertThat(retry.delivered()).isTrue();
        assertThat(gateway.uploadCount).isEqualTo(1);
        assertThat(gateway.metadataCallCount).isEqualTo(1);
        assertThat(gateway.parseCallCount).isEqualTo(2);
        assertThat(gateway.parseDocumentId).isEqualTo("doc_1");
    }

    @Test
    void blankFilenameStillDedupsByEmbeddingTheHashInASynthesizedName() {
        // A blank original filename must not silently disable dedup: the second identical delivery must
        // be recognised as already present rather than uploaded again.
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway
        );

        IngestJob job = blankFilenameJob();
        String expectedFragment = job.contentHash().substring(7, 19);

        assertThat(adapter.deliver(job, "ragflow-transcript-memory").delivered()).isTrue();
        assertThat(adapter.deliver(job, "ragflow-transcript-memory").delivered()).isTrue();

        assertThat(gateway.uploadCount).isEqualTo(1);
        assertThat(gateway.uploadFilename).endsWith("-" + expectedFragment);
    }

    @Test
    void existingRagflowDocumentIsRecordedSoLaterReDeliveriesSkipTheLookup() {
        // First delivery finds the document already present in RAGFlow (no prior cache entry); the
        // result must be a no-op upload AND the presence must be cached so later re-deliveries skip
        // even the RAGFlow lookup.
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        IngestJob job = validJob();
        gateway.preexistingFragments.add(job.contentHash().substring(7, 19));
        RagFlowTargetAdapter adapter = new RagFlowTargetAdapter(
            true,
            "http://127.0.0.1:9380",
            "token",
            Map.of("ragflow-transcript-memory", "ds_1"),
            gateway
        );

        assertThat(adapter.deliver(job, "ragflow-transcript-memory").delivered()).isTrue();
        assertThat(adapter.deliver(job, "ragflow-transcript-memory").delivered()).isTrue();

        assertThat(gateway.uploadCount).isEqualTo(0);
        assertThat(gateway.findByContentHashCallCount).isEqualTo(1);
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

    private IngestJob blankFilenameJob() {
        String body = """
            ---
            schema_version: agent_knowledge_document.v2
            result_type: conversation_chunk
            ---
            body with a blank source filename
            """;
        return new IngestJob(
            Map.of("provider", "codex", "project", "workspace-ragflow-advisor"),
            new DocumentPayload(
                "redacted_rag_ready_document",
                "redaction.v2",
                "   ",
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
        private boolean failMetadata;
        private boolean failParse;
        private boolean failPressure;
        private boolean alwaysReportAbsent;
        private int findByContentHashCallCount;
        private String uploadDatasetId;
        private String uploadFilename;
        private Map<String, String> metadata = new HashMap<>();
        private String metadataDocumentId;
        private int metadataCallCount;
        private boolean parseRequested;
        private String parseDocumentId;
        private int parseCallCount;
        private RagFlowPressureSnapshot pressureSnapshot = new RagFlowPressureSnapshot(0, 0, 0, 100, 100);
        private int uploadCount;
        private final java.util.Set<String> uploadedFragments = new java.util.HashSet<>();
        private final java.util.Set<String> preexistingFragments = new java.util.HashSet<>();

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
            metadataCallCount++;
            if (failMetadata) {
                throw new RagFlowDeliveryException("metadata boom");
            }
            this.metadataDocumentId = documentId;
            this.metadata = metadata;
        }

        @Override
        public void requestParse(String baseUrl, String apiKey, String datasetId, String documentId) {
            parseCallCount++;
            if (failParse) {
                throw new RagFlowDeliveryException("parse boom");
            }
            parseRequested = true;
            parseDocumentId = documentId;
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
            findByContentHashCallCount++;
            if (alwaysReportAbsent) {
                return false;
            }
            return uploadedFragments.contains(contentHashFragment)
                || preexistingFragments.contains(contentHashFragment);
        }

        private void recordFragment(String filename) {
            if (filename == null) {
                return;
            }
            // Mirror HttpRagFlowGateway#matchesContentHashFragment: the fragment is the token after the
            // last '-' in the base name (the name with any final extension stripped), including the
            // no-extension case.
            int lastDot = filename.lastIndexOf('.');
            String baseName = lastDot > 0 ? filename.substring(0, lastDot) : filename;
            int dash = baseName.lastIndexOf('-');
            if (dash >= 0) {
                uploadedFragments.add(baseName.substring(dash + 1));
            }
        }
    }
}
