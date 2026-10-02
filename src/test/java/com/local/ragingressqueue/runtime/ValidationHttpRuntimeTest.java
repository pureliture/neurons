package com.local.ragingressqueue.runtime;

import com.local.ragingressqueue.RagIngressQueueApplication;
import com.local.ragingressqueue.ingest.domain.validation.ContentHashVerifier;
import io.nats.client.Connection;
import io.nats.client.JetStream;
import io.nats.client.JetStreamManagement;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.ApplicationContext;
import org.springframework.test.context.bean.override.convention.TestBean;

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest(classes = RagIngressQueueApplication.class,
    webEnvironment = SpringBootTest.WebEnvironment.RANDOM_PORT,
    properties = {"spring.profiles.active=api", "server.address=127.0.0.1",
        "rag-ingress.nats.provision-on-startup=false"})
class ValidationHttpRuntimeTest {
    @TestBean(name = "natsConnection", methodName = "fakeConnection", enforceOverride = true)
    Connection connection;
    @TestBean(name = "jetStreamManagement", methodName = "fakeJetStreamManagement", enforceOverride = true)
    JetStreamManagement management;
    @TestBean(name = "jetStream", methodName = "fakeJetStream", enforceOverride = true)
    JetStream jetStream;
    @Autowired ApplicationContext context;

    @Test
    void realHttpValidationAndMalformedResponsesDoNotAccessNats() throws Exception {
        String body = "---\nschema_version: agent_knowledge_document.v2\nresult_type: conversation_chunk\n---\nredacted body\n";
        String input = """
            {"schemaVersion":"rag_ingress_enqueue.v1",
            "source":{"type":"local_pc","provider":"codex","project":"fixture"},
            "payload":{"kind":"redacted_rag_ready_document","redactionVersion":"redaction.v2",
            "document":{"filename":"chunk.md","contentType":"text/markdown","body":"%s",
            "metadata":{"schema_version":"agent_knowledge_document.v2","result_type":"conversation_chunk"}}},
            "contentHash":"%s","targetProfile":"index-transcript-memory","kind":"conversation_chunk"}
            """.formatted(body.replace("\n", "\\n"), ContentHashVerifier.sha256Hex(body));
        var valid = post(input);
        assertThat(valid.statusCode()).isEqualTo(200);
        assertThat(valid.body()).isEqualTo("{\"status\":\"valid\",\"errors\":[]}");
        var malformed = post("{\"source\":\"synthetic-private-marker");
        assertThat(malformed.statusCode()).isEqualTo(400);
        assertThat(malformed.body()).doesNotContain("synthetic-private-marker", "stack", "exception", "source");
        var unsupported = post(input.replace("redacted_rag_ready_document", "redacted_document_ref"));
        assertThat(unsupported.statusCode()).isEqualTo(422);
        assertThat(unsupported.body()).contains("unsupported_payload").doesNotContain("contentHash", "jobId");
    }

    private HttpResponse<String> post(String input) throws Exception {
        int port = Integer.parseInt(context.getEnvironment().getProperty("local.server.port"));
        try (var client = HttpClient.newHttpClient()) {
            return client.send(HttpRequest.newBuilder(URI.create("http://127.0.0.1:" + port + "/v1/ingest/validate"))
                .timeout(Duration.ofSeconds(5)).header("Content-Type", "application/json")
                .POST(HttpRequest.BodyPublishers.ofString(input)).build(), HttpResponse.BodyHandlers.ofString());
        }
    }

    static Connection fakeConnection() { return ApiProfileStartupSmokeTest.fakeConnection(); }
    static JetStreamManagement fakeJetStreamManagement() { return ApiProfileStartupSmokeTest.fakeJetStreamManagement(); }
    static JetStream fakeJetStream() { return ApiProfileStartupSmokeTest.fakeJetStream(); }
}
