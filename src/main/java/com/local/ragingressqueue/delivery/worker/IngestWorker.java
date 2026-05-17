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
    private final String targetProfile;
    private final IngestJobValidator validator;
    private final RedactionGuard redactionGuard;

    public IngestWorker(IngestConsumer consumer, RagTargetAdapter adapter, String targetProfile) {
        this(consumer, adapter, targetProfile, new IngestJobValidator(), new RedactionGuard());
    }

    IngestWorker(
        IngestConsumer consumer,
        RagTargetAdapter adapter,
        String targetProfile,
        IngestJobValidator validator,
        RedactionGuard redactionGuard
    ) {
        this.consumer = consumer;
        this.adapter = adapter;
        this.targetProfile = targetProfile;
        this.validator = validator;
        this.redactionGuard = redactionGuard;
    }

    public DeliveryDecision runOnce() {
        TargetPressure pressure = adapter.checkPressure(targetProfile);
        if (pressure != TargetPressure.OPEN) {
            return DeliveryDecision.skippedPressure("target pressure is " + pressure);
        }

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
            DeliveryResult result = adapter.deliver(message.job(), targetProfile);
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
