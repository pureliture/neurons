package com.local.ragingressqueue.api;

import org.junit.jupiter.api.Test;

import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class StatusServiceTest {
    @Test
    void defaultStatusFailsClosed() {
        Map<String, Object> status = new StatusService().currentStatus();

        assertThat(status).containsEntry("externalStatus", "not_configured");
        assertThat(status.get("target")).isEqualTo(Map.of("name", "ragflow", "pressure", "CLOSED"));
    }

    @Test
    void configuredLiveStatusReportsOpenWithoutExposingSecrets() {
        Map<String, Object> status = new StatusService(
            true,
            "http://host.docker.internal:9380",
            "secret-token",
            "ds_1"
        ).currentStatus();

        assertThat(status).containsEntry("externalStatus", "configured");
        assertThat(status.get("target")).isEqualTo(Map.of("name", "ragflow", "pressure", "OPEN"));
        assertThat(status.toString())
            .doesNotContain("secret-token")
            .doesNotContain("ds_1")
            .doesNotContain("host.docker.internal");
    }
}
