package com.local.ragingressqueue.adapter.ext.ragflow;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;

import java.util.Collection;
import java.util.List;
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

    /**
     * Lists documents in the dataset whose name matches {@code keyword} (RAGFlow keyword search over
     * document names), paging through the result set. Returns each match's backend id and name so the
     * caller can identify prior versions of a logical document for supersede.
     */
    List<RagFlowDocumentSummary> listDocumentsByKeyword(String baseUrl, String apiKey, String datasetId, String keyword);

    /** Deletes the given documents from the dataset. A null/empty id collection is a no-op. */
    void deleteDocuments(String baseUrl, String apiKey, String datasetId, Collection<String> documentIds);
}
