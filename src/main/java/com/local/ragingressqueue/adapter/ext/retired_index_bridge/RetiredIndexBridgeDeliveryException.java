package com.local.ragingressqueue.adapter.ext.retired_index_bridge;

public class RetiredIndexBridgeDeliveryException extends RuntimeException {
    public RetiredIndexBridgeDeliveryException(String message) {
        super(message);
    }

    public RetiredIndexBridgeDeliveryException(String message, Throwable cause) {
        super(message, cause);
    }
}
