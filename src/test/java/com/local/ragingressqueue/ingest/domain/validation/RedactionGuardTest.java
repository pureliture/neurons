package com.local.ragingressqueue.ingest.domain.validation;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

// G2: the POST guard is now SECRETS-ONLY. The full public-cleanliness redaction
// (general /Users paths, dataset_id, document_id, raw_transcript, bare token/
// api_key prose) moved server-side to the Python worker, so those benign public
// terms are ACCEPTED here and rejected only as actual secrets / private-dotdir
// paths that survived the client's conservative redact_text_v2.
class RedactionGuardTest {
    private final RedactionGuard guard = new RedactionGuard();

    @Test
    void rejectsBearerToken() {
        assertThat(guard.inspect(redactedBody() + "\nBearer abc.def.ghijkl"))
            .anyMatch(violation -> violation.contains("Bearer"));
    }

    @Test
    void rejectsBasicAuthAndSecretAssignment() {
        assertThat(guard.inspect(redactedBody() + "\nBasic YWxhZGRpbjpvcGVuc2VzYW1lYWE="))
            .isNotEmpty();
        assertThat(guard.inspect(redactedBody() + "\nexport API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345"))
            .isNotEmpty();
    }

    @Test
    void rejectsProviderTranscriptAndPrivateDotdirPath() {
        assertThat(guard.inspect(redactedBody() + "\n/Users/operator/.codex/session.jsonl"))
            .anyMatch(violation -> violation.contains("/Users/"));
        assertThat(guard.inspect(redactedBody() + "\n/Users/operator/.config/private/key.pem"))
            .anyMatch(violation -> violation.contains("/Users/"));
    }

    @Test
    void acceptsBenignPublicTermsHandledByWorker() {
        // Conservatively-redacted content carries these; the worker (not this
        // guard) applies the public-ingress redaction before delivery.
        assertThat(guard.inspect(redactedBody()
            + "\nwe discussed dataset_id and document_id design and a token budget"
            + "\nthe file /Users/operator/Projects/app/main.py and ~/notes.md"
            + "\nUserPromptSubmit raw_transcript and api_key handling in prose"))
            .isEmpty();
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
    void rejectsActualSecretsOutsideBody() {
        IngestJob job = new IngestJob(
            Map.of("provider", "codex", "project", "/Users/operator/.codex/x.jsonl"),
            new DocumentPayload(
                "redacted_rag_ready_document",
                "redaction.v2",
                "dataset_id.md",
                "text/markdown",
                redactedBody(),
                Map.of("auth", "Bearer ghp_ABCdef0123456789xyzQQ", "safe", "value")
            ),
            ContentHashVerifier.sha256Hex(redactedBody()),
            "index-transcript-memory",
            "conversation_chunk",
            "codex:conversation_chunk:sha256deadbeef"
        );

        assertThat(guard.inspectJob(job))
            .anyMatch(violation -> violation.contains("source.project"))
            .anyMatch(violation -> violation.contains("payload.document.metadata.auth"));
        // benign filename "dataset_id.md" and a clean idempotencyKey are accepted.
        assertThat(guard.inspectJob(job))
            .noneMatch(violation -> violation.contains("payload.document.filename"))
            .noneMatch(violation -> violation.contains("idempotencyKey"));
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
