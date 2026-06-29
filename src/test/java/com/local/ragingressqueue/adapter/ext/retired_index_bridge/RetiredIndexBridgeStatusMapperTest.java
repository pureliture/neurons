package com.local.ragingressqueue.adapter.ext.retired_index_bridge;

import com.local.ragingressqueue.common.IngestStatus;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class RetiredIndexBridgeStatusMapperTest {
    @Test
    void mapsCanonicalRunStatesToBackendNeutralStatus() {
        assertThat(RetiredIndexBridgeStatusMapper.fromRunState("UNSTART")).isEqualTo(IngestStatus.QUEUED);
        assertThat(RetiredIndexBridgeStatusMapper.fromRunState("RUNNING")).isEqualTo(IngestStatus.IN_FLIGHT);
        assertThat(RetiredIndexBridgeStatusMapper.fromRunState("DONE")).isEqualTo(IngestStatus.INDEXED);
        assertThat(RetiredIndexBridgeStatusMapper.fromRunState("FAIL")).isEqualTo(IngestStatus.FAILED);
        assertThat(RetiredIndexBridgeStatusMapper.fromRunState("FAILED")).isEqualTo(IngestStatus.FAILED);
        assertThat(RetiredIndexBridgeStatusMapper.fromRunState("CANCEL")).isEqualTo(IngestStatus.DEAD_LETTER);
    }

    @Test
    void normalizesCaseAndWhitespace() {
        assertThat(RetiredIndexBridgeStatusMapper.fromRunState(" done ")).isEqualTo(IngestStatus.INDEXED);
        assertThat(RetiredIndexBridgeStatusMapper.fromRunState("running")).isEqualTo(IngestStatus.IN_FLIGHT);
    }

    @Test
    void failsClosedForUnknownOrNullRunState() {
        assertThat(RetiredIndexBridgeStatusMapper.fromRunState(null)).isEqualTo(IngestStatus.FAILED);
        assertThat(RetiredIndexBridgeStatusMapper.fromRunState("")).isEqualTo(IngestStatus.FAILED);
        assertThat(RetiredIndexBridgeStatusMapper.fromRunState("WAT")).isEqualTo(IngestStatus.FAILED);
    }
}
