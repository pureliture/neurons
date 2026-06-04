package com.local.ragingressqueue.status.service;

import com.local.ragingressqueue.ingest.domain.TargetProfileRegistry;
import com.local.ragingressqueue.queue.port.QueueStatusProvider;
import com.local.ragingressqueue.queue.port.QueueStatusSnapshot;
import com.local.ragingressqueue.target.port.BackendKind;
import com.local.ragingressqueue.target.port.RagTargetAdapter;
import com.local.ragingressqueue.target.port.TargetPressureSnapshot;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Service;

import java.util.Locale;
import java.util.Map;
import java.util.Objects;

@Service
@Profile("api")
public class StatusService {
    // Operator /status currently surfaces a single representative backend; the profile and backend
    // name are resolved from the registry rather than hardcoded. Multi-backend aggregation is a
    // tracked follow-up.
    private final RagTargetAdapter adapter;
    private final QueueStatusProvider queueStatusProvider;
    private final String primaryProfile;
    private final String targetName;

    public StatusService() {
        this(null, null, TargetProfileRegistry.DEFAULT);
    }

    @Autowired
    public StatusService(RagTargetAdapter adapter, QueueStatusProvider queueStatusProvider) {
        this(adapter, queueStatusProvider, TargetProfileRegistry.DEFAULT);
    }

    StatusService(
        RagTargetAdapter adapter,
        QueueStatusProvider queueStatusProvider,
        TargetProfileRegistry profileRegistry
    ) {
        TargetProfileRegistry registry = Objects.requireNonNull(
            profileRegistry,
            "profileRegistry must not be null"
        );
        this.adapter = adapter;
        this.queueStatusProvider = queueStatusProvider;
        this.primaryProfile = registry.primaryProfileId();
        BackendKind backendKind = registry.backendKind(primaryProfile)
            .orElseThrow(() -> new IllegalStateException(
                "primary profile is missing backend kind: " + primaryProfile
            ));
        this.targetName = backendKind.name().toLowerCase(Locale.ROOT);
    }

    public Map<String, Object> currentStatus() {
        TargetPressureSnapshot targetSnapshot = targetSnapshot();
        return Map.of(
            "queue", queueStatus(),
            "target", targetStatus(targetSnapshot),
            "documentStatus", Map.of("indexedCandidateCount", 0),
            "authorization", Map.of("authorizedCount", 0),
            "externalStatus", externalStatus(targetSnapshot)
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

    private TargetPressureSnapshot targetSnapshot() {
        if (adapter == null) {
            return TargetPressureSnapshot.closed("not_configured");
        }
        return adapter.pressureSnapshot(primaryProfile);
    }

    private Map<String, Object> targetStatus(TargetPressureSnapshot snapshot) {
        if (snapshot.reason() != null) {
            return Map.of(
                "name", targetName,
                "pressure", snapshot.pressure().name(),
                "running", snapshot.running(),
                "unstart", snapshot.unstart(),
                "sampled", snapshot.sampled(),
                "reason", snapshot.reason()
            );
        }
        return Map.of(
            "name", targetName,
            "pressure", snapshot.pressure().name(),
            "running", snapshot.running(),
            "unstart", snapshot.unstart(),
            "sampled", snapshot.sampled()
        );
    }

    private String externalStatus(TargetPressureSnapshot snapshot) {
        return "not_configured".equals(snapshot.reason()) ? "not_configured" : "configured";
    }
}
