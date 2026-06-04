package com.local.ragingressqueue.common;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class IngestStatusTest {
    @Test
    void definesBackendNeutralLifecycleStates() {
        assertThat(IngestStatus.values())
            .extracting(Enum::name)
            .containsExactlyInAnyOrder(
                "ACCEPTED",
                "QUEUED",
                "IN_FLIGHT",
                "INDEXED",
                "FAILED",
                "DEAD_LETTER"
            );
    }

    @Test
    void doesNotExposeBackendRunStatesOrAuthorization() {
        assertThat(IngestStatus.values())
            .extracting(Enum::name)
            .doesNotContain("AUTHORIZED", "DONE", "FAIL", "RUNNING", "UNSTART", "CANCEL", "THROTTLED", "DELIVERED", "INDEXING");
    }
}
