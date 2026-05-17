package com.local.ragingressqueue.adapter.infra.nats;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.ingest.domain.validation.ContentHashVerifier;
import com.local.ragingressqueue.queue.port.AcknowledgementHandle;
import com.local.ragingressqueue.queue.port.IngestMessage;
import org.junit.jupiter.api.Test;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class NatsIngestConsumerTest {
    @Test
    void fetchDecodesMessagesWithDeliveryAttempt() {
        IngestJobMessageCodec codec = new IngestJobMessageCodec();
        FakeHandle handle = new FakeHandle();
        FakeGateway gateway = new FakeGateway(List.of(new RawIngestMessage(codec.encode(validJob()), 3, handle)));
        NatsIngestConsumer consumer = new NatsIngestConsumer(gateway, codec);

        List<IngestMessage> messages = consumer.fetch(10);

        assertThat(messages).hasSize(1);
        assertThat(messages.getFirst().deliveryAttempt()).isEqualTo(3);
        assertThat(messages.getFirst().job().kind()).isEqualTo("conversation_chunk");
        assertThat(gateway.requestedBatchSize).isEqualTo(10);
    }

    @Test
    void ackAndNakDelegateToRawMessageHandle() {
        IngestJobMessageCodec codec = new IngestJobMessageCodec();
        FakeHandle handle = new FakeHandle();
        FakeGateway gateway = new FakeGateway(List.of(new RawIngestMessage(codec.encode(validJob()), 1, handle)));
        NatsIngestConsumer consumer = new NatsIngestConsumer(gateway, codec);
        IngestMessage message = consumer.fetch(1).getFirst();

        consumer.ack(message);
        consumer.nak(message);

        assertThat(handle.ackCount).isEqualTo(1);
        assertThat(handle.nakCount).isEqualTo(1);
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

    private static final class FakeGateway implements JetStreamConsumerGateway {
        private final List<RawIngestMessage> messages;
        private int requestedBatchSize;

        private FakeGateway(List<RawIngestMessage> messages) {
            this.messages = new ArrayList<>(messages);
        }

        @Override
        public List<RawIngestMessage> fetch(int maxBatchSize) {
            requestedBatchSize = maxBatchSize;
            return messages;
        }
    }

    private static final class FakeHandle implements AcknowledgementHandle {
        private int ackCount;
        private int nakCount;

        @Override
        public void ack() {
            ackCount++;
        }

        @Override
        public void nak() {
            nakCount++;
        }
    }
}
