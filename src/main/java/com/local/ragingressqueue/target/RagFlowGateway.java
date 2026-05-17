package com.local.ragingressqueue.target;

import com.local.ragingressqueue.core.DocumentPayload;

import java.util.Map;

interface RagFlowGateway {
    RagFlowDocumentRef uploadDocument(String baseUrl, String apiKey, String datasetId, DocumentPayload payload);

    void updateMetadata(String baseUrl, String apiKey, String datasetId, String documentId, Map<String, String> metadata);

    void requestParse(String baseUrl, String apiKey, String datasetId, String documentId);
}
