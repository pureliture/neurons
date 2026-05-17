package com.local.ragingressqueue.api;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

class IdempotencyStore {
    private final Map<String, String> contentHashesByKey = new ConcurrentHashMap<>();

    boolean conflicts(String idempotencyKey, String contentHash) {
        if (idempotencyKey == null || idempotencyKey.isBlank()) {
            return false;
        }
        String existing = contentHashesByKey.putIfAbsent(idempotencyKey, contentHash);
        return existing != null && !existing.equals(contentHash);
    }
}
