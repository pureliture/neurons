package com.local.ragingressqueue.api;

import com.local.ragingressqueue.api.dto.EnqueueRequest;
import com.local.ragingressqueue.api.dto.EnqueueResponse;
import com.local.ragingressqueue.core.IngestJob;
import com.local.ragingressqueue.core.validation.IngestJobValidator;
import com.local.ragingressqueue.core.validation.RedactionGuard;
import com.local.ragingressqueue.queue.IngestPublisher;
import com.local.ragingressqueue.queue.PublishResult;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.context.annotation.Profile;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RestController;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

@RestController
@Profile("api")
public class IngressController {
    private static final String SCHEMA_VERSION = "rag_ingress_enqueue.v1";
    private static final String RESERVED_REF_KIND = "redacted_document_ref";

    private final IngestPublisher publisher;
    private final IngestJobValidator validator;
    private final RedactionGuard redactionGuard;
    private final IdempotencyStore idempotencyStore;
    private final StatusService statusService;

    @Autowired
    public IngressController(IngestPublisher publisher, StatusService statusService) {
        this(publisher, statusService, new IngestJobValidator(), new RedactionGuard(), new IdempotencyStore());
    }

    IngressController(
        IngestPublisher publisher,
        StatusService statusService,
        IngestJobValidator validator,
        RedactionGuard redactionGuard,
        IdempotencyStore idempotencyStore
    ) {
        this.publisher = publisher;
        this.validator = validator;
        this.redactionGuard = redactionGuard;
        this.idempotencyStore = idempotencyStore;
        this.statusService = statusService;
    }

    public static IngressController createForTests(IngestPublisher publisher) {
        return new IngressController(publisher, new StatusService());
    }

    @PostMapping("/v1/ingest/enqueue")
    public ResponseEntity<EnqueueResponse> enqueue(@RequestBody EnqueueRequest request) {
        if (request == null || !SCHEMA_VERSION.equals(request.schemaVersion())) {
            return badRequest(List.of("schemaVersion must be rag_ingress_enqueue.v1"));
        }
        if (request.payload() != null && RESERVED_REF_KIND.equals(request.payload().kind())) {
            return ResponseEntity.status(422)
                .body(EnqueueResponse.rejected("unsupported_payload", List.of("redacted_document_ref is reserved but disabled")));
        }
        IngestJob job = request.toIngestJob();
        List<String> violations = new ArrayList<>(validator.validate(job));
        violations.addAll(redactionGuard.inspectJob(job));
        if (!violations.isEmpty()) {
            return badRequest(List.of("request rejected"));
        }
        if (idempotencyStore.conflicts(request.idempotencyKey(), request.contentHash())) {
            return ResponseEntity.status(409)
                .body(EnqueueResponse.rejected("idempotency_conflict", List.of("idempotencyKey conflict")));
        }

        PublishResult result = publisher.publish(job);
        if (!result.accepted()) {
            return ResponseEntity.status(503)
                .body(EnqueueResponse.rejected("publish_failed", List.of("publish ack not received")));
        }
        return ResponseEntity.status(202).body(EnqueueResponse.queued(result.jobId()));
    }

    @GetMapping("/healthz")
    public Map<String, String> healthz() {
        return Map.of("status", "ok", "component", "ingress-api");
    }

    @GetMapping("/status")
    public Map<String, Object> status() {
        return statusService.currentStatus();
    }

    private ResponseEntity<EnqueueResponse> badRequest(List<String> errors) {
        return ResponseEntity.badRequest().body(EnqueueResponse.rejected("rejected", errors));
    }
}
