package com.local.ragingressqueue.adapter.ext.ragflow;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;

import java.util.Map;

public interface RagFlowGateway {
    RagFlowDocumentRef uploadDocument(String baseUrl, String apiKey, String datasetId, DocumentPayload payload);

    void updateMetadata(String baseUrl, String apiKey, String datasetId, String documentId, Map<String, String> metadata);

    void requestParse(String baseUrl, String apiKey, String datasetId, String documentId);

    RagFlowPressureSnapshot pressureSnapshot(String baseUrl, String apiKey, String datasetId);

    /**
     * Returns true if a document already exists in the given dataset whose name carries
     * {@code contentHashFragment} as its hash-suffix token (the token immediately before the final
     * extension), paging through the keyword result set until the match is found or the results are
     * exhausted. Used for content_hash-based delivery dedup.
     */
    boolean findByContentHash(String baseUrl, String apiKey, String datasetId, String contentHashFragment);
}
