package com.local.ragingressqueue.core.validation;

import com.local.ragingressqueue.core.DocumentPayload;
import com.local.ragingressqueue.core.IngestJob;

import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStream;
import java.io.UncheckedIOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.regex.Pattern;

public class RedactionGuard {
    private static final List<NamedPattern> DENYLIST = loadDenylist();

    public List<String> inspect(String body) {
        List<String> violations = inspectValue("body", body);
        if (body == null || body.isBlank()) {
            violations.add("body is required");
            return violations;
        }
        if (!body.contains("schema_version:")) {
            violations.add("missing schema_version frontmatter");
        }
        if (!body.contains("result_type:")) {
            violations.add("missing result_type frontmatter");
        }
        return violations;
    }

    public List<String> inspectJob(IngestJob job) {
        List<String> violations = new ArrayList<>();
        if (job == null) {
            violations.add("job is required");
            return violations;
        }
        inspectMap("source", job.source(), violations);
        violations.addAll(inspectValue("contentHash", job.contentHash()));
        violations.addAll(inspectValue("targetProfile", job.targetProfile()));
        violations.addAll(inspectValue("kind", job.kind()));
        violations.addAll(inspectValue("idempotencyKey", job.idempotencyKey()));
        DocumentPayload payload = job.payload();
        if (payload != null) {
            violations.addAll(inspectValue("payload.kind", payload.kind()));
            violations.addAll(inspectValue("payload.redactionVersion", payload.redactionVersion()));
            violations.addAll(inspectValue("payload.document.filename", payload.filename()));
            violations.addAll(inspectValue("payload.document.contentType", payload.contentType()));
            violations.addAll(inspect(payload.body()));
            inspectMap("payload.document.metadata", payload.metadata(), violations);
        }
        return violations;
    }

    public List<String> inspectValue(String field, String value) {
        List<String> violations = new ArrayList<>();
        if (value == null) {
            return violations;
        }
        for (NamedPattern denied : DENYLIST) {
            if (denied.pattern().matcher(value).find()) {
                violations.add("forbidden pattern in " + field + ": " + denied.name());
            }
        }
        return violations;
    }

    private void inspectMap(String field, Map<String, String> values, List<String> violations) {
        if (values == null) {
            return;
        }
        values.forEach((key, value) -> {
            violations.addAll(inspectValue(field + "." + key, key));
            violations.addAll(inspectValue(field + "." + key, value));
        });
    }

    private static List<NamedPattern> loadDenylist() {
        String resourceName = "redaction-denylist.txt";
        InputStream resource = RedactionGuard.class.getClassLoader().getResourceAsStream(resourceName);
        if (resource != null) {
            try (BufferedReader reader = new BufferedReader(new java.io.InputStreamReader(resource, StandardCharsets.UTF_8))) {
                return compileDenylist(reader.lines().toList());
            } catch (IOException error) {
                throw new UncheckedIOException("failed to load redaction denylist resource: " + resourceName, error);
            }
        }
        Path path = Path.of("scripts/redaction-denylist.txt");
        try {
            return compileDenylist(Files.readAllLines(path, StandardCharsets.UTF_8));
        } catch (IOException error) {
            throw new UncheckedIOException("failed to load redaction denylist: " + path, error);
        }
    }

    private static List<NamedPattern> compileDenylist(List<String> lines) {
        return lines.stream()
            .map(String::trim)
            .filter(line -> !line.isEmpty())
            .filter(line -> !line.startsWith("#"))
            .map(line -> new NamedPattern(line, Pattern.compile(line, Pattern.CASE_INSENSITIVE)))
            .toList();
    }

    private record NamedPattern(String name, Pattern pattern) {
    }
}
