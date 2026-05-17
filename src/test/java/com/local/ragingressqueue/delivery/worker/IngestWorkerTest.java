package com.local.ragingressqueue.delivery.worker;
import com.local.ragingressqueue.delivery.domain.DeliveryDecision;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.common.TargetIndexingState;
import com.local.ragingressqueue.delivery.domain.TargetPressure;
import com.local.ragingressqueue.ingest.domain.validation.ContentHashVerifier;
import com.local.ragingressqueue.queue.port.IngestConsumer;
import com.local.ragingressqueue.queue.port.IngestMessage;
import com.local.ragingressqueue.delivery.domain.DeliveryResult;
import com.local.ragingressqueue.target.port.RagTargetAdapter;
import org.junit.jupiter.api.Test;

import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class IngestWorkerTest {
    @Test
    void openPressureFetchesAndDelivers() {
        FakeConsumer consumer = new FakeConsumer(List.of(new IngestMessage(validJob(), 1)));
        FakeAdapter adapter = new FakeAdapter(TargetPressure.OPEN, DeliveryResult.delivered("target-ref"));
        IngestWorker worker = new IngestWorker(consumer, adapter, "ragflow-transcript-memory");

        DeliveryDecision decision = worker.runOnce();

        assertThat(decision.status()).isEqualTo(DeliveryDecision.Status.DELIVERED);
        assertThat(consumer.fetchCount).isEqualTo(1);
        assertThat(adapter.deliverCount).isEqualTo(1);
        assertThat(consumer.ackCount).isEqualTo(1);
    }

    @Test
    void throttledPressureDoesNotFetchOrDeliver() {
        FakeConsumer consumer = new FakeConsumer(List.of(new IngestMessage(validJob(), 1)));
        FakeAdapter adapter = new FakeAdapter(TargetPressure.THROTTLED, DeliveryResult.delivered("target-ref"));
        IngestWorker worker = new IngestWorker(consumer, adapter, "ragflow-transcript-memory");

        DeliveryDecision decision = worker.runOnce();

        assertThat(decision.status()).isEqualTo(DeliveryDecision.Status.SKIPPED_PRESSURE);
        assertThat(consumer.fetchCount).isZero();
        assertThat(adapter.deliverCount).isZero();
    }

    @Test
    void closedPressureDoesNotFetchOrDeliver() {
        FakeConsumer consumer = new FakeConsumer(List.of(new IngestMessage(validJob(), 1)));
        FakeAdapter adapter = new FakeAdapter(TargetPressure.CLOSED, DeliveryResult.delivered("target-ref"));
        IngestWorker worker = new IngestWorker(consumer, adapter, "ragflow-transcript-memory");

        DeliveryDecision decision = worker.runOnce();

        assertThat(decision.status()).isEqualTo(DeliveryDecision.Status.SKIPPED_PRESSURE);
        assertThat(consumer.fetchCount).isZero();
        assertThat(adapter.deliverCount).isZero();
    }

    @Test
    void failedDeliveryNaksMessage() {
        FakeConsumer consumer = new FakeConsumer(List.of(new IngestMessage(validJob(), 1)));
        FakeAdapter adapter = new FakeAdapter(TargetPressure.OPEN, DeliveryResult.failed("target rejected"));
        IngestWorker worker = new IngestWorker(consumer, adapter, "ragflow-transcript-memory");

        DeliveryDecision decision = worker.runOnce();

        assertThat(decision.status()).isEqualTo(DeliveryDecision.Status.RETRY_SCHEDULED);
        assertThat(consumer.nakCount).isEqualTo(1);
    }

    @Test
    void maxDeliverExceededMapsToQuarantineCandidate() {
        FakeConsumer consumer = new FakeConsumer(List.of(new IngestMessage(validJob(), 5)));
        FakeAdapter adapter = new FakeAdapter(TargetPressure.OPEN, DeliveryResult.failed("target rejected"));
        IngestWorker worker = new IngestWorker(consumer, adapter, "ragflow-transcript-memory");

        DeliveryDecision decision = worker.runOnce();

        assertThat(decision.status()).isEqualTo(DeliveryDecision.Status.QUARANTINE_CANDIDATE);
        assertThat(consumer.ackCount).isEqualTo(1);
        assertThat(consumer.nakCount).isZero();
    }

    @Test
    void invalidQueuedPayloadIsAckedAndNotDelivered() {
        FakeConsumer consumer = new FakeConsumer(List.of(new IngestMessage(invalidPrivateJob(), 1)));
        FakeAdapter adapter = new FakeAdapter(TargetPressure.OPEN, DeliveryResult.delivered("target-ref"));
        IngestWorker worker = new IngestWorker(consumer, adapter, "ragflow-transcript-memory");

        DeliveryDecision decision = worker.runOnce();

        assertThat(decision.status()).isEqualTo(DeliveryDecision.Status.QUARANTINE_CANDIDATE);
        assertThat(adapter.deliverCount).isZero();
        assertThat(consumer.ackCount).isEqualTo(1);
        assertThat(consumer.nakCount).isZero();
    }

    @Test
    void indexedTargetStateIsNotAuthorization() {
        assertThat(TargetIndexingState.INDEXED.name()).isNotEqualTo("AUTHORIZED");
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

    private IngestJob invalidPrivateJob() {
        String body = """
            ---
            schema_version: agent_knowledge_document.v2
            result_type: conversation_chunk
            ---
            redacted body Bearer abc.def.ghi
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

    private static final class FakeConsumer implements IngestConsumer {
        private final List<IngestMessage> messages;
        private int fetchCount;
        private int ackCount;
        private int nakCount;

        private FakeConsumer(List<IngestMessage> messages) {
            this.messages = messages;
        }

        @Override
        public List<IngestMessage> fetch(int maxBatchSize) {
            fetchCount++;
            return messages;
        }

        @Override
        public void ack(IngestMessage message) {
            ackCount++;
        }

        @Override
        public void nak(IngestMessage message) {
            nakCount++;
        }
    }

    private static final class FakeAdapter implements RagTargetAdapter {
        private final TargetPressure pressure;
        private final DeliveryResult result;
        private int deliverCount;

        private FakeAdapter(TargetPressure pressure, DeliveryResult result) {
            this.pressure = pressure;
            this.result = result;
        }

        @Override
        public TargetPressure checkPressure(String targetProfile) {
            return pressure;
        }

        @Override
        public DeliveryResult deliver(IngestJob job, String targetProfile) {
            deliverCount++;
            return result;
        }

        @Override
        public com.local.ragingressqueue.target.port.TargetStatusSnapshot getStatus(IngestJob job, String targetProfile) {
            return new com.local.ragingressqueue.target.port.TargetStatusSnapshot(
                job.contentHashPrefix(),
                job.contentHash(),
                targetProfile,
                TargetIndexingState.ACCEPTED,
                "redacted"
            );
        }
    }
}
