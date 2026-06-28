package com.local.ragingressqueue.runtime;

import org.junit.jupiter.api.Test;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import static org.assertj.core.api.Assertions.assertThat;

class K3sMigrationContractTest {
    private static final Path SPEC_DIR = Path.of("docs/specs/2026-06-27-neurons-k3s-migration");
    private static final Path CONTRACT_DIR = Path.of("deploy/k3s/public-contract");

    @Test
    void approvedSpecKeepsPublicPrivateBoundaryAndLiveMutationGates() throws IOException {
        String requirements = Files.readString(SPEC_DIR.resolve("requirements.md"), StandardCharsets.UTF_8);
        String design = Files.readString(SPEC_DIR.resolve("design.md"), StandardCharsets.UTF_8);

        assertThat(requirements).contains("neurons", "compose 전체", "removed legacy external-memory surface");
        assertThat(requirements).contains("Tailscale subnet router", "backup/restore rehearsal", "neurons-ops");
        assertThat(requirements).contains("safety window", "24h", "abort");
        assertThat(design).contains("public contract + private ops overlay");
        assertThat(design).contains("Live apply, secret mutation, compose stop, and primary cutover remain approval-gated");
        assertThat(design).contains("NetworkPolicy", "kube-apiserver", "egress");
    }

    @Test
    void workloadInventoryCoversComposeOwnedServicesAndExclusions() throws IOException {
        String inventory = Files.readString(
            CONTRACT_DIR.resolve("workload-inventory.yaml"),
            StandardCharsets.UTF_8
        );
        Set<String> rootServices = composeServices(Path.of("compose.yaml"));
        Set<String> sessionMemoryServices = composeServices(Path.of("worker/deploy/session-memory/compose.yaml"));
        Set<String> inventoryComposeServices = listedValues(inventory, "composeServices");

        assertThat(inventoryComposeServices).containsAll(rootServices);
        assertThat(inventoryComposeServices).containsAll(sessionMemoryServices);

        assertThat(inventory).contains("removed-legacy-external-memory-surface");
        assertThat(inventory).contains("dendrite");
        assertThat(inventory).contains("backupRestoreRequired: true");
        assertThat(inventory).contains("activation: profile-gated");
        assertThat(inventory).contains("enabledOnlyWhenSourceProfileEnabled: true");
    }

    @Test
    void workloadInventoryClassifiesScaleOutWithoutLeakingReplicaCounts() throws IOException {
        String inventory = Files.readString(
            CONTRACT_DIR.resolve("workload-inventory.yaml"),
            StandardCharsets.UTF_8
        );

        // Each k3s workload (one composeServices: block per workload; excludedWorkloads
        // carry none) must declare a scaleCategory and a replicaPolicy.
        long workloadBlocks = inventory.lines().filter(line -> line.strip().equals("composeServices:")).count();
        long scaleCategoryCount = inventory.lines().filter(line -> line.strip().startsWith("scaleCategory:")).count();
        long replicaPolicyCount = inventory.lines().filter(line -> line.strip().startsWith("replicaPolicy:")).count();
        assertThat(workloadBlocks).isEqualTo(14L);
        assertThat(scaleCategoryCount).isEqualTo(workloadBlocks);
        assertThat(replicaPolicyCount).isEqualTo(workloadBlocks);

        // Only the agreed category vocabulary is used (no competing-consumer: the worker
        // lane stays serialized until the WorkQueue/shared-store preconditions ship).
        assertThat(inventory).contains(
            "scaleCategory: horizontally-scalable",
            "scaleCategory: serialized-worker",
            "scaleCategory: singleton-stateful",
            "scaleCategory: not-a-target"
        );
        assertThat(inventory).doesNotContain("scaleCategory: competing-consumer");

        // Public artifacts carry policy labels, never production replica counts. The
        // canary Deployment uses a single-digit replicas: 1, so a 2+ digit count is a leak.
        assertThat(readPublicK3sArtifacts()).doesNotContainPattern("replicas:\\s*[0-9]{2,}");
    }

    @Test
    void listedValuesReadsOnlyTheRequestedListWithoutFixedIndent() {
        String yaml = String.join("\n",
            "workloads:",
            "  - id: api",
            "    composeServices:",
            "      - ingress-api",
            "    otherServices:",
            "      - not-compose",
            "  - id: worker",
            "    composeServices:",
            "        - ingress-worker-py",
            "nextTopLevel:",
            "  - not-compose-either"
        );

        assertThat(listedValues(yaml, "composeServices"))
            .containsExactly("ingress-api", "ingress-worker-py");
    }

    @Test
    void publicContractDelegatesSecretsAndHostSpecificOverlayToPrivateOpsRepo() throws IOException {
        String readme = Files.readString(Path.of("deploy/k3s/README.md"), StandardCharsets.UTF_8);
        String overlayContract = Files.readString(
            CONTRACT_DIR.resolve("ops-overlay-contract.yaml"),
            StandardCharsets.UTF_8
        );
        String allPublicK3sArtifacts = readPublicK3sArtifacts();

        assertThat(readme).contains("neurons-ops");
        assertThat(overlayContract).contains("privateRepo: neurons-ops");
        assertThat(overlayContract).contains("tailscale");
        assertThat(overlayContract).contains("backupRestore");
        assertThat(overlayContract).contains("networkPolicy");
        assertThat(overlayContract).contains("workQueueIsolation");
        assertThat(overlayContract).contains("maxCanaryWindowHours: 24");
        assertThat(allPublicK3sArtifacts).contains("kind: Namespace");
        assertThat(allPublicK3sArtifacts).contains("kind: ConfigMap");
        assertThat(allPublicK3sArtifacts).doesNotContain("kind: Secret");
        assertThat(allPublicK3sArtifacts).doesNotContain("/Users/");
        assertThat(allPublicK3sArtifacts).doesNotContain("/home/");
        assertThat(allPublicK3sArtifacts).doesNotContain("dataset_id:");
        assertThat(allPublicK3sArtifacts).doesNotContain("document_id:");
    }

    @Test
    void runbooksRequireDryRunBackupRestoreCanaryAndApprovalBeforeMutation() throws IOException {
        String backup = Files.readString(
            SPEC_DIR.resolve("backup-restore-rehearsal.md"),
            StandardCharsets.UTF_8
        );
        String canary = Files.readString(
            SPEC_DIR.resolve("canary-cutover-runbook.md"),
            StandardCharsets.UTF_8
        );

        assertThat(backup).contains("CouchDB");
        assertThat(backup).contains("Postgres ledger");
        assertThat(backup).contains("Neo4j");
        assertThat(backup).contains("Qdrant");
        assertThat(backup).contains("restore rehearsal");
        assertThat(backup).contains("redacted evidence");

        assertThat(canary).contains("client dry-run");
        assertThat(canary).contains("server dry-run");
        assertThat(canary).contains("explicit approval");
        assertThat(canary).contains("compose retire");
        assertThat(canary).contains("read/write canary");
        assertThat(canary).contains("public-safe synthetic");
        assertThat(canary).contains("shadow stream");
        assertThat(canary).contains("separate durable");
        assertThat(canary).contains("24h");
        assertThat(canary).contains("NetworkPolicy");
        assertThat(canary).contains("kube-apiserver");
    }

    @Test
    void singleGoalCutoverControlKeepsFullTransitionGateBounded() throws IOException {
        String control = Files.readString(
            SPEC_DIR.resolve("single-goal-cutover-control.md"),
            StandardCharsets.UTF_8
        );

        assertThat(control).contains("single agentic-execution goal");
        assertThat(control).contains("Gate 0");
        assertThat(control).contains("Gate 1");
        assertThat(control).contains("Gate 2");
        assertThat(control).contains("Gate 3");
        assertThat(control).contains("Gate 4");
        assertThat(control).contains("Gate 5");
        assertThat(control).contains("Gate 6");
        assertThat(control).contains("rollback");
        assertThat(control).contains("WorkQueue isolation");
        assertThat(control).contains("backup/restore rehearsal");
        assertThat(control).contains("read/write canary");
        assertThat(control).contains("compose retire");
        assertThat(control).contains("stop and ask");
        assertThat(control).contains("do not mark complete");
    }

    private String readPublicK3sArtifacts() throws IOException {
        StringBuilder output = new StringBuilder();
        for (Path path : List.of(
            Path.of("deploy/k3s/README.md"),
            CONTRACT_DIR.resolve("workload-inventory.yaml"),
            CONTRACT_DIR.resolve("ops-overlay-contract.yaml"),
            CONTRACT_DIR.resolve("base/namespace.yaml"),
            CONTRACT_DIR.resolve("base/config-contract.yaml"),
            CONTRACT_DIR.resolve("base/kustomization.yaml")
        )) {
            output.append(Files.readString(path, StandardCharsets.UTF_8)).append('\n');
        }
        return output.toString();
    }

    private Set<String> composeServices(Path path) throws IOException {
        Set<String> services = new LinkedHashSet<>();
        boolean inServices = false;
        Pattern servicePattern = Pattern.compile("^  ([A-Za-z0-9_-]+):\\s*$");
        for (String line : Files.readAllLines(path, StandardCharsets.UTF_8)) {
            if (line.equals("services:")) {
                inServices = true;
                continue;
            }
            if (inServices && !line.isBlank() && !line.startsWith(" ") && !line.startsWith("#")) {
                break;
            }
            Matcher matcher = servicePattern.matcher(line);
            if (inServices && matcher.matches()) {
                services.add(matcher.group(1));
            }
        }
        return services;
    }

    private Set<String> listedValues(String yaml, String listKey) {
        Set<String> values = new LinkedHashSet<>();
        boolean inList = false;
        int listKeyIndent = -1;
        for (String line : yaml.lines().toList()) {
            String stripped = line.strip();
            if (stripped.equals(listKey + ":")) {
                inList = true;
                listKeyIndent = leadingWhitespaceLength(line);
                continue;
            }
            if (!inList) {
                continue;
            }
            if (line.isBlank()) {
                continue;
            }
            int indent = leadingWhitespaceLength(line);
            if (indent <= listKeyIndent) {
                inList = false;
                continue;
            }
            if (stripped.startsWith("- ")) {
                values.add(stripped.substring(2).strip());
            }
        }
        return values;
    }

    private int leadingWhitespaceLength(String line) {
        int count = 0;
        while (count < line.length() && Character.isWhitespace(line.charAt(count))) {
            count++;
        }
        return count;
    }
}
