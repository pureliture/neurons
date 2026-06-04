package com.local.ragingressqueue.adapter.ext.ragflow;

import com.local.ragingressqueue.common.IngestStatus;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class RagFlowStatusMapperTest {
    @Test
    void mapsCanonicalRunStatesToBackendNeutralStatus() {
        assertThat(RagFlowStatusMapper.fromRunState("UNSTART")).isEqualTo(IngestStatus.QUEUED);
        assertThat(RagFlowStatusMapper.fromRunState("RUNNING")).isEqualTo(IngestStatus.IN_FLIGHT);
        assertThat(RagFlowStatusMapper.fromRunState("DONE")).isEqualTo(IngestStatus.INDEXED);
        assertThat(RagFlowStatusMapper.fromRunState("FAIL")).isEqualTo(IngestStatus.FAILED);
        assertThat(RagFlowStatusMapper.fromRunState("FAILED")).isEqualTo(IngestStatus.FAILED);
        assertThat(RagFlowStatusMapper.fromRunState("CANCEL")).isEqualTo(IngestStatus.DEAD_LETTER);
    }

    @Test
    void normalizesCaseAndWhitespace() {
        assertThat(RagFlowStatusMapper.fromRunState(" done ")).isEqualTo(IngestStatus.INDEXED);
        assertThat(RagFlowStatusMapper.fromRunState("running")).isEqualTo(IngestStatus.IN_FLIGHT);
    }

    @Test
    void failsClosedForUnknownOrNullRunState() {
        assertThat(RagFlowStatusMapper.fromRunState(null)).isEqualTo(IngestStatus.FAILED);
        assertThat(RagFlowStatusMapper.fromRunState("")).isEqualTo(IngestStatus.FAILED);
        assertThat(RagFlowStatusMapper.fromRunState("WAT")).isEqualTo(IngestStatus.FAILED);
    }
}
