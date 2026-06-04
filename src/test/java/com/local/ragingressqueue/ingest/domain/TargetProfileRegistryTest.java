package com.local.ragingressqueue.ingest.domain;

import com.local.ragingressqueue.target.port.BackendKind;
import org.junit.jupiter.api.Test;
import org.yaml.snakeyaml.Yaml;

import java.lang.reflect.RecordComponent;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Locale;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class TargetProfileRegistryTest {
    private final TargetProfileRegistry registry = TargetProfileRegistry.DEFAULT;

    @Test
    void knownProfilesRouteToRagflowBackendKind() {
        assertThat(registry.knownProfileIds()).isNotEmpty();
        for (String id : registry.knownProfileIds()) {
            assertThat(registry.backendKind(id)).contains(BackendKind.RAGFLOW);
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
        TargetProfile profile = registry.find("ragflow-transcript-memory").orElseThrow();

        assertThat(profile.datasetRole()).isEqualTo("transcript-memory");
        assertThat(profile.backendKind()).isEqualTo(BackendKind.RAGFLOW);
        // The routing value object must not carry any physical backend resource id (dataset id/token).
        assertThat(TargetProfile.class.getRecordComponents())
            .extracting(RecordComponent::getName)
            .containsExactly("id", "backendKind", "datasetRole");
    }

    @Test
    @SuppressWarnings("unchecked")
    void registryStaysInParityWithApplicationYmlTargetProfiles() throws Exception {
        Map<String, Object> root = new Yaml().load(Files.readString(Path.of("src/main/resources/application.yml")));
        Map<String, Object> ragIngress = (Map<String, Object>) root.get("rag-ingress");
        Map<String, Object> ymlProfiles = (Map<String, Object>) ragIngress.get("target-profiles");

        assertThat(ymlProfiles.keySet()).isEqualTo(registry.knownProfileIds());

        ymlProfiles.forEach((id, raw) -> {
            Map<String, Object> entry = (Map<String, Object>) raw;
            String ymlAdapter = String.valueOf(entry.get("adapter"));
            String ymlDatasetRole = String.valueOf(entry.get("dataset-role"));
            TargetProfile profile = registry.find(id).orElseThrow();
            assertThat(ymlAdapter).isEqualTo(profile.backendKind().name().toLowerCase(Locale.ROOT));
            assertThat(ymlDatasetRole).isEqualTo(profile.datasetRole());
        });
    }
}
