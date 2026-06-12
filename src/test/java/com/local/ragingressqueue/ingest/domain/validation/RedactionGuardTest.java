package com.local.ragingressqueue.ingest.domain.validation;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class RedactionGuardTest {
    private final RedactionGuard guard = new RedactionGuard();

    @Test
    void rejectsBearerToken() {
        assertThat(guard.inspect(redactedBody() + "\nBearer abc.def.ghi"))
            .anyMatch(violation -> violation.contains("Bearer"));
    }

    @Test
    void rejectsRawDatasetAndDocumentIds() {
        assertThat(guard.inspect(redactedBody() + "\ndataset_id: raw\ndocument_id: raw"))
            .anyMatch(violation -> violation.contains("dataset_id"))
            .anyMatch(violation -> violation.contains("document_id"));
    }

    @Test
    void rejectsPrivatePath() {
        assertThat(guard.inspect(redactedBody() + "\n/Users/operator/private/transcript.jsonl"))
            .anyMatch(violation -> violation.contains("/Users/"));
    }

    @Test
    void rejectsRawTranscriptFixture() {
        String rawTranscript = redactedBody() + "\nUserPromptSubmit raw_transcript /Users/operator/session.jsonl";

        assertThat(guard.inspect(rawTranscript))
            .anyMatch(violation -> violation.contains("raw_transcript"))
            .anyMatch(violation -> violation.contains("/Users/"));
    }

    @Test
    void acceptsValidRedactedFrontmatter() {
        assertThat(guard.inspect(redactedBody())).isEmpty();
    }

    @Test
    void denylistIsAvailableAsRuntimeResource() {
        assertThat(RedactionGuard.class.getClassLoader().getResource("redaction-denylist.txt")).isNotNull();
    }

    @Test
    void rejectsForbiddenValuesOutsideBody() {
        IngestJob job = new IngestJob(
            Map.of("provider", "codex", "project", "/Users/operator/private"),
            new DocumentPayload(
                "redacted_rag_ready_document",
                "redaction.v2",
                "dataset_id.md",
                "text/markdown",
                redactedBody(),
                Map.of("documentId", "raw-doc", "safe", "value")
            ),
            ContentHashVerifier.sha256Hex(redactedBody()),
            "ragflow-transcript-memory",
            "conversation_chunk",
            "access_token_123"
        );

        assertThat(guard.inspectJob(job))
            .anyMatch(violation -> violation.contains("source.project"))
            .anyMatch(violation -> violation.contains("payload.document.filename"))
            .anyMatch(violation -> violation.contains("payload.document.metadata.documentId"))
            .anyMatch(violation -> violation.contains("idempotencyKey"));
    }

    @Test
    void allowsBenignTokenWordButRejectsTokenSecretAssignment() {
        // Bare prose mention of the word "token" must not be a false-positive rejection.
        assertThat(guard.inspect(redactedBody() + "\nthe access token expired and the token limit was reached"))
            .isEmpty();
        // A token assigned to a secret-shaped value is still rejected.
        assertThat(guard.inspect(redactedBody() + "\ntoken: ghp_ABCdef0123456789xyzQQ012345"))
            .anyMatch(violation -> violation.contains("token"));
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
