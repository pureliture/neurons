package com.local.ragingressqueue.target;

import com.local.ragingressqueue.core.DocumentPayload;
import com.local.ragingressqueue.core.IngestJob;
import com.local.ragingressqueue.core.TargetPressure;
import com.local.ragingressqueue.core.validation.ContentHashVerifier;
import org.junit.jupiter.api.Test;

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
}
