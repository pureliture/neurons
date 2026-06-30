package com.local.ragingressqueue.adapter.ext.unavailable;

import com.local.ragingressqueue.common.IngestStatus;
import com.local.ragingressqueue.delivery.domain.DeliveryResult;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.target.port.RagTargetAdapter;
import com.local.ragingressqueue.target.port.TargetPressureSnapshot;
import com.local.ragingressqueue.target.port.TargetStatusSnapshot;

public class UnavailableTargetAdapter implements RagTargetAdapter {
    private static final String NOT_CONFIGURED = "not_configured";

    @Override
    public TargetPressureSnapshot pressureSnapshot(String targetProfile) {
        return TargetPressureSnapshot.closed(NOT_CONFIGURED);
    }

    @Override
    public DeliveryResult deliver(IngestJob job, String targetProfile) {
        return DeliveryResult.failed("target adapter not configured");
    }

    @Override
    public TargetStatusSnapshot getStatus(IngestJob job, String targetProfile) {
        return new TargetStatusSnapshot(
            job.contentHashPrefix(),
            job.contentHash(),
            targetProfile,
            IngestStatus.FAILED,
            "redacted"
        );
    }
}
