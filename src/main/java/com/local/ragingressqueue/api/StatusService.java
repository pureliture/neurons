package com.local.ragingressqueue.api;

import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Service;

import java.util.Map;

@Service
@Profile("api")
public class StatusService {
    public Map<String, Object> currentStatus() {
        return Map.of(
            "queue", Map.of("pending", 0, "inFlight", 0, "redelivered", 0, "deadLetter", 0),
            "target", Map.of("name", "ragflow", "pressure", "CLOSED"),
            "documentStatus", Map.of("indexedCandidateCount", 0),
            "authorization", Map.of("authorizedCount", 0),
            "externalStatus", "not_configured"
        );
    }
}
