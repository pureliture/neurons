package com.local.ragingressqueue.adapter.ext.retired_index_bridge;

import com.local.ragingressqueue.common.IngestStatus;

import java.util.Locale;

/**
 * Maps RetiredIndexBridge document run states to the backend-neutral {@link IngestStatus}. RetiredIndexBridge run-state
 * strings ({@code UNSTART/RUNNING/CANCEL/DONE/FAIL/FAILED}) never leak past this adapter boundary.
 *
 * <p>Unknown or null run states map fail-closed to {@link IngestStatus#FAILED}. {@code DEAD_LETTER}
 * is only produced here for a backend {@code CANCEL}; terminal dead-lettering from exhausted retries
 * is decided by the worker (max-deliver/quarantine), not from a single run-state read.</p>
 */
public final class RetiredIndexBridgeStatusMapper {
    private RetiredIndexBridgeStatusMapper() {
    }

    public static IngestStatus fromRunState(String retired_index_bridgeRunState) {
        if (retired_index_bridgeRunState == null) {
            return IngestStatus.FAILED;
        }
        return switch (retired_index_bridgeRunState.trim().toUpperCase(Locale.ROOT)) {
            case "UNSTART" -> IngestStatus.QUEUED;
            case "RUNNING" -> IngestStatus.IN_FLIGHT;
            case "DONE" -> IngestStatus.INDEXED;
            case "FAIL", "FAILED" -> IngestStatus.FAILED;
            case "CANCEL" -> IngestStatus.DEAD_LETTER;
            default -> IngestStatus.FAILED;
        };
    }
}
