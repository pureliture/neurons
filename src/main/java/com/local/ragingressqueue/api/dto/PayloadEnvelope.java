package com.local.ragingressqueue.api.dto;

public record PayloadEnvelope(
    String kind,
    String redactionVersion,
    DocumentRequest document
) {
}
