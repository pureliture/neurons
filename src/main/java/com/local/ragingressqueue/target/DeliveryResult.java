package com.local.ragingressqueue.target;

public record DeliveryResult(boolean delivered, String targetRef, String error) {
    public static DeliveryResult delivered(String targetRef) {
        return new DeliveryResult(true, targetRef, null);
    }

    public static DeliveryResult failed(String error) {
        return new DeliveryResult(false, null, error);
    }
}
