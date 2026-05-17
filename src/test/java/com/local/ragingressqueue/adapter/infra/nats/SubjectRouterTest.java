package com.local.ragingressqueue.adapter.infra.nats;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;
import static org.assertj.core.api.Assertions.assertThatThrownBy;

class SubjectRouterTest {
    private final SubjectRouter router = new SubjectRouter();

    @Test
    void routesConversationChunkToTranscriptSubject() {
        assertThat(router.subjectFor("conversation_chunk")).isEqualTo("rag.ingress.transcript");
    }

    @Test
    void routesDerivedDocumentsToDocumentSubject() {
        assertThat(router.subjectFor("session_summary")).isEqualTo("rag.ingress.document");
        assertThat(router.subjectFor("task_summary")).isEqualTo("rag.ingress.document");
        assertThat(router.subjectFor("approved_memory_card")).isEqualTo("rag.ingress.document");
    }

    @Test
    void rejectsUnknownKind() {
        assertThatThrownBy(() -> router.subjectFor("unknown"))
            .isInstanceOf(IllegalArgumentException.class)
            .hasMessageContaining("unknown document kind");
    }
}
