package com.local.ragingressqueue.target.port;

/**
 * Logical index backend kind that a targetProfile routes to.
 *
 * <p>Search index adapters live behind this enum; a new backend adds a value here plus a matching adapter
 * implementation, without touching the public enqueue contract.</p>
 */
public enum BackendKind {
    RETIRED_INDEX_BRIDGE
}
