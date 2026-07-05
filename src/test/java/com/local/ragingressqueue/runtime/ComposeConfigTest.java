package com.local.ragingressqueue.runtime;

import org.junit.jupiter.api.Test;
import org.yaml.snakeyaml.Yaml;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

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
        assertThat(compose).contains("RETIRED_INDEX_BRIDGE_PRESSURE_RUNNING_THROTTLE_THRESHOLD");
        assertThat(compose).contains("RETIRED_INDEX_BRIDGE_PRESSURE_RUNNING_CLOSED_THRESHOLD");
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
        assertThat(compose).contains("NEO4J_AUTH: ${LLM_BRAIN_NEO4J_USER:-neo4j}/${LLM_BRAIN_NEO4J_PASSWORD:?set LLM_BRAIN_NEO4J_PASSWORD}");
        assertThat(compose).contains("LLM_BRAIN_NEO4J_URI: ${LLM_BRAIN_NEO4J_URI:-bolt://llm-brain-neo4j:7687}");
        assertThat(compose).doesNotContain("\n      NEO4J_USER:");
        assertThat(compose).doesNotContain("gemini-3.5-flash-thinking");
        // Secrets must be required (${VAR:?...}) — no checked-in shared password/secret
        // fallback may let the stack boot with public credentials and bypass the
        // .env.example placeholder contract.
        assertThat(compose).doesNotContain(":-llmbrain");
        assertThat(compose).contains("LLM_BRAIN_COUCHDB_PASSWORD:?set LLM_BRAIN_COUCHDB_PASSWORD");
        assertThat(compose).contains("LLM_BRAIN_COUCHDB_SECRET:?set LLM_BRAIN_COUCHDB_SECRET");
        assertThat(compose).contains("LLM_BRAIN_LEDGER_POSTGRES_PASSWORD:?set LLM_BRAIN_LEDGER_POSTGRES_PASSWORD");
        assertThat(compose).contains("LLM_BRAIN_NEO4J_PASSWORD:?set LLM_BRAIN_NEO4J_PASSWORD");
        // Host networking is allowed for EXACTLY ONE service: neuron-knowledge-mcp
        // (documented — reach loopback Neo4j/vertex-wrapper/PG while binding the
        // tailnet interface). The bulk-semantic / graph-trigger lanes and every other
        // service must stay on the bridge network. Guard the directive count so a new
        // host-networked service cannot slip in unnoticed (comments are ignored).
        long hostNetworkDirectives = compose.lines()
            .filter(line -> line.strip().equals("network_mode: host"))
            .count();
        assertThat(hostNetworkDirectives).isEqualTo(1);
        assertThat(compose).doesNotContain("index-server");
        assertThat(compose).doesNotContain("index-redis");
        assertThat(compose).doesNotContain("index-mysql");
    }

    @Test
    void mcpHttpServicePreservesEnvFileAllowedHostsFallback() throws IOException {
        String mcpService = serviceBlock(
            Files.readString(Path.of("compose.yaml")),
            "neuron-knowledge-mcp"
        );

        assertThat(mcpService).contains("MCP_HTTP_ALLOWED_HOSTS_FROM_COMPOSE: ${MCP_HTTP_ALLOWED_HOSTS:-}");
        assertThat(mcpService).contains("export MCP_HTTP_ALLOWED_HOSTS=");
        assertThat(mcpService).doesNotContain("MCP_HTTP_ALLOWED_HOSTS: ${MCP_HTTP_ALLOWED_HOSTS:-}");
    }

    @Test
    void retiredIndexBridgeEnvAnchorIsSharedByJavaAndPythonWorkers() throws IOException {
        String compose = Files.readString(Path.of("compose.yaml"));
        String pythonWorker = serviceBlock(compose, "ingress-worker-py");

        assertThat(compose).contains("x-retired-index-bridge-env: &retired-index-bridge-env");
        assertThat(compose).contains("x-ingress-java-env: &ingress-java-env\n  <<: *retired-index-bridge-env");
        assertThat(pythonWorker).contains("<<: *retired-index-bridge-env");
        assertThat(serviceBlock(compose, "ingress-api")).contains("<<: *ingress-java-env");
        assertThat(serviceBlock(compose, "ingress-worker")).contains("<<: *ingress-java-env");

        for (String key : retiredIndexBridgeCommonEnvKeys()) {
            assertThat(directYamlKeyDeclarationCount(compose, key)).as(key).isEqualTo(1);
            assertThat(pythonWorker).doesNotContain("\n      " + key + ":");
        }
    }

    @Test
    void retiredIndexBridgeEnvAnchorResolvesThroughYamlMergeForJavaAndPythonServices() throws IOException {
        Map<String, Object> compose = composeYaml();
        Map<String, Object> sharedEnv = map(compose.get("x-retired-index-bridge-env"));

        assertThat(sharedEnv.keySet()).containsExactlyElementsOf(retiredIndexBridgeCommonEnvKeys());
        assertThat(sharedEnv)
            .doesNotContainKeys(
                "RAG_INGRESS_NATS_URL",
                "ALLOW_LIVE_QUEUE",
                "SHADOW_DELIVER",
                "RAG_INGRESS_ALLOW_LIVE_QUEUE",
                "RAG_INGRESS_DELIVER"
            );

        for (String serviceName : List.of("ingress-api", "ingress-worker", "ingress-worker-py")) {
            Map<String, Object> env = serviceEnvironment(compose, serviceName);
            for (String key : retiredIndexBridgeCommonEnvKeys()) {
                assertThat(env).containsEntry(key, sharedEnv.get(key));
            }
        }

        Map<String, Object> pythonEnv = serviceEnvironment(compose, "ingress-worker-py");
        assertThat(pythonEnv)
            .containsKeys("ALLOW_LIVE_QUEUE", "SHADOW_DELIVER", "SHADOW_STREAM", "SHADOW_DURABLE");
        assertThat(serviceEnvironment(compose, "ingress-api"))
            .doesNotContainKeys("ALLOW_LIVE_QUEUE", "SHADOW_DELIVER");
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

    @Test
    void envExampleCoversAllRequiredComposeVars() throws IOException {
        // Every hard-required compose var (${VAR:?...}) must be documented in
        // .env.example so an operator copying the sample cannot miss a fail-closed var.
        String compose = Files.readString(Path.of("compose.yaml"));
        String envExample = Files.readString(Path.of(".env.example"));

        java.util.Set<String> required = new java.util.TreeSet<>();
        var requiredMatcher =
            java.util.regex.Pattern.compile("\\$\\{([A-Z0-9_]+):\\?").matcher(compose);
        while (requiredMatcher.find()) {
            required.add(requiredMatcher.group(1));
        }

        java.util.Set<String> documented = new java.util.TreeSet<>();
        var documentedMatcher =
            java.util.regex.Pattern.compile("(?m)^\\s*([A-Z0-9_]+)\\s*=").matcher(envExample);
        while (documentedMatcher.find()) {
            documented.add(documentedMatcher.group(1));
        }

        java.util.Set<String> missing = new java.util.TreeSet<>(required);
        missing.removeAll(documented);
        assertThat(missing)
            .as("required ${VAR:?} compose vars missing from .env.example")
            .isEmpty();
    }

    private static String serviceBlock(String compose, String serviceName) {
        String header = "  " + serviceName + ":\n";
        int start = compose.indexOf(header);
        assertThat(start).as("service exists: " + serviceName).isGreaterThanOrEqualTo(0);

        var nextService = java.util.regex.Pattern.compile("(?m)^  [A-Za-z0-9_-]+:\\s*$")
            .matcher(compose);
        int end = compose.length();
        while (nextService.find()) {
            if (nextService.start() > start) {
                end = nextService.start();
                break;
            }
        }
        return compose.substring(start, end);
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> composeYaml() throws IOException {
        Object loaded = new Yaml().load(Files.readString(Path.of("compose.yaml")));
        return (Map<String, Object>) loaded;
    }

    private static Map<String, Object> serviceEnvironment(Map<String, Object> compose, String serviceName) {
        Map<String, Object> services = map(compose.get("services"));
        Map<String, Object> service = map(services.get(serviceName));
        return map(service.get("environment"));
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> map(Object value) {
        assertThat(value).isInstanceOf(Map.class);
        return (Map<String, Object>) value;
    }

    private static List<String> retiredIndexBridgeCommonEnvKeys() {
        return List.of(
            "RETIRED_INDEX_BRIDGE_BASE_URL",
            "RETIRED_INDEX_BRIDGE_API_KEY",
            "RETIRED_INDEX_BRIDGE_TRANSCRIPT_MEMORY_DATASET_ID",
            "RETIRED_INDEX_BRIDGE_SESSION_MEMORY_DATASET_ID",
            "RETIRED_INDEX_BRIDGE_SESSION_SUMMARY_DATASET_ID",
            "RETIRED_INDEX_BRIDGE_PROJECT_MEMORY_DATASET_ID",
            "RETIRED_INDEX_BRIDGE_PROCEDURAL_MEMORY_DATASET_ID",
            "RETIRED_INDEX_BRIDGE_TASK_SUMMARY_DATASET_ID",
            "RETIRED_INDEX_BRIDGE_APPROVED_MEMORY_CARD_DATASET_ID"
        );
    }

    private static long directYamlKeyDeclarationCount(String yaml, String key) {
        return yaml.lines()
            .filter(line -> line.matches("\\s+" + key + ":.*"))
            .count();
    }
}
