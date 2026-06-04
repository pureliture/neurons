package com.local.ragingressqueue.adapter.ext.ragflow;

/**
 * A minimal view of a RAGFlow document returned by a list query: the backend document id and the
 * stored document name. Used to locate prior versions of a logical document for supersede.
 */
public record RagFlowDocumentSummary(String documentId, String name) {
}
