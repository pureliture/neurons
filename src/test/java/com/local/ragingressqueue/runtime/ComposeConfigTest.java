package com.local.ragingressqueue.runtime;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;

class ComposeConfigTest {
    @Test
    void composeDefinesOnlyIngressQueueServices() throws IOException {
        String compose = Files.readString(Path.of("compose.yaml"));

        assertThat(compose).contains("nats-jetstream:");
        assertThat(compose).contains("restart: unless-stopped");
        assertThat(compose).contains("org.opencontainers.image.revision");
        assertThat(compose).contains("ingress-api:");
        assertThat(compose).contains("ingress-worker:");
        assertThat(compose).contains("RAG_INGRESS_NATS_URL: nats://nats-jetstream:4222");
        assertThat(compose).contains("RAGFLOW_PRESSURE_RUNNING_THROTTLE_THRESHOLD");
        assertThat(compose).contains("RAGFLOW_PRESSURE_RUNNING_CLOSED_THRESHOLD");
        assertThat(compose).contains("host.docker.internal:host-gateway");
        assertThat(compose).contains("127.0.0.1:4222:4222");
        assertThat(compose).contains("127.0.0.1:8080:8080");
        assertThat(compose).contains("SPRING_MAIN_WEB_APPLICATION_TYPE: none");
        assertThat(compose).doesNotContain("ragflow-server");
        assertThat(compose).doesNotContain("ragflow-redis");
        assertThat(compose).doesNotContain("ragflow-mysql");
    }

    @Test
    void dockerfileUsesCorretto25Runtime() throws IOException {
        String dockerfile = Files.readString(Path.of("Dockerfile"));

        assertThat(dockerfile).contains("amazoncorretto:25");
        assertThat(dockerfile).contains("gradle");
        assertThat(dockerfile).contains("COPY scripts ./scripts");
    }
}
