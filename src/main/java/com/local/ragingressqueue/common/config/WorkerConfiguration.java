package com.local.ragingressqueue.common.config;
import com.local.ragingressqueue.delivery.worker.IngestWorker;

import com.local.ragingressqueue.queue.port.IngestConsumer;
import com.local.ragingressqueue.target.port.RagTargetAdapter;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;

@Configuration
@Profile("worker")
public class WorkerConfiguration {
    @Bean
    IngestWorker ingestWorker(
        IngestConsumer consumer,
        RagTargetAdapter adapter,
        @Value("${rag-ingress.worker.target-profile:ragflow-transcript-memory}") String targetProfile
    ) {
        return new IngestWorker(consumer, adapter, targetProfile);
    }
}
