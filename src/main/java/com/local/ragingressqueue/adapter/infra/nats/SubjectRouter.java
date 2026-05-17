package com.local.ragingressqueue.adapter.infra.nats;

import java.util.Map;

public class SubjectRouter {
    private static final Map<String, String> SUBJECTS_BY_KIND = Map.of(
        "conversation_chunk", "rag.ingress.transcript",
        "session_summary", "rag.ingress.document",
        "task_summary", "rag.ingress.document",
        "approved_memory_card", "rag.ingress.document"
    );

    public String subjectFor(String kind) {
        String subject = SUBJECTS_BY_KIND.get(kind);
        if (subject == null) {
            throw new IllegalArgumentException("unknown document kind: " + kind);
        }
        return subject;
    }
}
