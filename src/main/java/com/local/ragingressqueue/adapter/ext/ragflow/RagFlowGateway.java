package com.local.ragingressqueue.adapter.ext.ragflow;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;

import java.util.Map;

public interface RagFlowGateway {
    RagFlowDocumentRef uploadDocument(String baseUrl, String apiKey, String datasetId, DocumentPayload payload);

    void updateMetadata(String baseUrl, String apiKey, String datasetId, String documentId, Map<String, String> metadata);

    void requestParse(String baseUrl, String apiKey, String datasetId, String documentId);

    RagFlowPressureSnapshot pressureSnapshot(String baseUrl, String apiKey, String datasetId);
}
