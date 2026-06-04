package com.local.ragingressqueue.adapter.ext.ragflow;

import com.local.ragingressqueue.common.IngestStatus;

import java.util.Locale;

/**
 * Maps RAGFlow document run states to the backend-neutral {@link IngestStatus}. RAGFlow run-state
 * strings ({@code UNSTART/RUNNING/CANCEL/DONE/FAIL/FAILED}) never leak past this adapter boundary.
 *
 * <p>Unknown or null run states map fail-closed to {@link IngestStatus#FAILED}. {@code DEAD_LETTER}
 * is only produced here for a backend {@code CANCEL}; terminal dead-lettering from exhausted retries
 * is decided by the worker (max-deliver/quarantine), not from a single run-state read.</p>
 */
public final class RagFlowStatusMapper {
    private RagFlowStatusMapper() {
    }

    public static IngestStatus fromRunState(String ragflowRunState) {
        if (ragflowRunState == null) {
            return IngestStatus.FAILED;
        }
        return switch (ragflowRunState.trim().toUpperCase(Locale.ROOT)) {
            case "UNSTART" -> IngestStatus.QUEUED;
            case "RUNNING" -> IngestStatus.IN_FLIGHT;
            case "DONE" -> IngestStatus.INDEXED;
            case "FAIL", "FAILED" -> IngestStatus.FAILED;
            case "CANCEL" -> IngestStatus.DEAD_LETTER;
            default -> IngestStatus.FAILED;
        };
    }
}
