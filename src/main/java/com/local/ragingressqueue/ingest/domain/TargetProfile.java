package com.local.ragingressqueue.ingest.domain;

import com.local.ragingressqueue.target.port.BackendKind;

/**
 * Logical routing entry: a {@code targetProfile} id maps to a backend kind and a logical dataset
 * role. The physical backend resource id (e.g. a RetiredIndexBridge dataset id) is intentionally NOT carried
 * here — it stays private to the backend adapter/config and must never reach a public surface.
 */
public record TargetProfile(String id, BackendKind backendKind, String datasetRole) {
}
