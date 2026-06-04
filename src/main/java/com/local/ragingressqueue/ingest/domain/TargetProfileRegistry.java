package com.local.ragingressqueue.ingest.domain;

import com.local.ragingressqueue.target.port.BackendKind;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;
import java.util.Set;

/**
 * Single source of truth mapping logical {@code targetProfile} ids to a {@link BackendKind} and a
 * logical dataset role. Authoritative for which profiles the public enqueue contract accepts.
 *
 * <p>Physical backend resource ids (RAGFlow dataset ids, tokens) are intentionally absent — they
 * stay in the backend adapter/config. The {@link #DEFAULT} registry mirrors
 * {@code rag-ingress.target-profiles} in {@code application.yml}; a parity test keeps the two
 * consistent. Full {@code @ConfigurationProperties} binding + startup fail-fast is a tracked
 * follow-up, deliberately out of scope for this contract/groundwork slice.</p>
 */
public final class TargetProfileRegistry {
    public static final TargetProfileRegistry DEFAULT = new TargetProfileRegistry(defaultProfiles());

    private final Map<String, TargetProfile> profilesById;

    public TargetProfileRegistry(Map<String, TargetProfile> profilesById) {
        this.profilesById = Collections.unmodifiableMap(new LinkedHashMap<>(profilesById));
    }

    public boolean isKnown(String targetProfileId) {
        return targetProfileId != null && profilesById.containsKey(targetProfileId);
    }

    public Optional<TargetProfile> find(String targetProfileId) {
        return targetProfileId == null ? Optional.empty() : Optional.ofNullable(profilesById.get(targetProfileId));
    }

    public Optional<BackendKind> backendKind(String targetProfileId) {
        return find(targetProfileId).map(TargetProfile::backendKind);
    }

    /** Profile ids in declaration order. */
    public Set<String> knownProfileIds() {
        return profilesById.keySet();
    }

    /** Representative profile used by single-target operator surfaces; first declared profile. */
    public String primaryProfileId() {
        if (profilesById.isEmpty()) {
            throw new IllegalStateException("TargetProfileRegistry contains no profiles");
        }
        return profilesById.keySet().iterator().next();
    }

    private static Map<String, TargetProfile> defaultProfiles() {
        Map<String, TargetProfile> profiles = new LinkedHashMap<>();
        register(profiles, "ragflow-transcript-memory", "transcript-memory");
        register(profiles, "ragflow-session-memory", "session-memory");
        register(profiles, "ragflow-session-summary", "session-summary");
        register(profiles, "ragflow-project-memory", "project-memory");
        register(profiles, "ragflow-task-summary", "task-summary");
        register(profiles, "ragflow-approved-memory-card", "approved-memory-card");
        register(profiles, "ragflow-procedural-memory", "procedural-memory");
        return profiles;
    }

    private static void register(Map<String, TargetProfile> profiles, String id, String datasetRole) {
        profiles.put(id, new TargetProfile(id, BackendKind.RAGFLOW, datasetRole));
    }
}
