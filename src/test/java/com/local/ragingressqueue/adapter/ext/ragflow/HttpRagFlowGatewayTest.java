package com.local.ragingressqueue.adapter.ext.ragflow;

import com.sun.net.httpserver.HttpServer;
import org.junit.jupiter.api.Test;

import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicInteger;

import static org.assertj.core.api.Assertions.assertThat;

class HttpRagFlowGatewayTest {
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
