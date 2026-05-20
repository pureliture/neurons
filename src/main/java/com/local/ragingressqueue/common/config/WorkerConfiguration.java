package com.local.ragingressqueue.common.config;
import com.local.ragingressqueue.delivery.worker.IngestWorker;

import com.local.ragingressqueue.queue.port.IngestConsumer;
import com.local.ragingressqueue.target.port.RagTargetAdapter;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;

@Configuration
@Profile("worker")
public class WorkerConfiguration {
    @Bean
    IngestWorker ingestWorker(
        IngestConsumer consumer,
        RagTargetAdapter adapter
    ) {
        return new IngestWorker(consumer, adapter);
    }
}
