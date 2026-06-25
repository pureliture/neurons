package com.local.ragingressqueue.runtime;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

import static org.assertj.core.api.Assertions.assertThat;

class ComposeConfigTest {
    @Test
    void composeDefinesIngressAndProfileGatedLlmBrainServices() throws IOException {
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
        assertThat(compose).contains("127.0.0.1:18080:8080");
        assertThat(compose).contains("SPRING_MAIN_WEB_APPLICATION_TYPE: none");
        assertThat(compose).contains("llm-brain-neo4j:");
        assertThat(compose).contains("llm-brain-couchdb:");
        assertThat(compose).contains("llm-brain-ledger-postgres:");
        assertThat(compose).contains("llm-brain-vertex-wrapper:");
        assertThat(compose).contains("llm-brain-tools:");
        assertThat(compose).contains("llm-brain-graph-trigger:");
        assertThat(compose).contains("profiles: [\"llm-brain-graph\", \"llm-brain-core\"]");
        assertThat(compose).contains("profiles: [\"llm-brain-core\"]");
        assertThat(compose).contains("COUCHDB_URL: http://llm-brain-couchdb:5984");
        assertThat(compose).contains("NEURON_LEDGER_PG_DSN: postgresql://");
        assertThat(compose).contains("@llm-brain-ledger-postgres:5432/");
        assertThat(compose).contains("LLM_BRAIN_LLM_BASE_URL: http://llm-brain-vertex-wrapper:");
        assertThat(compose).contains("LLM_BRAIN_LLM_MODEL: ${LLM_BRAIN_LLM_MODEL:-gemma-4-26b-a4b-it-maas}");
        assertThat(compose).contains("LLM_BRAIN_SMALL_LLM_MODEL: ${LLM_BRAIN_SMALL_LLM_MODEL:-gemma-4-26b-a4b-it-maas}");
        assertThat(compose).contains("LLM_BRAIN_LLM_REASONING_EFFORT: ${LLM_BRAIN_LLM_REASONING_EFFORT:-none}");
        assertThat(compose).contains("LLM_BRAIN_GRAPH_WRITE_TIMEOUT_SECONDS: ${LLM_BRAIN_GRAPH_WRITE_TIMEOUT_SECONDS:-1200}");
        assertThat(compose).contains("neuron-knowledge couchdb-graph-trigger");
        assertThat(compose).contains("LLM_BRAIN_GRAPH_TRIGGER_INTERVAL_SECONDS");
        assertThat(compose).contains("LLM_BRAIN_GRAPH_TRIGGER_LIMIT");
        // Hot path is episode-only by default: entity extraction must default off.
        assertThat(compose).contains("LLM_BRAIN_GRAPH_EXTRACT_ENTITIES: ${LLM_BRAIN_GRAPH_EXTRACT_ENTITIES:-false}");
        assertThat(compose).doesNotContain("LLM_BRAIN_GRAPH_EXTRACT_ENTITIES: ${LLM_BRAIN_GRAPH_EXTRACT_ENTITIES:-true}");
        // Bulk semantic lane: off-by-default service on its own opt-in profile.
        assertThat(compose).contains("llm-brain-bulk-semantic-trigger:");
        assertThat(compose).contains("profiles: [\"llm-brain-bulk-semantic\"]");
        assertThat(compose).contains("neuron-knowledge couchdb-bulk-semantic-trigger");
        assertThat(compose).contains("LLM_BRAIN_BULK_SEMANTIC_TRIGGER_INTERVAL_SECONDS");
        assertThat(compose).contains("LLM_BRAIN_BULK_SEMANTIC_EMBEDDINGS");
        assertThat(compose).contains("GOOGLE_APPLICATION_CREDENTIALS");
        assertThat(compose).contains("NEO4J_AUTH: ${LLM_BRAIN_NEO4J_USER:-neo4j}/${LLM_BRAIN_NEO4J_PASSWORD:-llmbrain}");
        assertThat(compose).contains("LLM_BRAIN_NEO4J_URI: ${LLM_BRAIN_NEO4J_URI:-bolt://llm-brain-neo4j:7687}");
        assertThat(compose).doesNotContain("\n      NEO4J_USER:");
        assertThat(compose).doesNotContain("gemini-3.5-flash-thinking");
        // Host networking is allowed for EXACTLY ONE service: neuron-knowledge-mcp
        // (documented — reach loopback Neo4j/vertex-wrapper/PG while binding the
        // tailnet interface). The bulk-semantic / graph-trigger lanes and every other
        // service must stay on the bridge network. Guard the directive count so a new
        // host-networked service cannot slip in unnoticed (comments are ignored).
        long hostNetworkDirectives = compose.lines()
            .filter(line -> line.strip().equals("network_mode: host"))
            .count();
        assertThat(hostNetworkDirectives).isEqualTo(1);
        assertThat(compose).doesNotContain("ragflow-server");
        assertThat(compose).doesNotContain("ragflow-redis");
        assertThat(compose).doesNotContain("ragflow-mysql");
    }

    @Test
    void envExampleKeepsLlmBrainSecretsAsPlaceholders() throws IOException {
        String envExample = Files.readString(Path.of(".env.example"));

        assertThat(envExample).contains("COMPOSE_PROFILES=llm-brain-core");
        assertThat(envExample).contains("LLM_BRAIN_COUCHDB_PASSWORD=replace-with-local-couchdb-password");
        assertThat(envExample).contains("LLM_BRAIN_LEDGER_POSTGRES_PASSWORD=replace-with-local-postgres-password");
        assertThat(envExample).contains("LLM_BRAIN_NEO4J_PASSWORD=replace-with-local-neo4j-password");
        assertThat(envExample).contains("LLM_BRAIN_VERTEX_ADC_PATH=./secrets/vertex-adc.json");
        assertThat(envExample).contains("LLM_BRAIN_LLM_MODEL=gemma-4-26b-a4b-it-maas");
        assertThat(envExample).contains("LLM_BRAIN_SMALL_LLM_MODEL=gemma-4-26b-a4b-it-maas");
        assertThat(envExample).contains("LLM_BRAIN_GRAPH_TRIGGER_INTERVAL_SECONDS=300");
        assertThat(envExample).contains("LLM_BRAIN_GRAPH_EXTRACT_ENTITIES=false");
        assertThat(envExample).contains("LLM_BRAIN_BULK_SEMANTIC_TRIGGER_INTERVAL_SECONDS=900");
        assertThat(envExample).contains("LLM_BRAIN_BULK_SEMANTIC_EMBEDDINGS=true");
        assertThat(envExample).doesNotContain("gemini-3.5-flash-thinking");
    }

    @Test
    void dockerfileUsesCorretto25Runtime() throws IOException {
        String dockerfile = Files.readString(Path.of("Dockerfile"));

        assertThat(dockerfile).contains("amazoncorretto:25");
        assertThat(dockerfile).contains("gradle");
        assertThat(dockerfile).contains("COPY scripts ./scripts");
    }
}
