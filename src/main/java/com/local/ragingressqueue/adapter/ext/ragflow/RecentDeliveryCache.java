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
 * <p>Design trade-offs, valid for the single-worker deployment this serves:
 * <ul>
 *   <li>Optimistic: a cache hit short-circuits to "already delivered" without re-checking RAGFlow,
 *       so a document deleted externally within the TTL window would be skipped rather than
 *       re-uploaded. The TTL (configurable) bounds that window; keep it close to the expected
 *       search-index lag if external deletes are a concern.</li>
 *   <li>Not concurrency-serialized: the adapter consumes the queue on a single thread, so the
 *       seen→lookup→upload→record sequence is effectively serial per key. A multi-threaded
 *       consumer would need a per-key lock to fully close a same-key TOCTOU window.</li>
 * </ul>
 */
class RecentDeliveryCache {
    private final long ttlMillis;
    private final int maxSize;
    private final Clock clock;
    private final Map<String, Long> expiryByKey;

    RecentDeliveryCache(Duration ttl, int maxSize, Clock clock) {
        this.ttlMillis = Math.max(0L, ttl.toMillis());
        this.maxSize = Math.max(1, maxSize);
        this.clock = clock;
        // Insertion-ordered map. TTL is uniform, so insertion order equals expiry order: the eldest
        // entry is always the earliest to expire. Size-based eviction therefore drops the most-stale
        // entry first, and lazy TTL cleanup in seen() is sufficient. Memory is bounded by maxSize.
        this.expiryByKey = new LinkedHashMap<>(16, 0.75f, false) {
            @Override
            protected boolean removeEldestEntry(Map.Entry<String, Long> eldest) {
                return size() > RecentDeliveryCache.this.maxSize;
            }
        };
    }

    boolean seen(String datasetId, String fragment) {
        if (isBlank(fragment)) {
            return false;
        }
        String key = key(datasetId, fragment);
        synchronized (this) {
            Long expiry = expiryByKey.get(key);
            if (expiry == null) {
                return false;
            }
            if (clock.millis() >= expiry) {
                expiryByKey.remove(key);
                return false;
            }
            return true;
        }
    }

    void record(String datasetId, String fragment) {
        if (isBlank(fragment)) {
            return;
        }
        String key = key(datasetId, fragment);
        long expiry = clock.millis() + ttlMillis;
        synchronized (this) {
            // Remove first so re-recording refreshes both expiry and insertion recency.
            expiryByKey.remove(key);
            expiryByKey.put(key, expiry);
        }
    }

    private static String key(String datasetId, String fragment) {
        // datasetId and fragment are RAGFlow hex identifiers, so '|' is a collision-free delimiter.
        return (datasetId == null ? "" : datasetId) + "|" + fragment;
    }

    private static boolean isBlank(String value) {
        return value == null || value.isBlank();
    }
}
