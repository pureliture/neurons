package com.local.ragingressqueue.adapter.ext.ragflow;

import java.time.Clock;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * Remembers content_hash fragments delivered to a dataset in the recent past so that
 * back-to-back re-deliveries dedup even before RAGFlow's document search index reflects
 * the just-uploaded document. Bounded by a TTL and a maximum entry count; purely in-memory
 * and scoped to a single worker process.
 *
 * <p>Each entry tracks delivery progress as a {@link Stage} so a retry after a partial success
 * resumes only the steps that have not completed — it neither re-uploads (creating a duplicate)
 * nor short-circuits a still-incomplete delivery as finished:
 * <ul>
 *   <li>{@link Stage#UPLOADED}: the document was uploaded but metadata has not been applied. A retry
 *       resumes from updateMetadata using the stored {@code documentId}.</li>
 *   <li>{@link Stage#METADATA_DONE}: upload and metadata completed but parse was not requested. A
 *       retry resumes from requestParse only, without re-applying metadata.</li>
 *   <li>{@link Stage#FINALIZED}: upload, metadata, and parse all completed, so a later re-delivery
 *       short-circuits to "already delivered".</li>
 * </ul>
 *
 * <p>Design trade-offs, valid for the single-worker deployment this serves:
 * <ul>
 *   <li>Optimistic: a finalized hit short-circuits to "already delivered" without re-checking RAGFlow,
 *       so a document deleted externally within the TTL window would be skipped rather than
 *       re-uploaded. The TTL (configurable) bounds that window; keep it close to the expected
 *       search-index lag if external deletes are a concern.</li>
 *   <li>Not concurrency-serialized: the adapter consumes the queue on a single thread, so the
 *       lookup→upload→record sequence is effectively serial per key. A multi-threaded consumer would
 *       need a per-key lock to fully close a same-key TOCTOU window.</li>
 *   <li>Eviction is strictly by age (eldest first), not by stage. An UPLOADED entry becomes FINALIZED
 *       within one delivery cycle (milliseconds) unless a post-upload step fails, so a not-finalized
 *       entry is effectively always among the newest and is evicted only after {@code maxSize} (default
 *       10000) newer deliveries arrive within the TTL — at which point a retry falls back to
 *       {@code findByContentHash}, and the worst case is a benign duplicate, not data loss.</li>
 *   <li>Process-scoped: if the worker restarts mid-delivery the entry is lost. A retry then falls back
 *       to {@code findByContentHash}; a document uploaded but never parsed before the crash can still
 *       be matched by name and treated as present. Closing that gap would require querying RAGFlow run
 *       state and is out of scope here.</li>
 * </ul>
 */
class RecentDeliveryCache {
    private final long ttlMillis;
    private final int maxSize;
    private final Clock clock;
    private final Map<String, Entry> entriesByKey;

    RecentDeliveryCache(Duration ttl, int maxSize, Clock clock) {
        this.ttlMillis = Math.max(0L, ttl.toMillis());
        this.maxSize = Math.max(1, maxSize);
        this.clock = clock;
        // Insertion-ordered map. TTL is uniform, so insertion order equals expiry order: the eldest
        // entry is always the earliest to expire. Size-based eviction therefore drops the most-stale
        // entry first, and lazy TTL cleanup in lookup() is sufficient. Memory is bounded by maxSize.
        // Size the backing table so it holds maxSize entries without resizing: capacity must exceed
        // maxSize / loadFactor (0.75). Capped to keep a large maxSize from over-allocating up front.
        int initialCapacity = Math.min((int) Math.ceil(this.maxSize / 0.75) + 1, 2048);
        this.entriesByKey = new LinkedHashMap<>(initialCapacity, 0.75f, false) {
            @Override
            protected boolean removeEldestEntry(Map.Entry<String, Entry> eldest) {
                return size() > RecentDeliveryCache.this.maxSize;
            }
        };
    }

    /** Delivery progress for a cached (dataset, content-hash fragment) pair. */
    enum Stage {
        UPLOADED,
        METADATA_DONE,
        FINALIZED
    }

    /**
     * A recorded delivery for a (dataset, content-hash fragment) pair.
     *
     * @param expiry     epoch millis after which the entry is stale
     * @param stage      how far the delivery has progressed
     * @param documentId the uploaded RAGFlow document id used to resume post-upload steps on retry;
     *                   may be null for an entry recorded from a pre-existing document found via search
     */
    record Entry(long expiry, Stage stage, String documentId) {
    }

    /** Returns the live cache entry for the key, or null if absent or expired. */
    Entry lookup(String datasetId, String fragment) {
        if (isBlank(fragment)) {
            return null;
        }
        String key = key(datasetId, fragment);
        synchronized (this) {
            Entry entry = entriesByKey.get(key);
            if (entry == null) {
                return null;
            }
            if (clock.millis() > entry.expiry()) {
                entriesByKey.remove(key);
                return null;
            }
            return entry;
        }
    }

    /**
     * Records an uploaded document whose metadata/parse have not run, so a retry resumes the
     * post-upload steps with {@code documentId} instead of re-uploading.
     */
    void recordUploaded(String datasetId, String fragment, String documentId) {
        put(datasetId, fragment, Stage.UPLOADED, documentId);
    }

    /** Marks upload + metadata complete (parse still pending) so a retry resumes from parse only. */
    void markMetadataDone(String datasetId, String fragment, String documentId) {
        put(datasetId, fragment, Stage.METADATA_DONE, documentId);
    }

    /** Marks a (dataset, fragment) delivery fully complete so later re-deliveries short-circuit. */
    void markFinalized(String datasetId, String fragment, String documentId) {
        put(datasetId, fragment, Stage.FINALIZED, documentId);
    }

    private void put(String datasetId, String fragment, Stage stage, String documentId) {
        if (isBlank(fragment)) {
            return;
        }
        String key = key(datasetId, fragment);
        synchronized (this) {
            // Preserve a previously recorded document id if this transition does not carry one (the
            // search-discovery path finalizes with a null id), so a known id is never lost.
            Entry existing = entriesByKey.get(key);
            String resolvedId = documentId != null ? documentId
                : (existing != null ? existing.documentId() : null);
            Entry entry = new Entry(clock.millis() + ttlMillis, stage, resolvedId);
            // Remove first so re-recording refreshes both expiry and insertion recency.
            entriesByKey.remove(key);
            entriesByKey.put(key, entry);
        }
    }

    private static String key(String datasetId, String fragment) {
        // datasetId and fragment are RAGFlow hex identifiers, so '|' is a collision-free delimiter.
        // datasetId is guaranteed non-blank by RagFlowTargetAdapter#isConfigured before the cache is
        // consulted, so it is not separately validated here.
        return (datasetId == null ? "" : datasetId) + "|" + fragment;
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
