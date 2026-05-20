package com.local.ragingressqueue.delivery.worker;

import com.local.ragingressqueue.delivery.domain.DeliveryDecision;
import com.local.ragingressqueue.delivery.domain.TargetPressure;
import com.local.ragingressqueue.ingest.domain.validation.IngestJobValidator;
import com.local.ragingressqueue.ingest.domain.validation.RedactionGuard;
import com.local.ragingressqueue.queue.port.IngestConsumer;
import com.local.ragingressqueue.queue.port.IngestMessage;
import com.local.ragingressqueue.delivery.domain.DeliveryResult;
import com.local.ragingressqueue.target.port.RagTargetAdapter;

import java.util.List;

public class IngestWorker {
    private static final int MAX_BATCH_SIZE = 10;
    private static final int MAX_DELIVER = 5;

    private final IngestConsumer consumer;
    private final RagTargetAdapter adapter;
    private final IngestJobValidator validator;
    private final RedactionGuard redactionGuard;

    public IngestWorker(IngestConsumer consumer, RagTargetAdapter adapter) {
        this(consumer, adapter, new IngestJobValidator(), new RedactionGuard());
    }

    IngestWorker(
        IngestConsumer consumer,
        RagTargetAdapter adapter,
        IngestJobValidator validator,
        RedactionGuard redactionGuard
    ) {
        this.consumer = consumer;
        this.adapter = adapter;
        this.validator = validator;
        this.redactionGuard = redactionGuard;
    }

    public DeliveryDecision runOnce() {
        List<IngestMessage> messages = consumer.fetch(MAX_BATCH_SIZE);
        if (messages.isEmpty()) {
            return DeliveryDecision.noWork();
        }

        DeliveryDecision lastDecision = DeliveryDecision.noWork();
        for (IngestMessage message : messages) {
            if (!validator.validate(message.job()).isEmpty() || !redactionGuard.inspectJob(message.job()).isEmpty()) {
                consumer.ack(message);
                lastDecision = DeliveryDecision.quarantineCandidate("queued payload failed validation");
                continue;
            }
            String messageTargetProfile = message.job().targetProfile();
            TargetPressure pressure = adapter.pressureSnapshot(messageTargetProfile).pressure();
            if (pressure != TargetPressure.OPEN) {
                consumer.nak(message);
                lastDecision = DeliveryDecision.skippedPressure("target pressure is " + pressure);
                continue;
            }
            DeliveryResult result = adapter.deliver(message.job(), messageTargetProfile);
            if (result.delivered()) {
                consumer.ack(message);
                lastDecision = DeliveryDecision.delivered();
            } else if (message.deliveryAttempt() >= MAX_DELIVER) {
                consumer.ack(message);
                lastDecision = DeliveryDecision.quarantineCandidate("max deliver exceeded");
            } else {
                consumer.nak(message);
                lastDecision = DeliveryDecision.retryScheduled("target delivery failed");
            }
        }
        return lastDecision;
    }
}
