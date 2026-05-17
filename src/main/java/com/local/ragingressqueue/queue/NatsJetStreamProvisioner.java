package com.local.ragingressqueue.queue;

import io.nats.client.JetStreamApiException;
import io.nats.client.JetStreamManagement;
import io.nats.client.api.AckPolicy;
import io.nats.client.api.ConsumerConfiguration;
import io.nats.client.api.DeliverPolicy;
import io.nats.client.api.RetentionPolicy;
import io.nats.client.api.StorageType;
import io.nats.client.api.StreamConfiguration;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.context.annotation.Profile;
import org.springframework.core.Ordered;
import org.springframework.core.annotation.Order;
import org.springframework.stereotype.Component;

import java.io.IOException;
import java.time.Duration;

@Component
@Profile({"api", "worker"})
@Order(Ordered.HIGHEST_PRECEDENCE)
public class NatsJetStreamProvisioner implements ApplicationRunner {
    private static final String INGEST_SUBJECTS = "rag.ingress.>";

    private final JetStreamManagement management;
    private final boolean provisionOnStartup;
    private final String streamName;
    private final String consumerName;

    public NatsJetStreamProvisioner(
        JetStreamManagement management,
        @Value("${rag-ingress.nats.provision-on-startup:true}") boolean provisionOnStartup,
        @Value("${rag-ingress.nats.stream}") String streamName,
        @Value("${rag-ingress.nats.consumer}") String consumerName
    ) {
        this.management = management;
        this.provisionOnStartup = provisionOnStartup;
        this.streamName = streamName;
        this.consumerName = consumerName;
    }

    @Override
    public void run(ApplicationArguments args) {
        if (!provisionOnStartup) {
            return;
        }
        try {
            provisionStream();
            provisionConsumer();
        } catch (IOException | JetStreamApiException error) {
            throw new IllegalStateException("failed to provision NATS JetStream resources", error);
        }
    }

    private void provisionStream() throws IOException, JetStreamApiException {
        StreamConfiguration configuration = StreamConfiguration.builder()
            .name(streamName)
            .subjects(INGEST_SUBJECTS)
            .retentionPolicy(RetentionPolicy.WorkQueue)
            .storageType(StorageType.File)
            .duplicateWindow(Duration.ofMinutes(10))
            .build();
        if (streamExists()) {
            management.updateStream(configuration);
        } else {
            management.addStream(configuration);
        }
    }

    private void provisionConsumer() throws IOException, JetStreamApiException {
        ConsumerConfiguration configuration = ConsumerConfiguration.builder()
            .durable(consumerName)
            .name(consumerName)
            .filterSubject(INGEST_SUBJECTS)
            .deliverPolicy(DeliverPolicy.All)
            .ackPolicy(AckPolicy.Explicit)
            .ackWait(Duration.ofSeconds(30))
            .maxDeliver(5)
            .build();
        management.addOrUpdateConsumer(streamName, configuration);
    }

    private boolean streamExists() throws IOException, JetStreamApiException {
        try {
            management.getStreamInfo(streamName);
            return true;
        } catch (JetStreamApiException error) {
            if (error.getErrorCode() == 404 || error.getApiErrorCode() == 10059) {
                return false;
            }
            throw error;
        }
    }
}
