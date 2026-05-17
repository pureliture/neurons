package com.local.ragingressqueue.queue;

import com.local.ragingressqueue.core.IngestJob;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

@Component
@Profile("api")
public class NatsIngestPublisher implements IngestPublisher {
    private final JetStreamPublisherGateway gateway;
    private final SubjectRouter subjectRouter;
    private final IngestJobMessageCodec codec;

    public NatsIngestPublisher(
        JetStreamPublisherGateway gateway,
        SubjectRouter subjectRouter,
        IngestJobMessageCodec codec
    ) {
        this.gateway = gateway;
        this.subjectRouter = subjectRouter;
        this.codec = codec;
    }

    @Override
    public PublishResult publish(IngestJob job) {
        String subject = subjectRouter.subjectFor(job.kind());
        String messageId = messageIdFor(job);
        try {
            JetStreamPublishAck ack = gateway.publish(subject, codec.encode(job), messageId);
            if (ack == null || !ack.persisted()) {
                return PublishResult.failed("publish ack not received");
            }
            return PublishResult.accepted(ack.stream() + ":" + ack.sequence());
        } catch (RuntimeException error) {
            return PublishResult.failed(error.getMessage() == null ? "publish failed" : error.getMessage());
        }
    }

    private String messageIdFor(IngestJob job) {
        if (job.idempotencyKey() != null && !job.idempotencyKey().isBlank()) {
            return job.idempotencyKey();
        }
        return job.targetProfile() + ":" + job.kind() + ":" + job.contentHash();
    }
}
