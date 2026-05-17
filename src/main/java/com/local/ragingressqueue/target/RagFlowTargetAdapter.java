package com.local.ragingressqueue.target;

import com.local.ragingressqueue.core.IngestJob;
import com.local.ragingressqueue.core.TargetIndexingState;
import com.local.ragingressqueue.core.TargetPressure;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

import java.util.LinkedHashMap;
import java.util.Map;

@Component
@Profile("worker")
public class RagFlowTargetAdapter implements RagTargetAdapter {
    private final boolean deliveryEnabled;
    private final String baseUrl;
    private final String apiKey;
    private final Map<String, String> datasetIds;
    private final RagFlowGateway gateway;
    private final RagFlowPressurePolicy pressurePolicy;

    public RagFlowTargetAdapter(@Value("${rag-ingress.target.ragflow.delivery-enabled:false}") boolean deliveryEnabled) {
        this(deliveryEnabled, "", "", Map.of(), null, RagFlowPressurePolicy.DEFAULT);
    }

    @Autowired
    public RagFlowTargetAdapter(
        @Value("${rag-ingress.target.ragflow.delivery-enabled:false}") boolean deliveryEnabled,
        @Value("${rag-ingress.target.ragflow.base-url:}") String baseUrl,
        @Value("${rag-ingress.target.ragflow.api-key:}") String apiKey,
        @Value("${rag-ingress.target-profiles.ragflow-transcript-memory.dataset-id:}") String transcriptMemoryDatasetId,
        @Value("${rag-ingress.target-profiles.ragflow-session-summary.dataset-id:}") String sessionSummaryDatasetId,
        @Value("${rag-ingress.target-profiles.ragflow-task-summary.dataset-id:}") String taskSummaryDatasetId,
        @Value("${rag-ingress.target-profiles.ragflow-approved-memory-card.dataset-id:}") String approvedMemoryCardDatasetId,
        @Value("${rag-ingress.target.ragflow.pressure.running-throttle-threshold:20}") int runningThrottleThreshold,
        @Value("${rag-ingress.target.ragflow.pressure.unstart-throttle-threshold:5}") int unstartThrottleThreshold,
        @Value("${rag-ingress.target.ragflow.pressure.running-closed-threshold:100}") int runningClosedThreshold,
        @Value("${rag-ingress.target.ragflow.pressure.unstart-closed-threshold:25}") int unstartClosedThreshold,
        RagFlowGateway gateway
    ) {
        this(
            deliveryEnabled,
            baseUrl,
            apiKey,
            Map.of(
                "ragflow-transcript-memory", transcriptMemoryDatasetId,
                "ragflow-session-summary", sessionSummaryDatasetId,
                "ragflow-task-summary", taskSummaryDatasetId,
                "ragflow-approved-memory-card", approvedMemoryCardDatasetId
            ),
            gateway,
            new RagFlowPressurePolicy(
                runningThrottleThreshold,
                unstartThrottleThreshold,
                runningClosedThreshold,
                unstartClosedThreshold
            )
        );
    }

    RagFlowTargetAdapter(
        boolean deliveryEnabled,
        String baseUrl,
        String apiKey,
        Map<String, String> datasetIds,
        RagFlowGateway gateway
    ) {
        this(deliveryEnabled, baseUrl, apiKey, datasetIds, gateway, RagFlowPressurePolicy.DEFAULT);
    }

    RagFlowTargetAdapter(
        boolean deliveryEnabled,
        String baseUrl,
        String apiKey,
        Map<String, String> datasetIds,
        RagFlowGateway gateway,
        RagFlowPressurePolicy pressurePolicy
    ) {
        this.deliveryEnabled = deliveryEnabled;
        this.baseUrl = trimToEmpty(baseUrl);
        this.apiKey = trimToEmpty(apiKey);
        this.datasetIds = datasetIds;
        this.gateway = gateway;
        this.pressurePolicy = pressurePolicy;
    }

    @Override
    public TargetPressure checkPressure(String targetProfile) {
        if (!isConfigured(targetProfile)) {
            return TargetPressure.CLOSED;
        }
        try {
            return pressurePolicy.evaluate(gateway.pressureSnapshot(baseUrl, apiKey, datasetId(targetProfile)));
        } catch (RuntimeException error) {
            return TargetPressure.CLOSED;
        }
    }

    @Override
    public DeliveryResult deliver(IngestJob job, String targetProfile) {
        if (!isConfigured(targetProfile)) {
            return DeliveryResult.failed("ragflow delivery unavailable");
        }
        if (job.payload() == null || isBlank(job.payload().body())) {
            return DeliveryResult.failed("ragflow delivery failed");
        }
        String datasetId = datasetId(targetProfile);
        try {
            RagFlowDocumentRef ref = gateway.uploadDocument(baseUrl, apiKey, datasetId, job.payload());
            gateway.updateMetadata(baseUrl, apiKey, datasetId, ref.documentId(), metadataFor(job, targetProfile));
            gateway.requestParse(baseUrl, apiKey, datasetId, ref.documentId());
            return DeliveryResult.delivered("redacted");
        } catch (RagFlowDeliveryException error) {
            return DeliveryResult.failed("ragflow delivery failed");
        }
    }

    @Override
    public TargetStatusSnapshot getStatus(IngestJob job, String targetProfile) {
        TargetIndexingState state = isConfigured(targetProfile) ? TargetIndexingState.ACCEPTED : TargetIndexingState.FAILED;
        return new TargetStatusSnapshot(
            job.contentHashPrefix(),
            job.contentHash(),
            targetProfile,
            state,
            "redacted"
        );
    }

    private boolean isConfigured(String targetProfile) {
        return deliveryEnabled
            && gateway != null
            && !isBlank(baseUrl)
            && !isBlank(apiKey)
            && !isBlank(datasetId(targetProfile));
    }

    private String datasetId(String targetProfile) {
        return trimToEmpty(datasetIds.get(targetProfile));
    }

    private Map<String, String> metadataFor(IngestJob job, String targetProfile) {
        Map<String, String> metadata = new LinkedHashMap<>();
        if (job.payload().metadata() != null) {
            metadata.putAll(job.payload().metadata());
        }
        if (job.source() != null) {
            metadata.putAll(job.source());
        }
        metadata.put("content_hash", job.contentHash());
        metadata.put("content_hash_prefix", job.contentHashPrefix());
        metadata.put("target_profile", targetProfile);
        metadata.put("kind", job.kind());
        metadata.put("redaction_version", job.payload().redactionVersion());
        return metadata;
    }

    private static boolean isBlank(String value) {
        return trimToEmpty(value).isEmpty();
    }

    private static String trimToEmpty(String value) {
        return value == null ? "" : value.trim();
    }
}
