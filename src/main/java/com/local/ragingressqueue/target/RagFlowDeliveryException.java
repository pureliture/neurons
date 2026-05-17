package com.local.ragingressqueue.target;

public class RagFlowDeliveryException extends RuntimeException {
    public RagFlowDeliveryException(String message) {
        super(message);
    }

    public RagFlowDeliveryException(String message, Throwable cause) {
        super(message, cause);
    }
}
