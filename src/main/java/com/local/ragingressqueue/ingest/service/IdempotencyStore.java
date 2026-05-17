package com.local.ragingressqueue.ingest.service;

import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;

public class IdempotencyStore {
    private final Map<String, String> contentHashesByKey = new ConcurrentHashMap<>();

    public boolean conflicts(String idempotencyKey, String contentHash) {
        if (idempotencyKey == null || idempotencyKey.isBlank()) {
            return false;
        }
        String existing = contentHashesByKey.putIfAbsent(idempotencyKey, contentHash);
        return existing != null && !existing.equals(contentHash);
    }
}
