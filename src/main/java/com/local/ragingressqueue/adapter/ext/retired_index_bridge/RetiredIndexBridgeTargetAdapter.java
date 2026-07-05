package com.local.ragingressqueue.adapter.ext.retired_index_bridge;

import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.ingest.domain.validation.ContentHashVerifier;
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
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

@Component
@Profile("retired-index-bridge")
public class RetiredIndexBridgeTargetAdapter implements RagTargetAdapter {
    private static final Logger LOGGER = LoggerFactory.getLogger(RetiredIndexBridgeTargetAdapter.class);
    private static final Duration DEFAULT_DEDUP_CACHE_TTL = Duration.ofMinutes(10);
    private static final int DEFAULT_DEDUP_CACHE_MAX_SIZE = 10_000;
    private static final String DEFAULT_SUPERSEDE_LOGICAL_ID_FIELD = "logical_document_id";

    private final boolean deliveryEnabled;
    private final String baseUrl;
    private final String apiKey;
    private final Map<String, String> datasetIds;
    private final RetiredIndexBridgeGateway gateway;
    private final RetiredIndexBridgePressurePolicy pressurePolicy;
    private final RecentDeliveryCache recentDeliveryCache;
    private final boolean supersedeEnabled;
    private final String supersedeLogicalIdField;

    public RetiredIndexBridgeTargetAdapter(@Value("${rag-ingress.target.index.delivery-enabled:false}") boolean deliveryEnabled) {
        this(deliveryEnabled, "", "", Map.of(), null, RetiredIndexBridgePressurePolicy.DEFAULT);
    }

    @Autowired
    public RetiredIndexBridgeTargetAdapter(
        @Value("${rag-ingress.target.index.delivery-enabled:false}") boolean deliveryEnabled,
        @Value("${rag-ingress.target.index.base-url:}") String baseUrl,
        @Value("${rag-ingress.target.index.api-key:}") String apiKey,
        @Value("${rag-ingress.target-profiles.index-transcript-memory.dataset-id:}") String transcriptMemoryDatasetId,
        @Value("${rag-ingress.target-profiles.index-session-memory.dataset-id:}") String sessionMemoryDatasetId,
        @Value("${rag-ingress.target-profiles.index-session-summary.dataset-id:}") String sessionSummaryDatasetId,
        @Value("${rag-ingress.target-profiles.index-project-memory.dataset-id:}") String projectMemoryDatasetId,
        @Value("${rag-ingress.target-profiles.index-task-summary.dataset-id:}") String taskSummaryDatasetId,
        @Value("${rag-ingress.target-profiles.index-approved-memory-card.dataset-id:}") String approvedMemoryCardDatasetId,
        @Value("${rag-ingress.target-profiles.index-procedural-memory.dataset-id:}") String proceduralMemoryDatasetId,
        @Value("${rag-ingress.target.index.pressure.running-throttle-threshold:20}") int runningThrottleThreshold,
        @Value("${rag-ingress.target.index.pressure.unstart-throttle-threshold:5}") int unstartThrottleThreshold,
        @Value("${rag-ingress.target.index.pressure.running-closed-threshold:100}") int runningClosedThreshold,
        @Value("${rag-ingress.target.index.pressure.unstart-closed-threshold:25}") int unstartClosedThreshold,
        @Value("${rag-ingress.target.index.dedup-cache.ttl-seconds:600}") long dedupCacheTtlSeconds,
        @Value("${rag-ingress.target.index.dedup-cache.max-size:10000}") int dedupCacheMaxSize,
        @Value("${rag-ingress.target.index.supersede.enabled:false}") boolean supersedeEnabled,
        @Value("${rag-ingress.target.index.supersede.logical-id-field:logical_document_id}") String supersedeLogicalIdField,
        RetiredIndexBridgeGateway gateway
    ) {
        this(
            deliveryEnabled,
            baseUrl,
            apiKey,
            Map.of(
                "index-transcript-memory", trimToEmpty(transcriptMemoryDatasetId),
                "index-session-memory", trimToEmpty(sessionMemoryDatasetId),
                "index-session-summary", trimToEmpty(sessionSummaryDatasetId),
                "index-project-memory", trimToEmpty(projectMemoryDatasetId),
                "index-task-summary", trimToEmpty(taskSummaryDatasetId),
                "index-approved-memory-card", trimToEmpty(approvedMemoryCardDatasetId),
                "index-procedural-memory", trimToEmpty(proceduralMemoryDatasetId)
            ),
            gateway,
            new RetiredIndexBridgePressurePolicy(
                runningThrottleThreshold,
                unstartThrottleThreshold,
                runningClosedThreshold,
                unstartClosedThreshold
            ),
            new RecentDeliveryCache(Duration.ofSeconds(dedupCacheTtlSeconds), dedupCacheMaxSize, Clock.systemUTC()),
            supersedeEnabled,
            supersedeLogicalIdField
        );
    }

    RetiredIndexBridgeTargetAdapter(
        boolean deliveryEnabled,
        String baseUrl,
        String apiKey,
        Map<String, String> datasetIds,
        RetiredIndexBridgeGateway gateway
    ) {
        this(deliveryEnabled, baseUrl, apiKey, datasetIds, gateway, RetiredIndexBridgePressurePolicy.DEFAULT);
    }

    RetiredIndexBridgeTargetAdapter(
        boolean deliveryEnabled,
        String baseUrl,
        String apiKey,
        Map<String, String> datasetIds,
        RetiredIndexBridgeGateway gateway,
        RetiredIndexBridgePressurePolicy pressurePolicy
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

    RetiredIndexBridgeTargetAdapter(
        boolean deliveryEnabled,
        String baseUrl,
        String apiKey,
        Map<String, String> datasetIds,
        RetiredIndexBridgeGateway gateway,
        RetiredIndexBridgePressurePolicy pressurePolicy,
        RecentDeliveryCache recentDeliveryCache
    ) {
        this(deliveryEnabled, baseUrl, apiKey, datasetIds, gateway, pressurePolicy, recentDeliveryCache,
            false, DEFAULT_SUPERSEDE_LOGICAL_ID_FIELD);
    }

    // Test/config seam for the version-supersede policy. Uses the default pressure policy and dedup
    // cache, varying only the supersede configuration.
    RetiredIndexBridgeTargetAdapter(
        boolean deliveryEnabled,
        String baseUrl,
        String apiKey,
        Map<String, String> datasetIds,
        RetiredIndexBridgeGateway gateway,
        boolean supersedeEnabled,
        String supersedeLogicalIdField
    ) {
        this(
            deliveryEnabled,
            baseUrl,
            apiKey,
            datasetIds,
            gateway,
            RetiredIndexBridgePressurePolicy.DEFAULT,
            new RecentDeliveryCache(DEFAULT_DEDUP_CACHE_TTL, DEFAULT_DEDUP_CACHE_MAX_SIZE, Clock.systemUTC()),
            supersedeEnabled,
            supersedeLogicalIdField
        );
    }

    RetiredIndexBridgeTargetAdapter(
        boolean deliveryEnabled,
        String baseUrl,
        String apiKey,
        Map<String, String> datasetIds,
        RetiredIndexBridgeGateway gateway,
        RetiredIndexBridgePressurePolicy pressurePolicy,
        RecentDeliveryCache recentDeliveryCache,
        boolean supersedeEnabled,
        String supersedeLogicalIdField
    ) {
        this.deliveryEnabled = deliveryEnabled;
        this.baseUrl = trimToEmpty(baseUrl);
        this.apiKey = trimToEmpty(apiKey);
        this.datasetIds = datasetIds;
        this.gateway = gateway;
        this.pressurePolicy = pressurePolicy;
        this.recentDeliveryCache = recentDeliveryCache;
        this.supersedeEnabled = supersedeEnabled;
        this.supersedeLogicalIdField = isBlank(supersedeLogicalIdField)
            ? DEFAULT_SUPERSEDE_LOGICAL_ID_FIELD
            : supersedeLogicalIdField.trim();
    }

    @Override
    public TargetPressureSnapshot pressureSnapshot(String targetProfile) {
        if (!isConfigured(targetProfile)) {
            return TargetPressureSnapshot.closed("not_configured");
        }
        try {
            RetiredIndexBridgePressureSnapshot s = gateway.pressureSnapshot(baseUrl, apiKey, datasetId(targetProfile));
            TargetPressure p = pressurePolicy.evaluate(s);
            return new TargetPressureSnapshot(p, s.running(), s.unstart(), s.sampled(), null);
        } catch (RuntimeException error) {
            return TargetPressureSnapshot.closed("pressure_read_failed");
        }
    }

    @Override
    public DeliveryResult deliver(IngestJob job, String targetProfile) {
        if (!isConfigured(targetProfile)) {
            return DeliveryResult.failed("retired_index_bridge delivery unavailable");
        }
        if (job.payload() == null || isBlank(job.payload().body())) {
            return DeliveryResult.failed("retired_index_bridge delivery failed");
        }
        String datasetId = datasetId(targetProfile);
        String contentHashFragment = contentHashFragment(job);
        if (contentHashFragment.isEmpty()) {
            // A missing or malformed content hash silently disables dedup for this delivery, so a
            // requeue would upload again. Surface it for operators without leaking document content.
            LOGGER.warn("RetiredIndexBridge delivery proceeding without content_hash dedup: "
                + "missing or malformed content hash for target profile {}", targetProfile);
        }
        // Stable logical-document identifier (hashed) used for version supersede. Empty unless the
        // producer supplies the configured logical-id field, so naming and supersede are inert for
        // documents (e.g. append-only chunks) that carry no logical id.
        String logicalIdFragment = logicalIdFragment(job);
        try {
            // Recent-delivery cache short-circuits before the extra RetiredIndexBridge lookup, covering the
            // retired-index-bridge freshness race (a just-uploaded document may not yet be searchable) and
            // saving one GET per re-delivery.
            RecentDeliveryCache.Entry cached = recentDeliveryCache.lookup(datasetId, contentHashFragment);
            if (cached != null && cached.stage() == RecentDeliveryCache.Stage.FINALIZED) {
                // Fully delivered before (upload + metadata + parse all completed): nothing to do.
                return DeliveryResult.delivered("redacted");
            }
            String documentId;
            boolean metadataDone;
            if (cached != null) {
                // Uploaded on a prior attempt but the post-upload steps did not finish (they threw and
                // the delivery was retried). Resume with the known document id instead of re-uploading,
                // and replay only the steps that did not complete, tracked by the cached stage.
                documentId = cached.documentId();
                metadataDone = cached.stage() == RecentDeliveryCache.Stage.METADATA_DONE;
            } else if (gateway.findByContentHash(baseUrl, apiKey, datasetId, contentHashFragment)) {
                // Already present in RetiredIndexBridge from a prior fully-successful delivery: record as finalized
                // so later re-deliveries skip even this lookup.
                recentDeliveryCache.markFinalized(datasetId, contentHashFragment, null);
                return DeliveryResult.delivered("redacted");
            } else {
                DocumentPayload payloadWithHash =
                    payloadWithHashInFilename(job.payload(), contentHashFragment, logicalIdFragment);
                RetiredIndexBridgeDocumentRef ref = gateway.uploadDocument(baseUrl, apiKey, datasetId, payloadWithHash);
                documentId = ref.documentId();
                metadataDone = false;
                // Mark uploaded (metadata/parse pending) before those steps run. If one throws, the
                // delivery is reported failed and retried; this entry makes the retry resume from the
                // failed step with the same document id instead of creating a duplicate while RetiredIndexBridge's
                // search index lags.
                recentDeliveryCache.recordUploaded(datasetId, contentHashFragment, documentId);
            }
            if (!metadataDone) {
                gateway.updateMetadata(baseUrl, apiKey, datasetId, documentId, metadataFor(job, targetProfile));
                // Record metadata completion so a parse-only failure does not replay updateMetadata.
                recentDeliveryCache.markMetadataDone(datasetId, contentHashFragment, documentId);
            }
            gateway.requestParse(baseUrl, apiKey, datasetId, documentId);
            // Only now is the delivery fully complete; promote the entry so re-deliveries short-circuit.
            recentDeliveryCache.markFinalized(datasetId, contentHashFragment, documentId);
            // The current version is safely in place; retire any prior versions of the same logical
            // document. Done last and best-effort so a cleanup failure never fails the delivery.
            supersedePriorVersions(datasetId, job.payload().filename(), contentHashFragment, logicalIdFragment);
            return DeliveryResult.delivered("redacted");
        } catch (RuntimeException error) {
            // Fail closed on any delivery error (RetiredIndexBridgeDeliveryException and otherwise), matching
            // pressureSnapshot's handling, so an unexpected unchecked exception does not escape after
            // the cache has been mutated. The recorded stage lets a retry resume safely. Log the cause
            // so a programming error is diagnosable rather than hidden behind the generic failure.
            LOGGER.warn("RetiredIndexBridge delivery failed for target profile {}", targetProfile, error);
            return DeliveryResult.failed("retired_index_bridge delivery failed");
        }
    }

    @Override
    public TargetStatusSnapshot getStatus(IngestJob job, String targetProfile) {
        // No live RetiredIndexBridge run-state poll in this slice: report a conservative lifecycle status.
        // When live polling is wired, the backend run state flows through RetiredIndexBridgeStatusMapper.
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

    private static DocumentPayload payloadWithHashInFilename(
        DocumentPayload payload, String contentHashFragment, String logicalIdFragment) {
        if (contentHashFragment.isEmpty()) {
            return payload;
        }
        return withFilename(payload, hashedFilename(payload.filename(), contentHashFragment, logicalIdFragment));
    }

    // Canonical uploaded name. The content-hash token is always last (dedup matches the trailing
    // token); a logical-id token precedes it when present so prior versions of the same logical
    // document can be found by keyword search and matched exactly for supersede.
    private static String hashedFilename(String original, String contentHashFragment, String logicalIdFragment) {
        String suffix = logicalIdFragment.isEmpty()
            ? "-" + contentHashFragment
            : "-" + logicalIdFragment + "-" + contentHashFragment;
        if (original == null || original.isBlank()) {
            // No usable original name: synthesize one carrying the suffix so findByContentHash can still
            // match on re-delivery. Returning the original name would embed no hash and make every
            // re-delivery upload a fresh duplicate.
            return "document" + suffix;
        }
        // Canonicalize exactly as the upload path does, so the name we compute for supersede matching
        // equals the name RetiredIndexBridge actually stores.
        String sanitized = HttpRetiredIndexBridgeGateway.sanitizeDocumentName(original);
        int dot = sanitized.lastIndexOf('.');
        return dot > 0
            ? sanitized.substring(0, dot) + suffix + sanitized.substring(dot)
            : sanitized + suffix;
    }

    private String logicalIdFragment(IngestJob job) {
        if (!supersedeEnabled) {
            return "";
        }
        String raw = logicalIdValue(job);
        if (isBlank(raw)) {
            return "";
        }
        // Hash the logical id to a fixed-width 16-hex fragment: collision-resistant (64-bit), uniform,
        // and never exposes the raw logical id in the document name.
        String hashed = ContentHashVerifier.sha256Hex(raw.trim());
        return hashed.length() >= 23 ? hashed.substring(7, 23) : "";
    }

    private String logicalIdValue(IngestJob job) {
        Map<String, String> metadata = job.payload() == null ? null : job.payload().metadata();
        String fromMetadata = metadata == null ? null : metadata.get(supersedeLogicalIdField);
        String fromSource = job.source() == null ? null : job.source().get(supersedeLogicalIdField);
        if (!isBlank(fromMetadata)) {
            if (!isBlank(fromSource) && !fromSource.equals(fromMetadata)) {
                // Both maps carry the field with different values; metadata wins. Surface the discarded
                // source value (length only) so a producer misconfiguration is visible without leaking it.
                LOGGER.warn("RetiredIndexBridge supersede logical-id field present in both payload metadata and source "
                    + "with differing values; using the metadata value");
            }
            return fromMetadata;
        }
        return fromSource;
    }

    private void supersedePriorVersions(
        String datasetId, String originalFilename, String contentHashFragment, String logicalIdFragment) {
        if (!supersedeEnabled || logicalIdFragment.isEmpty() || contentHashFragment.isEmpty()) {
            return;
        }
        // The just-delivered document's canonical name. A prior version is the very same name with only
        // the content-hash token differing, so unrelated documents (different base name, or written by
        // another tool) and sibling documents that merely share the logical id under a different name
        // are never eligible for deletion. Supersede is idempotent on the resume path because the
        // current version's own hash token is excluded by the inequality check below.
        String currentName = hashedFilename(originalFilename, contentHashFragment, logicalIdFragment);
        try {
            List<String> stale = new ArrayList<>();
            for (RetiredIndexBridgeDocumentSummary doc : gateway.listDocumentsByKeyword(baseUrl, apiKey, datasetId, logicalIdFragment)) {
                if (isPriorVersion(doc.name(), currentName, contentHashFragment)) {
                    stale.add(doc.documentId());
                }
            }
            if (!stale.isEmpty()) {
                gateway.deleteDocuments(baseUrl, apiKey, datasetId, stale);
            }
        } catch (RuntimeException error) {
            // Cleanup is best-effort: the new version is already delivered. Leave stale versions for a
            // later delivery to retire rather than failing (and re-queuing) a successful delivery.
            // Log the cause so a programming error here is diagnosable, not mistaken for a network blip.
            LOGGER.warn("RetiredIndexBridge supersede cleanup failed for a logical document; stale versions may remain", error);
        }
    }

    /**
     * A prior version is a document whose name equals the current upload's name with only the trailing
     * 12-hex content-hash token replaced by a different one. Requiring an exact match on everything
     * else (base name, logical-id token, extension) means a document with a different name — an
     * unrelated upload or a sibling sharing only the logical id — is never deleted.
     */
    private static boolean isPriorVersion(String candidateName, String currentName, String contentHashFragment) {
        if (candidateName == null) {
            return false;
        }
        String currentExt = extensionOf(currentName);
        String candidateExt = extensionOf(candidateName);
        if (!currentExt.equals(candidateExt)) {
            return false;
        }
        String currentBase = currentName.substring(0, currentName.length() - currentExt.length());
        String candidateBase = candidateName.substring(0, candidateName.length() - candidateExt.length());
        // currentBase ends with "-" + contentHashFragment; everything before that is the shared prefix
        // (base + logical-id token) that a genuine prior version must reproduce exactly.
        if (currentBase.length() <= contentHashFragment.length()) {
            return false;
        }
        String prefix = currentBase.substring(0, currentBase.length() - contentHashFragment.length());
        if (!candidateBase.startsWith(prefix)) {
            return false;
        }
        String hashToken = candidateBase.substring(prefix.length());
        return isHashFragment(hashToken) && !hashToken.equals(contentHashFragment);
    }

    private static String extensionOf(String name) {
        int dot = name.lastIndexOf('.');
        return dot > 0 ? name.substring(dot) : "";
    }

    private static boolean isHashFragment(String token) {
        if (token.length() != 12) {
            return false;
        }
        for (int i = 0; i < token.length(); i++) {
            char c = token.charAt(i);
            boolean hex = (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f');
            if (!hex) {
                return false;
            }
        }
        return true;
    }

    private static DocumentPayload withFilename(DocumentPayload payload, String filename) {
        return new DocumentPayload(
            payload.kind(),
            payload.redactionVersion(),
            filename,
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
        // Only when supersede is active: the source overlay above can clobber the logical-id field, so
        // keep the persisted value aligned with the one that drives the filename/supersede key (payload
        // metadata wins over source). Gated by the flag so the default path's metadata precedence is
        // unchanged when the feature is off.
        if (supersedeEnabled) {
            String logicalIdFromMetadata = job.payload().metadata() == null
                ? null
                : job.payload().metadata().get(supersedeLogicalIdField);
            if (!isBlank(logicalIdFromMetadata)) {
                metadata.put(supersedeLogicalIdField, logicalIdFromMetadata);
            }
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
