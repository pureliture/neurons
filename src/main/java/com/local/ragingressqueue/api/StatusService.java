package com.local.ragingressqueue.api;

import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Service;

import java.util.Map;

@Service
@Profile("api")
public class StatusService {
    private final boolean ragFlowDeliveryEnabled;
    private final String ragFlowBaseUrl;
    private final String ragFlowApiKey;
    private final String transcriptMemoryDatasetId;

    public StatusService() {
        this(false, "", "", "");
    }

    @Autowired
    public StatusService(
        @Value("${rag-ingress.target.ragflow.delivery-enabled:false}") boolean ragFlowDeliveryEnabled,
        @Value("${rag-ingress.target.ragflow.base-url:}") String ragFlowBaseUrl,
        @Value("${rag-ingress.target.ragflow.api-key:}") String ragFlowApiKey,
        @Value("${rag-ingress.target-profiles.ragflow-transcript-memory.dataset-id:}") String transcriptMemoryDatasetId
    ) {
        this.ragFlowDeliveryEnabled = ragFlowDeliveryEnabled;
        this.ragFlowBaseUrl = ragFlowBaseUrl == null ? "" : ragFlowBaseUrl.trim();
        this.ragFlowApiKey = ragFlowApiKey == null ? "" : ragFlowApiKey.trim();
        this.transcriptMemoryDatasetId = transcriptMemoryDatasetId == null ? "" : transcriptMemoryDatasetId.trim();
    }

    public Map<String, Object> currentStatus() {
        return Map.of(
            "queue", Map.of("pending", 0, "inFlight", 0, "redelivered", 0, "deadLetter", 0),
            "target", Map.of("name", "ragflow", "pressure", pressure()),
            "documentStatus", Map.of("indexedCandidateCount", 0),
            "authorization", Map.of("authorizedCount", 0),
            "externalStatus", externalStatus()
        );
    }

    private String pressure() {
        return isLiveConfigured() ? "OPEN" : "CLOSED";
    }

    private String externalStatus() {
        return isLiveConfigured() ? "configured" : "not_configured";
    }

    private boolean isLiveConfigured() {
        return ragFlowDeliveryEnabled
            && !ragFlowBaseUrl.isEmpty()
            && !ragFlowApiKey.isEmpty()
            && !transcriptMemoryDatasetId.isEmpty();
    }
}
