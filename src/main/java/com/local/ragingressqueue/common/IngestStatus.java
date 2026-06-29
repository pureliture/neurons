package com.local.ragingressqueue.common;

/**
 * Backend-neutral lifecycle status for a RAG-ready document moving through the ingress bus.
 *
 * <p>This is the single public/job status vocabulary. The queue core and every public surface speak
 * only these values; backend adapters (e.g. RetiredIndexBridge) map their own run states into this enum
 * internally and never expose backend-specific run states past the adapter boundary.</p>
 */
public enum IngestStatus {
    /** Validated at enqueue, before it is published to the queue. */
    ACCEPTED,
    /** Durably held in the queue, awaiting worker delivery (incl. backpressure / retry-pending). */
    QUEUED,
    /** Handed to the backend adapter; backend indexing is asynchronous and not yet confirmed. */
    IN_FLIGHT,
    /** Backend confirmed the document is indexed. */
    INDEXED,
    /** A delivery attempt failed; non-terminal (the queue may retry). */
    FAILED,
    /** Terminal failure: retries exhausted, quarantined, or backend-cancelled. */
    DEAD_LETTER
}
