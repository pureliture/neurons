package com.local.ragingressqueue.adapter.infra.nats;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.ingest.domain.validation.ContentHashVerifier;
import com.local.ragingressqueue.queue.port.PublishResult;
import org.junit.jupiter.api.Test;

import java.nio.charset.StandardCharsets;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class NatsIngestPublisherTest {
    @Test
    void publishUsesRoutedSubjectAndJetStreamAck() {
        FakeGateway gateway = new FakeGateway(new JetStreamPublishAck("RAG_INGRESS_QUEUE", 42, false));
        NatsIngestPublisher publisher = new NatsIngestPublisher(gateway, new SubjectRouter(), new IngestJobMessageCodec());

        PublishResult result = publisher.publish(validJob("stable-key"));

        assertThat(result.accepted()).isTrue();
        assertThat(result.jobId()).isEqualTo("RAG_INGRESS_QUEUE:42");
        assertThat(gateway.subject).isEqualTo("rag.ingress.transcript");
        assertThat(gateway.messageId).isEqualTo("stable-key");
        assertThat(new String(gateway.payload, StandardCharsets.UTF_8)).contains("conversation_chunk");
    }

    @Test
    void publishFallsBackToContentHashWhenIdempotencyKeyIsAbsent() {
        FakeGateway gateway = new FakeGateway(new JetStreamPublishAck("RAG_INGRESS_QUEUE", 7, false));
        NatsIngestPublisher publisher = new NatsIngestPublisher(gateway, new SubjectRouter(), new IngestJobMessageCodec());
        IngestJob job = validJob(null);

        publisher.publish(job);

        assertThat(gateway.messageId).isEqualTo("index-transcript-memory:conversation_chunk:" + job.contentHash());
    }

    @Test
    void publishWithoutAckIsRejected() {
        FakeGateway gateway = new FakeGateway(new JetStreamPublishAck("RAG_INGRESS_QUEUE", 0, false));
        NatsIngestPublisher publisher = new NatsIngestPublisher(gateway, new SubjectRouter(), new IngestJobMessageCodec());

        PublishResult result = publisher.publish(validJob("stable-key"));

        assertThat(result.accepted()).isFalse();
        assertThat(result.error()).contains("publish ack");
    }

    @Test
    void publishGatewayFailureIsRejected() {
        FakeGateway gateway = new FakeGateway(new IllegalStateException("nats unavailable"));
        NatsIngestPublisher publisher = new NatsIngestPublisher(gateway, new SubjectRouter(), new IngestJobMessageCodec());

        PublishResult result = publisher.publish(validJob("stable-key"));

        assertThat(result.accepted()).isFalse();
        assertThat(result.error()).contains("nats unavailable");
    }

    private IngestJob validJob(String idempotencyKey) {
        String body = """
            ---
            schema_version: agent_knowledge_document.v2
            result_type: conversation_chunk
            ---
            redacted body
            """;
        return new IngestJob(
            Map.of("provider", "codex", "project", "workspace-index-advisor"),
            new DocumentPayload(
                "redacted_rag_ready_document",
                "redaction.v2",
                "chunk.md",
                "text/markdown",
                body,
                Map.of("schema_version", "agent_knowledge_document.v2", "result_type", "conversation_chunk")
            ),
            ContentHashVerifier.sha256Hex(body),
            "index-transcript-memory",
            "conversation_chunk",
            idempotencyKey
        );
    }

    private static final class FakeGateway implements JetStreamPublisherGateway {
        private final JetStreamPublishAck ack;
        private final RuntimeException failure;
        private String subject;
        private byte[] payload;
        private String messageId;

        private FakeGateway(JetStreamPublishAck ack) {
            this.ack = ack;
            this.failure = null;
        }

        private FakeGateway(RuntimeException failure) {
            this.ack = null;
            this.failure = failure;
        }

        @Override
        public JetStreamPublishAck publish(String subject, byte[] payload, String messageId) {
            this.subject = subject;
            this.payload = payload;
            this.messageId = messageId;
            if (failure != null) {
                throw failure;
            }
            return ack;
        }
    }
}
