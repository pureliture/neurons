package com.local.ragingressqueue.target;

class RagFlowDeliveryException extends RuntimeException {
    RagFlowDeliveryException(String message) {
        super(message);
    }

    RagFlowDeliveryException(String message, Throwable cause) {
        super(message, cause);
    }
}
