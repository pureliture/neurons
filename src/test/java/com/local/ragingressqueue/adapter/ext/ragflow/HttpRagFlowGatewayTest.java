package com.local.ragingressqueue.adapter.ext.ragflow;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

class HttpRagFlowGatewayTest {
    @Test
    void findByContentHashPaginatesUntilTheMatchingNameIsFoundOnALaterPage() throws Exception {
        // The keyword query can return more hits than one page holds; the real name match may land on a
        // later page. findByContentHash must page through the reported total instead of inspecting only
        // the first page, otherwise it falsely reports absent and a duplicate gets re-uploaded.
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        AtomicInteger requests = new AtomicInteger();
        server.createContext("/api/v1/datasets/ds_1/documents", exchange -> {
            int page = pageOf(exchange.getRequestURI().getQuery());
            requests.incrementAndGet();
            // total = 31 > page_size (30): the matching document only appears on page 2.
            String docs = page == 1 ? noiseDocs(30) : "{\"name\":\"chunk-48daba68a6f6.md\"}";
            writeJson(exchange, """
                {"code": 0, "data": {"total": 31, "docs": [%s]}}
                """.formatted(docs));
        });
        server.start();
        try {
            HttpRagFlowGateway gateway = new HttpRagFlowGateway();

            boolean found = gateway.findByContentHash(
                "http://127.0.0.1:" + server.getAddress().getPort(),
                "token",
                "ds_1",
                "48daba68a6f6"
            );

            assertThat(found).isTrue();
            assertThat(requests).hasValue(2);
        } finally {
            server.stop(0);
        }
    }

    @Test
    void findByContentHashStopsAfterOnePageWhenTotalFitsInASinglePage() throws Exception {
        // The common case (a fragment with zero or one hit) must remain a single GET so the dedup
        // lookup keeps its perf win and does not issue extra RAGFlow round-trips.
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        AtomicInteger requests = new AtomicInteger();
        server.createContext("/api/v1/datasets/ds_1/documents", exchange -> {
            requests.incrementAndGet();
            writeJson(exchange, """
                {"code": 0, "data": {"total": 0, "docs": []}}
                """);
        });
        server.start();
        try {
            HttpRagFlowGateway gateway = new HttpRagFlowGateway();

            boolean found = gateway.findByContentHash(
                "http://127.0.0.1:" + server.getAddress().getPort(),
                "token",
                "ds_1",
                "48daba68a6f6"
            );

            assertThat(found).isFalse();
            assertThat(requests).hasValue(1);
        } finally {
            server.stop(0);
        }
    }

    private static int pageOf(String query) {
        for (String part : query.split("&")) {
            if (part.startsWith("page=")) {
                return Integer.parseInt(part.substring("page=".length()));
            }
        }
        return 1;
    }

    private static String noiseDocs(int count) {
        StringBuilder builder = new StringBuilder();
        for (int i = 0; i < count; i++) {
            if (i > 0) {
                builder.append(',');
            }
            builder.append("{\"name\":\"noise-").append(i).append(".md\"}");
        }
        return builder.toString();
    }

    private static void writeJson(com.sun.net.httpserver.HttpExchange exchange, String response) throws java.io.IOException {
        byte[] body = response.getBytes(StandardCharsets.UTF_8);
        exchange.sendResponseHeaders(200, body.length);
        exchange.getResponseBody().write(body);
        exchange.close();
    }

    @Test
    void pressureSnapshotCountsRagFlowDocumentRunStates() throws Exception {
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
        AtomicInteger requests = new AtomicInteger();
        server.createContext("/api/v1/datasets/ds_1/documents", exchange -> {
            int requestNumber = requests.incrementAndGet();
            String docs = switch (requestNumber) {
                case 1 -> """
                      {"run": "RUNNING"},
                      {"run": "RUNNING"},
                      {"run": "UNSTART"},
                      {"run": "FAIL"},
                      {"run": "DONE"}
                    """;
                case 2 -> """
                      {"run": "RUNNING"}
                    """;
                default -> """
                      {"run": "UNSTART"}
                    """;
            };
            String response = """
                {
                  "code": 0,
                  "data": {
                    "total": 5,
                    "docs": [
                %s
                    ]
                  }
                }
                """.formatted(docs);
            byte[] body = response.getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.start();
        try {
            HttpRagFlowGateway gateway = new HttpRagFlowGateway();

            RagFlowPressureSnapshot snapshot = gateway.pressureSnapshot(
                "http://127.0.0.1:" + server.getAddress().getPort(),
                "token",
                "ds_1"
            );

            assertThat(snapshot).isEqualTo(new RagFlowPressureSnapshot(5, 5, 1, 1, 5, 5));
            assertThat(requests).hasValue(3);
        } finally {
            server.stop(0);
        }
    }
}
