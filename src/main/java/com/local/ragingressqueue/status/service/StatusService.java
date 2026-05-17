package com.local.ragingressqueue.status.service;

import com.local.ragingressqueue.adapter.infra.nats.QueueStatusProvider;
import com.local.ragingressqueue.adapter.infra.nats.QueueStatusSnapshot;
import com.local.ragingressqueue.target.port.RagTargetAdapter;
import com.local.ragingressqueue.target.port.TargetPressureSnapshot;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Service;

import java.util.Map;

@Service
@Profile("api")
public class StatusService {
    private final RagTargetAdapter adapter;
    private final QueueStatusProvider queueStatusProvider;

    public StatusService() {
        this(null, null);
    }

    @Autowired
    public StatusService(RagTargetAdapter adapter, QueueStatusProvider queueStatusProvider) {
        this.adapter = adapter;
        this.queueStatusProvider = queueStatusProvider;
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
        return adapter.pressureSnapshot("ragflow-transcript-memory");
    }

    private Map<String, Object> targetStatus(TargetPressureSnapshot snapshot) {
        if (snapshot.reason() != null) {
            return Map.of(
                "name", "ragflow",
                "pressure", snapshot.pressure().name(),
                "running", snapshot.running(),
                "unstart", snapshot.unstart(),
                "sampled", snapshot.sampled(),
                "reason", snapshot.reason()
            );
        }
        return Map.of(
            "name", "ragflow",
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
