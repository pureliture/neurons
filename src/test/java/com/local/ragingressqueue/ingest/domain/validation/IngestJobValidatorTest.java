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
    void acceptsSessionRecapForSessionMemoryTarget() {
        IngestJob job = validJob(
            "session_recap",
            "ragflow-session-memory",
            "session-recap.md",
            "session_recap"
        );

        assertThat(validator.validate(job)).isEmpty();
    }

    @Test
    void acceptsToolEvidenceSummaryForTranscriptMemoryTarget() {
        IngestJob job = validJob(
            "tool_evidence_summary",
            "ragflow-transcript-memory",
            "tool-evidence-summary.md",
            "tool_evidence_summary"
        );

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
        return validJob("conversation_chunk", "ragflow-transcript-memory", "chunk.md", "conversation_chunk");
    }

    private IngestJob validJob(String resultType, String targetProfile, String filename, String kind) {
        String body = """
            ---
            schema_version: agent_knowledge_document.v2
            result_type: %s
            ---
            redacted body
            """.formatted(resultType);
        return new IngestJob(
            Map.of("type", "local_pc", "provider", "codex", "project", "workspace-ragflow-advisor"),
            new DocumentPayload(
                "redacted_rag_ready_document",
                "redaction.v2",
                filename,
                "text/markdown",
                body,
                Map.of("schema_version", "agent_knowledge_document.v2", "result_type", resultType)
            ),
            ContentHashVerifier.sha256Hex(body),
            targetProfile,
            kind,
            null
        );
    }
}
