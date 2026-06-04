package com.local.ragingressqueue.adapter.ext.ragflow;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.common.IngestStatus;
import com.local.ragingressqueue.delivery.domain.TargetPressure;
import com.local.ragingressqueue.target.port.RagTargetAdapter;
import com.local.ragingressqueue.delivery.domain.DeliveryResult;
import com.local.ragingressqueue.target.port.TargetPressureSnapshot;
import com.local.ragingressqueue.target.port.TargetStatusSnapshot;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

import java.time.Clock;
import java.time.Duration;
import java.util.LinkedHashMap;
import java.util.Map;

@Component
@Profile({"api", "worker"})
public class RagFlowTargetAdapter implements RagTargetAdapter {
    private static final Logger LOGGER = LoggerFactory.getLogger(RagFlowTargetAdapter.class);
    private static final Duration DEFAULT_DEDUP_CACHE_TTL = Duration.ofMinutes(10);
    private static final int DEFAULT_DEDUP_CACHE_MAX_SIZE = 10_000;

    private final boolean deliveryEnabled;
    private final String baseUrl;
    private final String apiKey;
    private final Map<String, String> datasetIds;
    private final RagFlowGateway gateway;
    private final RagFlowPressurePolicy pressurePolicy;
    private final RecentDeliveryCache recentDeliveryCache;

    public RagFlowTargetAdapter(@Value("${rag-ingress.target.ragflow.delivery-enabled:false}") boolean deliveryEnabled) {
        this(deliveryEnabled, "", "", Map.of(), null, RagFlowPressurePolicy.DEFAULT);
    }

    @Autowired
    public RagFlowTargetAdapter(
        @Value("${rag-ingress.target.ragflow.delivery-enabled:false}") boolean deliveryEnabled,
        @Value("${rag-ingress.target.ragflow.base-url:}") String baseUrl,
        @Value("${rag-ingress.target.ragflow.api-key:}") String apiKey,
        @Value("${rag-ingress.target-profiles.ragflow-transcript-memory.dataset-id:}") String transcriptMemoryDatasetId,
        @Value("${rag-ingress.target-profiles.ragflow-session-memory.dataset-id:}") String sessionMemoryDatasetId,
        @Value("${rag-ingress.target-profiles.ragflow-session-summary.dataset-id:}") String sessionSummaryDatasetId,
        @Value("${rag-ingress.target-profiles.ragflow-project-memory.dataset-id:}") String projectMemoryDatasetId,
        @Value("${rag-ingress.target-profiles.ragflow-task-summary.dataset-id:}") String taskSummaryDatasetId,
        @Value("${rag-ingress.target-profiles.ragflow-approved-memory-card.dataset-id:}") String approvedMemoryCardDatasetId,
        @Value("${rag-ingress.target-profiles.ragflow-procedural-memory.dataset-id:}") String proceduralMemoryDatasetId,
        @Value("${rag-ingress.target.ragflow.pressure.running-throttle-threshold:20}") int runningThrottleThreshold,
        @Value("${rag-ingress.target.ragflow.pressure.unstart-throttle-threshold:5}") int unstartThrottleThreshold,
        @Value("${rag-ingress.target.ragflow.pressure.running-closed-threshold:100}") int runningClosedThreshold,
        @Value("${rag-ingress.target.ragflow.pressure.unstart-closed-threshold:25}") int unstartClosedThreshold,
        @Value("${rag-ingress.target.ragflow.dedup-cache.ttl-seconds:600}") long dedupCacheTtlSeconds,
        @Value("${rag-ingress.target.ragflow.dedup-cache.max-size:10000}") int dedupCacheMaxSize,
        RagFlowGateway gateway
    ) {
        this(
            deliveryEnabled,
            baseUrl,
            apiKey,
            Map.of(
                "ragflow-transcript-memory", transcriptMemoryDatasetId,
                "ragflow-session-memory", firstNonBlank(sessionMemoryDatasetId, sessionSummaryDatasetId),
                "ragflow-session-summary", sessionSummaryDatasetId,
                "ragflow-project-memory", projectMemoryDatasetId,
                "ragflow-task-summary", taskSummaryDatasetId,
                "ragflow-approved-memory-card", approvedMemoryCardDatasetId,
                "ragflow-procedural-memory", proceduralMemoryDatasetId
            ),
            gateway,
            new RagFlowPressurePolicy(
                runningThrottleThreshold,
                unstartThrottleThreshold,
                runningClosedThreshold,
                unstartClosedThreshold
            ),
            new RecentDeliveryCache(Duration.ofSeconds(dedupCacheTtlSeconds), dedupCacheMaxSize, Clock.systemUTC())
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
        this(
            deliveryEnabled,
            baseUrl,
            apiKey,
            datasetIds,
            gateway,
            pressurePolicy,
            new RecentDeliveryCache(DEFAULT_DEDUP_CACHE_TTL, DEFAULT_DEDUP_CACHE_MAX_SIZE, Clock.systemUTC())
        );
    }

    RagFlowTargetAdapter(
        boolean deliveryEnabled,
        String baseUrl,
        String apiKey,
        Map<String, String> datasetIds,
        RagFlowGateway gateway,
        RagFlowPressurePolicy pressurePolicy,
        RecentDeliveryCache recentDeliveryCache
    ) {
        this.deliveryEnabled = deliveryEnabled;
        this.baseUrl = trimToEmpty(baseUrl);
        this.apiKey = trimToEmpty(apiKey);
        this.datasetIds = datasetIds;
        this.gateway = gateway;
        this.pressurePolicy = pressurePolicy;
        this.recentDeliveryCache = recentDeliveryCache;
    }

    @Override
    public TargetPressureSnapshot pressureSnapshot(String targetProfile) {
        if (!isConfigured(targetProfile)) {
            return TargetPressureSnapshot.closed("not_configured");
        }
        try {
            RagFlowPressureSnapshot s = gateway.pressureSnapshot(baseUrl, apiKey, datasetId(targetProfile));
            TargetPressure p = pressurePolicy.evaluate(s);
            return new TargetPressureSnapshot(p, s.running(), s.unstart(), s.sampled(), null);
        } catch (RuntimeException error) {
            return TargetPressureSnapshot.closed("pressure_read_failed");
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
        String contentHashFragment = contentHashFragment(job);
        if (contentHashFragment.isEmpty()) {
            // A missing or malformed content hash silently disables dedup for this delivery, so a
            // requeue would upload again. Surface it for operators without leaking document content.
            LOGGER.warn("RAGFlow delivery proceeding without content_hash dedup: "
                + "missing or malformed content hash for target profile {}", targetProfile);
        }
        try {
            // Recent-delivery cache short-circuits before the extra RAGFlow lookup, covering the
            // search-index freshness race (a just-uploaded document may not yet be searchable) and
            // saving one GET per re-delivery.
            if (recentDeliveryCache.seen(datasetId, contentHashFragment)) {
                return DeliveryResult.delivered("redacted");
            }
            if (gateway.findByContentHash(baseUrl, apiKey, datasetId, contentHashFragment)) {
                recentDeliveryCache.record(datasetId, contentHashFragment);
                return DeliveryResult.delivered("redacted");
            }
            DocumentPayload payloadWithHash = payloadWithHashInFilename(job.payload(), contentHashFragment);
            RagFlowDocumentRef ref = gateway.uploadDocument(baseUrl, apiKey, datasetId, payloadWithHash);
            // Record immediately after a successful upload, before metadata/parse. If a post-upload
            // step throws, the delivery is reported failed and retried; recording here ensures the
            // retry deduplicates instead of creating a duplicate while RAGFlow's search index lags.
            recentDeliveryCache.record(datasetId, contentHashFragment);
            gateway.updateMetadata(baseUrl, apiKey, datasetId, ref.documentId(), metadataFor(job, targetProfile));
            gateway.requestParse(baseUrl, apiKey, datasetId, ref.documentId());
            return DeliveryResult.delivered("redacted");
        } catch (RagFlowDeliveryException error) {
            return DeliveryResult.failed("ragflow delivery failed");
        }
    }

    @Override
    public TargetStatusSnapshot getStatus(IngestJob job, String targetProfile) {
        // No live RAGFlow run-state poll in this slice: report a conservative lifecycle status.
        // When live polling is wired, the backend run state flows through RagFlowStatusMapper.
        IngestStatus status = isConfigured(targetProfile) ? IngestStatus.ACCEPTED : IngestStatus.FAILED;
        return new TargetStatusSnapshot(
            job.contentHashPrefix(),
            job.contentHash(),
            targetProfile,
            status,
            "redacted"
        );
    }

    private static String contentHashFragment(IngestJob job) {
        String hash = job.contentHash();
        if (hash != null && hash.startsWith("sha256:") && hash.length() >= 19) {
            return hash.substring(7, 19);
        }
        return "";
    }

    private static DocumentPayload payloadWithHashInFilename(DocumentPayload payload, String contentHashFragment) {
        if (contentHashFragment.isEmpty()) {
            return payload;
        }
        String original = payload.filename();
        if (original == null || original.isBlank()) {
            return payload;
        }
        int dot = original.lastIndexOf('.');
        String nameWithHash = dot > 0
            ? original.substring(0, dot) + "-" + contentHashFragment + original.substring(dot)
            : original + "-" + contentHashFragment;
        return new DocumentPayload(
            payload.kind(),
            payload.redactionVersion(),
            nameWithHash,
            payload.contentType(),
            payload.body(),
            payload.metadata()
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
        if (targetProfile == null) {
            return "";
        }
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

    private static String firstNonBlank(String first, String fallback) {
        String trimmed = trimToEmpty(first);
        return trimmed.isEmpty() ? trimToEmpty(fallback) : trimmed;
    }
}
