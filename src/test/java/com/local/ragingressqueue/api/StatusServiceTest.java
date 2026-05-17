package com.local.ragingressqueue.api;

import com.local.ragingressqueue.target.RagFlowDeliveryException;
import com.local.ragingressqueue.target.RagFlowDocumentRef;
import com.local.ragingressqueue.target.RagFlowGateway;
import com.local.ragingressqueue.target.RagFlowPressurePolicy;
import com.local.ragingressqueue.target.RagFlowPressureSnapshot;
import com.local.ragingressqueue.core.DocumentPayload;
import com.local.ragingressqueue.queue.QueueStatusSnapshot;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class StatusServiceTest {
    @Test
    void defaultStatusFailsClosed() {
        Map<String, Object> status = new StatusService().currentStatus();

        assertThat(status).containsEntry("externalStatus", "not_configured");
        assertThat(status.get("target")).isEqualTo(Map.of(
            "name", "ragflow",
            "pressure", "CLOSED",
            "running", 0,
            "unstart", 0,
            "sampled", 0,
            "reason", "not_configured"
        ));
    }

    @Test
    void configuredLiveStatusReportsOpenWithoutExposingSecrets() {
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        Map<String, Object> status = new StatusService(
            true,
            "http://host.docker.internal:9380",
            "secret-token",
            "ds_1",
            gateway,
            new RagFlowPressurePolicy(20, 5, 100, 25),
            () -> new QueueStatusSnapshot(7, 1, 2, 0)
        ).currentStatus();

        assertThat(status).containsEntry("externalStatus", "configured");
        assertThat(status.get("target")).isEqualTo(Map.of(
            "name", "ragflow",
            "pressure", "OPEN",
            "running", 0,
            "unstart", 0,
            "sampled", 100
        ));
        assertThat(status.get("queue")).isEqualTo(Map.of("pending", 7L, "inFlight", 1L, "redelivered", 2L, "deadLetter", 0L));
        assertThat(status.toString())
            .doesNotContain("secret-token")
            .doesNotContain("ds_1")
            .doesNotContain("host.docker.internal");
    }

    @Test
    void configuredLiveStatusReportsThrottledWhenRagFlowBacklogIsHigh() {
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        gateway.pressureSnapshot = new RagFlowPressureSnapshot(20, 2, 0, 78, 100);

        Map<String, Object> status = new StatusService(
            true,
            "http://host.docker.internal:9380",
            "secret-token",
            "ds_1",
            gateway,
            new RagFlowPressurePolicy(20, 5, 100, 25),
            null
        ).currentStatus();

        assertThat(status.get("target")).isEqualTo(Map.of(
            "name", "ragflow",
            "pressure", "THROTTLED",
            "running", 20,
            "unstart", 2,
            "sampled", 100
        ));
    }

    @Test
    void configuredLiveStatusFailsClosedWhenPressureReadFails() {
        FakeRagFlowGateway gateway = new FakeRagFlowGateway();
        gateway.failPressure = true;

        Map<String, Object> status = new StatusService(
            true,
            "http://host.docker.internal:9380",
            "secret-token",
            "ds_1",
            gateway,
            new RagFlowPressurePolicy(20, 5, 100, 25),
            null
        ).currentStatus();

        assertThat(status.get("target")).isEqualTo(Map.of(
            "name", "ragflow",
            "pressure", "CLOSED",
            "running", 0,
            "unstart", 0,
            "sampled", 0,
            "reason", "pressure_read_failed"
        ));
    }

    private static final class FakeRagFlowGateway implements RagFlowGateway {
        private boolean failPressure;
        private RagFlowPressureSnapshot pressureSnapshot = new RagFlowPressureSnapshot(0, 0, 0, 100, 100);

        @Override
        public RagFlowDocumentRef uploadDocument(String baseUrl, String apiKey, String datasetId, DocumentPayload payload) {
            throw new UnsupportedOperationException("not used");
        }

        @Override
        public void updateMetadata(String baseUrl, String apiKey, String datasetId, String documentId, Map<String, String> metadata) {
            throw new UnsupportedOperationException("not used");
        }

        @Override
        public void requestParse(String baseUrl, String apiKey, String datasetId, String documentId) {
            throw new UnsupportedOperationException("not used");
        }

        @Override
        public RagFlowPressureSnapshot pressureSnapshot(String baseUrl, String apiKey, String datasetId) {
            if (failPressure) {
                throw new RagFlowDeliveryException("boom");
            }
            return pressureSnapshot;
        }
    }
}
