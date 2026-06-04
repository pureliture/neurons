package com.local.ragingressqueue.status.service;

import com.local.ragingressqueue.queue.port.QueueStatusSnapshot;
import com.local.ragingressqueue.delivery.domain.DeliveryResult;
import com.local.ragingressqueue.delivery.domain.TargetPressure;
import com.local.ragingressqueue.ingest.domain.TargetProfile;
import com.local.ragingressqueue.ingest.domain.TargetProfileRegistry;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.target.port.BackendKind;
import com.local.ragingressqueue.target.port.RagTargetAdapter;
import com.local.ragingressqueue.target.port.TargetPressureSnapshot;
import com.local.ragingressqueue.target.port.TargetStatusSnapshot;
import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

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
        FakeRagTargetAdapter adapter = new FakeRagTargetAdapter(
            new TargetPressureSnapshot(TargetPressure.OPEN, 0, 0, 100, null)
        );

        Map<String, Object> status = new StatusService(
            adapter,
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
    }

    @Test
    void configuredLiveStatusUsesInjectedProfileRegistry() {
        FakeRagTargetAdapter adapter = new FakeRagTargetAdapter(
            new TargetPressureSnapshot(TargetPressure.OPEN, 0, 0, 100, null)
        );
        TargetProfileRegistry registry = new TargetProfileRegistry(Map.of(
            "custom-ragflow-profile",
            new TargetProfile("custom-ragflow-profile", BackendKind.RAGFLOW, "custom-role")
        ));

        Map<String, Object> status = new StatusService(adapter, null, registry).currentStatus();

        assertThat(adapter.lastTargetProfile).isEqualTo("custom-ragflow-profile");
        assertThat(status.get("target")).isEqualTo(Map.of(
            "name", "ragflow",
            "pressure", "OPEN",
            "running", 0,
            "unstart", 0,
            "sampled", 100
        ));
    }

    @Test
    void configuredLiveStatusRejectsNullProfileRegistry() {
        FakeRagTargetAdapter adapter = new FakeRagTargetAdapter(
            new TargetPressureSnapshot(TargetPressure.OPEN, 0, 0, 100, null)
        );

        assertThatThrownBy(() -> new StatusService(adapter, null, null))
            .isInstanceOf(NullPointerException.class)
            .hasMessageContaining("profileRegistry");
    }

    @Test
    void configuredLiveStatusRejectsRegistryWithoutBackendKind() {
        FakeRagTargetAdapter adapter = new FakeRagTargetAdapter(
            new TargetPressureSnapshot(TargetPressure.OPEN, 0, 0, 100, null)
        );
        TargetProfileRegistry registry = new TargetProfileRegistry(Map.of(
            "custom-ragflow-profile",
            new TargetProfile("custom-ragflow-profile", null, "custom-role")
        ));

        assertThatThrownBy(() -> new StatusService(adapter, null, registry))
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("primary profile");
    }

    @Test
    void configuredLiveStatusReportsThrottledWhenRagFlowBacklogIsHigh() {
        FakeRagTargetAdapter adapter = new FakeRagTargetAdapter(
            new TargetPressureSnapshot(TargetPressure.THROTTLED, 20, 2, 100, null)
        );

        Map<String, Object> status = new StatusService(adapter, null).currentStatus();

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
        FakeRagTargetAdapter adapter = new FakeRagTargetAdapter(
            TargetPressureSnapshot.closed("pressure_read_failed")
        );

        Map<String, Object> status = new StatusService(adapter, null).currentStatus();

        assertThat(status.get("target")).isEqualTo(Map.of(
            "name", "ragflow",
            "pressure", "CLOSED",
            "running", 0,
            "unstart", 0,
            "sampled", 0,
            "reason", "pressure_read_failed"
        ));
    }

    private static final class FakeRagTargetAdapter implements RagTargetAdapter {
        private final TargetPressureSnapshot snapshot;
        private String lastTargetProfile;

        private FakeRagTargetAdapter(TargetPressureSnapshot snapshot) {
            this.snapshot = snapshot;
        }

        @Override
        public TargetPressureSnapshot pressureSnapshot(String targetProfile) {
            lastTargetProfile = targetProfile;
            return snapshot;
        }

        @Override
        public DeliveryResult deliver(IngestJob job, String targetProfile) {
            throw new UnsupportedOperationException("not used");
        }

        @Override
        public TargetStatusSnapshot getStatus(IngestJob job, String targetProfile) {
            throw new UnsupportedOperationException("not used");
        }
    }
}
