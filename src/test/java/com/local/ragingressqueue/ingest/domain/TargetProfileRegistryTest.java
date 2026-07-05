package com.local.ragingressqueue.ingest.domain;

import com.local.ragingressqueue.target.port.BackendKind;
import org.junit.jupiter.api.Test;
import org.yaml.snakeyaml.Yaml;

import java.io.InputStream;
import java.lang.reflect.RecordComponent;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class TargetProfileRegistryTest {
    private final TargetProfileRegistry registry = TargetProfileRegistry.DEFAULT;

    @Test
    void emptyRegistryReportsNoPrimaryProfileClearly() {
        TargetProfileRegistry empty = new TargetProfileRegistry(Map.of());

        assertThat(empty.knownProfileIds()).isEmpty();
        assertThatThrownBy(empty::primaryProfileId)
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("no profiles");
    }

    @Test
    void nullRegistryInputBehavesLikeEmptyRegistry() {
        TargetProfileRegistry empty = new TargetProfileRegistry(null);

        assertThat(empty.knownProfileIds()).isEmpty();
        assertThat(empty.isKnown("index-transcript-memory")).isFalse();
        assertThatThrownBy(empty::primaryProfileId)
            .isInstanceOf(IllegalStateException.class)
            .hasMessageContaining("no profiles");
    }

    @Test
    void rejectsNullProfileValue() {
        Map<String, TargetProfile> malformed = new java.util.HashMap<>();
        malformed.put("index-transcript-memory", null);

        assertThatThrownBy(() -> new TargetProfileRegistry(malformed))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("must not be null");
    }

    @Test
    void rejectsProfileKeyMismatch() {
        assertThatThrownBy(() -> new TargetProfileRegistry(Map.of(
            "index-transcript-memory",
            new TargetProfile("index-session-memory", BackendKind.RETIRED_INDEX_BRIDGE, "transcript-memory")
        )))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("key must match");
    }

    @Test
    void knownProfilesRouteToRetiredIndexBridgeBackendKind() {
        assertThat(registry.knownProfileIds()).isNotEmpty();
        for (String id : registry.knownProfileIds()) {
            assertThat(registry.backendKind(id)).contains(BackendKind.RETIRED_INDEX_BRIDGE);
        }
    }

    @Test
    void unknownProfileIsNotKnownAndHasNoBackendKind() {
        assertThat(registry.isKnown("does-not-exist")).isFalse();
        assertThat(registry.backendKind("does-not-exist")).isEmpty();
        assertThat(registry.isKnown(null)).isFalse();
    }

    @Test
    void exposesLogicalDatasetRoleButNeverPhysicalResourceId() {
        TargetProfile profile = registry.find("index-transcript-memory").orElseThrow();

        assertThat(profile.datasetRole()).isEqualTo("transcript-memory");
        assertThat(profile.backendKind()).isEqualTo(BackendKind.RETIRED_INDEX_BRIDGE);
        // The routing value object must not carry any physical backend resource id (dataset id/token).
        assertThat(TargetProfile.class.getRecordComponents())
            .extracting(RecordComponent::getName)
            .containsExactly("id", "backendKind", "datasetRole");
    }

    @Test
    @SuppressWarnings("unchecked")
    void registryStaysInParityWithApplicationYmlTargetProfiles() throws Exception {
        // Load from the classpath (build/resources), not a CWD-relative path, so the test is
        // robust to the working directory the test runner is launched from.
        Map<String, Object> root;
        try (InputStream yml = getClass().getResourceAsStream("/application.yml")) {
            assertThat(yml).as("application.yml must be on the test classpath").isNotNull();
            root = new Yaml().load(yml);
        }
        Map<String, Object> ragIngress = (Map<String, Object>) root.get("rag-ingress");
        Map<String, Object> ymlProfiles = (Map<String, Object>) ragIngress.get("target-profiles");

        assertThat(new ArrayList<>(ymlProfiles.keySet())).containsExactlyElementsOf(registry.knownProfileIds());
        assertThat(registry.primaryProfileId()).isEqualTo(ymlProfiles.keySet().iterator().next());

        ymlProfiles.forEach((id, raw) -> {
            Map<String, Object> entry = (Map<String, Object>) raw;
            String ymlAdapter = String.valueOf(entry.get("adapter"));
            String ymlDatasetRole = String.valueOf(entry.get("dataset-role"));
            TargetProfile profile = registry.find(id).orElseThrow();
            assertThat(ymlAdapter).isEqualTo(profile.backendKind().name().toLowerCase(Locale.ROOT));
            assertThat(ymlDatasetRole).isEqualTo(profile.datasetRole());
        });
    }

    @Test
    @SuppressWarnings("unchecked")
    void registryAndApplicationYmlStayInParityWithSharedTargetProfileContract() throws Exception {
        Map<String, Object> contract = new Yaml().load(
            Files.readString(Path.of("docs/contracts/target-profiles.yaml"))
        );
        Map<String, Object> profiles = (Map<String, Object>) contract.get("profiles");
        Map<String, Object> applicationProfiles = applicationTargetProfiles();

        assertThat(contract.get("schemaVersion")).isEqualTo("neurons.target_profiles.v1");
        assertThat(new ArrayList<>(profiles.keySet())).containsExactlyElementsOf(registry.knownProfileIds());
        assertThat(new ArrayList<>(applicationProfiles.keySet())).containsExactlyElementsOf(profiles.keySet());

        profiles.forEach((id, raw) -> {
            Map<String, Object> profileContract = (Map<String, Object>) raw;
            TargetProfile profile = registry.find(id).orElseThrow();
            Map<String, Object> applicationProfile = (Map<String, Object>) applicationProfiles.get(id);

            assertThat(profileContract)
                .containsEntry("backendKind", profile.backendKind().name())
                .containsEntry("datasetRole", profile.datasetRole());
            assertThat(profileContract.get("retiredIndexBridgeDatasetEnv"))
                .as("contract names the public env key for %s", id)
                .isEqualTo(retiredIndexBridgeDatasetEnvKey(id));
            assertThat(applicationProfile.get("dataset-role")).isEqualTo(profileContract.get("datasetRole"));
            assertThat(applicationProfile.get("adapter")).isEqualTo("retired_index_bridge");

            assertThat(profileContract.keySet())
                .doesNotContain("datasetId", "dataset_id", "token", "apiKey", "api_key");
            assertThat(profileContract.values().toString().toLowerCase(Locale.ROOT))
                .doesNotContain("ds_")
                .doesNotContain("token");
        });
    }

    @SuppressWarnings("unchecked")
    private Map<String, Object> applicationTargetProfiles() throws Exception {
        Map<String, Object> root;
        try (InputStream yml = getClass().getResourceAsStream("/application.yml")) {
            assertThat(yml).as("application.yml must be on the test classpath").isNotNull();
            root = new Yaml().load(yml);
        }
        Map<String, Object> ragIngress = (Map<String, Object>) root.get("rag-ingress");
        return new LinkedHashMap<>((Map<String, Object>) ragIngress.get("target-profiles"));
    }

    private static String retiredIndexBridgeDatasetEnvKey(String profileId) {
        String role = profileId.substring("index-".length()).replace("-", "_").toUpperCase(Locale.ROOT);
        return "RETIRED_INDEX_BRIDGE_" + role + "_DATASET_ID";
    }
}
