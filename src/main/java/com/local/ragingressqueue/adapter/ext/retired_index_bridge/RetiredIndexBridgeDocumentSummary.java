package com.local.ragingressqueue.adapter.ext.retired_index_bridge;

/**
 * A minimal view of a RetiredIndexBridge document returned by a list query: the backend document id and the
 * stored document name. Used to locate prior versions of a logical document for supersede.
 */
public record RetiredIndexBridgeDocumentSummary(String documentId, String name) {
}
