package com.local.ragingressqueue.delivery.domain;

import com.local.ragingressqueue.common.IngestStatus;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class DeliveryDecisionTest {
    @Test
    void deliveredMapsToInFlightBecauseBackendIndexingIsAsynchronous() {
        assertThat(DeliveryDecision.delivered().toIngestStatus()).isEqualTo(IngestStatus.IN_FLIGHT);
    }

    @Test
    void backpressureKeepsJobQueued() {
        assertThat(DeliveryDecision.skippedPressure("throttled").toIngestStatus()).isEqualTo(IngestStatus.QUEUED);
        assertThat(DeliveryDecision.noWork().toIngestStatus()).isEqualTo(IngestStatus.QUEUED);
    }

    @Test
    void transientDeliveryFailureRemainsQueuedForRetry() {
        assertThat(DeliveryDecision.retryScheduled("target rejected").toIngestStatus()).isEqualTo(IngestStatus.QUEUED);
    }

    @Test
    void exhaustedRetriesMapToDeadLetter() {
        assertThat(DeliveryDecision.quarantineCandidate("max deliver exceeded").toIngestStatus())
            .isEqualTo(IngestStatus.DEAD_LETTER);
    }
}
