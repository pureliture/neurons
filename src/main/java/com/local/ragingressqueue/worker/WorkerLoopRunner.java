package com.local.ragingressqueue.worker;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.ApplicationArguments;
import org.springframework.boot.ApplicationRunner;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

@Component
@Profile("worker")
public class WorkerLoopRunner implements ApplicationRunner {
    private static final Logger LOGGER = LoggerFactory.getLogger(WorkerLoopRunner.class);

    private final IngestWorker worker;
    private final long pollIntervalMillis;

    public WorkerLoopRunner(
        IngestWorker worker,
        @Value("${rag-ingress.worker.poll-interval-ms:1000}") long pollIntervalMillis
    ) {
        this.worker = worker;
        this.pollIntervalMillis = pollIntervalMillis;
    }

    @Override
    public void run(ApplicationArguments args) throws InterruptedException {
        while (!Thread.currentThread().isInterrupted()) {
            DeliveryDecision decision = worker.runOnce();
            LOGGER.debug("worker decision status={}", decision.status());
            Thread.sleep(pollIntervalMillis);
        }
    }
}
