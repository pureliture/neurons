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
 * <p>Physical backend resource ids and credentials are intentionally absent. The retired
 * external index bridge adapter is not loaded by the default Spring profiles; these logical
 * profiles preserve the public enqueue contract while active delivery is owned elsewhere.</p>
 */
public final class TargetProfileRegistry {
    public static final TargetProfileRegistry DEFAULT = new TargetProfileRegistry(defaultProfiles());

    private final Map<String, TargetProfile> profilesById;

    public TargetProfileRegistry(Map<String, TargetProfile> profilesById) {
        Map<String, TargetProfile> source = profilesById == null ? Collections.emptyMap() : profilesById;
        LinkedHashMap<String, TargetProfile> copy = new LinkedHashMap<>();
        source.forEach((id, profile) -> {
            if (id == null || id.isBlank()) {
                throw new IllegalArgumentException("profile id must not be blank");
            }
            if (profile == null) {
                throw new IllegalArgumentException("profile '" + id + "' must not be null");
            }
            if (!id.equals(profile.id())) {
                throw new IllegalArgumentException("profile key must match TargetProfile.id for '" + id + "'");
            }
            copy.put(id, profile);
        });
        this.profilesById = Collections.unmodifiableMap(copy);
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
        register(profiles, "index-transcript-memory", "transcript-memory");
        register(profiles, "index-session-memory", "session-memory");
        register(profiles, "index-session-summary", "session-summary");
        register(profiles, "index-project-memory", "project-memory");
        register(profiles, "index-task-summary", "task-summary");
        register(profiles, "index-approved-memory-card", "approved-memory-card");
        register(profiles, "index-procedural-memory", "procedural-memory");
        return profiles;
    }

    private static void register(Map<String, TargetProfile> profiles, String id, String datasetRole) {
        profiles.put(id, new TargetProfile(id, BackendKind.RETIRED_INDEX_BRIDGE, datasetRole));
    }
}
