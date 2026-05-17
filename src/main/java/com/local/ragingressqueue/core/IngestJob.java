package com.local.ragingressqueue.core;

import java.util.Map;

public record IngestJob(
    Map<String, String> source,
    DocumentPayload payload,
    String contentHash,
    String targetProfile,
    String kind,
    String idempotencyKey
) {
    public IngestJob withPayload(DocumentPayload newPayload) {
        return new IngestJob(source, newPayload, contentHash, targetProfile, kind, idempotencyKey);
    }

    public IngestJob withContentHash(String newContentHash) {
        return new IngestJob(source, payload, newContentHash, targetProfile, kind, idempotencyKey);
    }

    public IngestJob withIdempotencyKey(String newIdempotencyKey) {
        return new IngestJob(source, payload, contentHash, targetProfile, kind, newIdempotencyKey);
    }

    public String contentHashPrefix() {
        if (contentHash == null || contentHash.length() < 19) {
            return "unavailable";
        }
        return contentHash.substring(0, 19);
    }

    @Override
    public String toString() {
        return "IngestJob[targetProfile=<redacted>, kind=<redacted>, contentHashPrefix=%s, idempotencyKey=%s, payload=<redacted>]"
            .formatted(contentHashPrefix(), idempotencyKey == null ? "<generated>" : "<provided>");
    }
}
