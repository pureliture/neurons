package com.local.ragingressqueue.target;

import com.local.ragingressqueue.core.IngestJob;
import com.local.ragingressqueue.core.TargetIndexingState;
import com.local.ragingressqueue.core.TargetPressure;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

@Component
@Profile("worker")
public class RagFlowTargetAdapter implements RagTargetAdapter {
    private final boolean deliveryEnabled;

    public RagFlowTargetAdapter(@Value("${rag-ingress.target.ragflow.delivery-enabled:false}") boolean deliveryEnabled) {
        this.deliveryEnabled = deliveryEnabled;
    }

    @Override
    public TargetPressure checkPressure(String targetProfile) {
        if (!deliveryEnabled) {
            return TargetPressure.CLOSED;
        }
        return TargetPressure.OPEN;
    }

    @Override
    public DeliveryResult deliver(IngestJob job, String targetProfile) {
        if (!deliveryEnabled) {
            return DeliveryResult.failed("ragflow delivery disabled");
        }
        return DeliveryResult.failed("ragflow delivery adapter is not configured");
    }

    @Override
    public TargetStatusSnapshot getStatus(IngestJob job, String targetProfile) {
        TargetIndexingState state = deliveryEnabled ? TargetIndexingState.ACCEPTED : TargetIndexingState.FAILED;
        return new TargetStatusSnapshot(
            job.contentHashPrefix(),
            job.contentHash(),
            targetProfile,
            state,
            "redacted"
        );
    }
}
