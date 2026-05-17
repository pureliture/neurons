package com.local.ragingressqueue.ingest.dto;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;

import java.util.Map;

public record EnqueueRequest(
    String schemaVersion,
    Map<String, String> source,
    PayloadEnvelope payload,
    String contentHash,
    String targetProfile,
    String kind,
    String idempotencyKey
) {
    public IngestJob toIngestJob() {
        DocumentRequest document = payload == null ? null : payload.document();
        DocumentPayload documentPayload = document == null
            ? null
            : new DocumentPayload(
                payload.kind(),
                payload.redactionVersion(),
                document.filename(),
                document.contentType(),
                document.body(),
                document.metadata()
            );
        return new IngestJob(source, documentPayload, contentHash, targetProfile, kind, idempotencyKey);
    }
}
