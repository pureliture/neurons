package com.local.ragingressqueue.common.logging;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.ingest.domain.validation.ContentHashVerifier;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class SafeJobSummaryTest {
    @Test
    void safeJobSummaryDoesNotExposeBodyOrMetadata() {
        String body = redactedBody() + "\n/Users/operator/private";
        IngestJob job = validJob(body);

        String summary = SafeJobSummary.from(job, "queued").toString();

        assertThat(summary)
            .doesNotContain("redacted body")
            .doesNotContain("/Users/")
            .doesNotContain("dataset_id")
            .doesNotContain("document_id")
            .contains("ragflow-transcript-memory")
            .contains("queued");
    }

    @Test
    void domainToStringDoesNotExposeBody() {
        String body = redactedBody() + "\nsecret body content";
        IngestJob job = validJob(body);

        assertThat(job.toString()).doesNotContain("secret body content");
        assertThat(job.payload().toString()).doesNotContain("secret body content");
        assertThat(job.toString())
            .doesNotContain("dataset_id")
            .doesNotContain("document_id")
            .doesNotContain("/Users/")
            .doesNotContain("workspace-ragflow-advisor");
        assertThat(job.payload().toString())
            .doesNotContain("dataset_id")
            .doesNotContain("document_id")
            .doesNotContain("chunk.md");
    }

    @Test
    void safeJobSummaryRedactsUnsafeSlugFields() {
        String body = redactedBody();
        IngestJob job = new IngestJob(
            Map.of("provider", "codex", "project", "/Users/operator/private"),
            new DocumentPayload(
                "redacted_rag_ready_document",
                "redaction.v2",
                "chunk.md",
                "text/markdown",
                body,
                Map.of()
            ),
            ContentHashVerifier.sha256Hex(body),
            "ragflow-transcript-memory",
            "conversation_chunk",
            null
        );

        String summary = SafeJobSummary.from(job, "queued").toString();

        assertThat(summary).doesNotContain("/Users/");
        assertThat(summary).contains("project=redacted");
    }

    private IngestJob validJob(String body) {
        return new IngestJob(
            Map.of("provider", "codex", "project", "workspace-ragflow-advisor"),
            new DocumentPayload(
                "redacted_rag_ready_document",
                "redaction.v2",
                "chunk.md",
                "text/markdown",
                body,
                Map.of("dataset_id", "raw-dataset", "document_id", "raw-doc")
            ),
            ContentHashVerifier.sha256Hex(body),
            "ragflow-transcript-memory",
            "conversation_chunk",
            null
        );
    }

    private String redactedBody() {
        return """
            ---
            schema_version: agent_knowledge_document.v2
            result_type: conversation_chunk
            ---
            redacted body
            """;
    }
}
