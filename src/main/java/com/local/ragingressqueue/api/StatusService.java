package com.local.ragingressqueue.api;

import com.local.ragingressqueue.core.TargetPressure;
import com.local.ragingressqueue.queue.QueueStatusProvider;
import com.local.ragingressqueue.queue.QueueStatusSnapshot;
import com.local.ragingressqueue.target.RagFlowGateway;
import com.local.ragingressqueue.target.RagFlowPressurePolicy;
import com.local.ragingressqueue.target.RagFlowPressureSnapshot;
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
    private final RagFlowGateway ragFlowGateway;
    private final RagFlowPressurePolicy pressurePolicy;
    private final QueueStatusProvider queueStatusProvider;

    public StatusService() {
        this(false, "", "", "", null, RagFlowPressurePolicy.DEFAULT, null);
    }

    @Autowired
    public StatusService(
        @Value("${rag-ingress.target.ragflow.delivery-enabled:false}") boolean ragFlowDeliveryEnabled,
        @Value("${rag-ingress.target.ragflow.base-url:}") String ragFlowBaseUrl,
        @Value("${rag-ingress.target.ragflow.api-key:}") String ragFlowApiKey,
        @Value("${rag-ingress.target-profiles.ragflow-transcript-memory.dataset-id:}") String transcriptMemoryDatasetId,
        @Value("${rag-ingress.target.ragflow.pressure.running-throttle-threshold:20}") int runningThrottleThreshold,
        @Value("${rag-ingress.target.ragflow.pressure.unstart-throttle-threshold:5}") int unstartThrottleThreshold,
        @Value("${rag-ingress.target.ragflow.pressure.running-closed-threshold:100}") int runningClosedThreshold,
        @Value("${rag-ingress.target.ragflow.pressure.unstart-closed-threshold:25}") int unstartClosedThreshold,
        RagFlowGateway ragFlowGateway,
        QueueStatusProvider queueStatusProvider
    ) {
        this(
            ragFlowDeliveryEnabled,
            ragFlowBaseUrl,
            ragFlowApiKey,
            transcriptMemoryDatasetId,
            ragFlowGateway,
            new RagFlowPressurePolicy(
                runningThrottleThreshold,
                unstartThrottleThreshold,
                runningClosedThreshold,
                unstartClosedThreshold
            ),
            queueStatusProvider
        );
    }

    StatusService(
        boolean ragFlowDeliveryEnabled,
        String ragFlowBaseUrl,
        String ragFlowApiKey,
        String transcriptMemoryDatasetId,
        RagFlowGateway ragFlowGateway,
        RagFlowPressurePolicy pressurePolicy,
        QueueStatusProvider queueStatusProvider
    ) {
        this.ragFlowDeliveryEnabled = ragFlowDeliveryEnabled;
        this.ragFlowBaseUrl = ragFlowBaseUrl == null ? "" : ragFlowBaseUrl.trim();
        this.ragFlowApiKey = ragFlowApiKey == null ? "" : ragFlowApiKey.trim();
        this.transcriptMemoryDatasetId = transcriptMemoryDatasetId == null ? "" : transcriptMemoryDatasetId.trim();
        this.ragFlowGateway = ragFlowGateway;
        this.pressurePolicy = pressurePolicy;
        this.queueStatusProvider = queueStatusProvider;
    }

    public Map<String, Object> currentStatus() {
        return Map.of(
            "queue", queueStatus(),
            "target", targetStatus(),
            "documentStatus", Map.of("indexedCandidateCount", 0),
            "authorization", Map.of("authorizedCount", 0),
            "externalStatus", externalStatus()
        );
    }

    private Map<String, Object> queueStatus() {
        QueueStatusSnapshot snapshot = queueStatusProvider == null
            ? QueueStatusSnapshot.unavailable()
            : queueStatusProvider.currentStatus();
        return Map.of(
            "pending", snapshot.pending(),
            "inFlight", snapshot.inFlight(),
            "redelivered", snapshot.redelivered(),
            "deadLetter", snapshot.deadLetter()
        );
    }

    private Map<String, Object> targetStatus() {
        if (!isLiveConfigured()) {
            return closedTargetStatus("not_configured");
        }
        try {
            RagFlowPressureSnapshot snapshot = ragFlowGateway.pressureSnapshot(
                ragFlowBaseUrl,
                ragFlowApiKey,
                transcriptMemoryDatasetId
            );
            return Map.of(
                "name", "ragflow",
                "pressure", pressurePolicy.evaluate(snapshot).name(),
                "running", snapshot.running(),
                "unstart", snapshot.unstart(),
                "sampled", snapshot.sampled()
            );
        } catch (RuntimeException error) {
            return closedTargetStatus("pressure_read_failed");
        }
    }

    private Map<String, Object> closedTargetStatus(String reason) {
        return Map.of(
            "name", "ragflow",
            "pressure", TargetPressure.CLOSED.name(),
            "running", 0,
            "unstart", 0,
            "sampled", 0,
            "reason", reason
        );
    }

    private String externalStatus() {
        return isLiveConfigured() ? "configured" : "not_configured";
    }

    private boolean isLiveConfigured() {
        return ragFlowDeliveryEnabled
            && ragFlowGateway != null
            && !ragFlowBaseUrl.isEmpty()
            && !ragFlowApiKey.isEmpty()
            && !transcriptMemoryDatasetId.isEmpty();
    }
}
