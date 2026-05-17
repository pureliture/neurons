package com.local.ragingressqueue.adapter.ext.ragflow;

public class RagFlowDeliveryException extends RuntimeException {
    public RagFlowDeliveryException(String message) {
        super(message);
    }

    public RagFlowDeliveryException(String message, Throwable cause) {
        super(message, cause);
    }
}
