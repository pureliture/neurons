package com.local.ragingressqueue.ingest.domain.validation;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.common.TargetIndexingState;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class IngestJobValidatorTest {
    private final IngestJobValidator validator = new IngestJobValidator();

    @Test
    void acceptsRedactedRagReadyDocument() {
        IngestJob job = validJob();

        assertThat(validator.validate(job)).isEmpty();
    }

    @Test
    void rejectsRagflowNamedPayloadKindInPublicDto() {
        IngestJob job = validJob().withPayload(validJob().payload().withKind("ragflow_ready_document"));

        assertThat(validator.validate(job)).anyMatch(violation -> violation.contains("payload.kind"));
    }

    @Test
    void rejectsPrivateLocatorPayload() {
        IngestJob job = validJob().withPayload(validJob().payload().withKind("private_locator"));

        assertThat(validator.validate(job)).anyMatch(violation -> violation.contains("private_locator"));
    }

    @Test
    void rejectsNonCanonicalContentHash() {
        IngestJob job = validJob().withContentHash("sha256:redacted");

        assertThat(validator.validate(job)).anyMatch(violation -> violation.contains("contentHash"));
    }

    @Test
    void rejectsDigestMismatchForCanonicalBody() {
        IngestJob job = validJob()
            .withContentHash("sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb");

        assertThat(validator.validate(job)).anyMatch(violation -> violation.contains("contentHash mismatch"));
    }

    @Test
    void acceptsExplicitIdempotencyKey() {
        IngestJob job = validJob().withIdempotencyKey("operator-provided-key-001");

        assertThat(validator.validate(job)).isEmpty();
    }

    @Test
    void rejectsUnknownTopLevelKind() {
        IngestJob job = new IngestJob(
            validJob().source(),
            validJob().payload(),
            validJob().contentHash(),
            validJob().targetProfile(),
            "unexpected_kind",
            null
        );

        assertThat(validator.validate(job)).anyMatch(violation -> violation.contains("kind is unknown"));
    }

    @Test
    void targetStatesDoNotIncludeAuthorized() {
        assertThat(TargetIndexingState.values())
            .extracting(Enum::name)
            .doesNotContain("AUTHORIZED");
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
            Map.of("type", "local_pc", "provider", "codex", "project", "workspace-ragflow-advisor"),
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
