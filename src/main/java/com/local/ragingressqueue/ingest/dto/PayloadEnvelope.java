package com.local.ragingressqueue.ingest.dto;

public record PayloadEnvelope(
    String kind,
    String redactionVersion,
    DocumentRequest document
) {
}
