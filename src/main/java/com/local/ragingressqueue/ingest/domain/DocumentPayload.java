package com.local.ragingressqueue.ingest.domain;

import java.util.Map;

public record DocumentPayload(
    String kind,
    String redactionVersion,
    String filename,
    String contentType,
    String body,
    Map<String, String> metadata
) {
    public DocumentPayload withKind(String newKind) {
        return new DocumentPayload(newKind, redactionVersion, filename, contentType, body, metadata);
    }

    @Override
    public String toString() {
        return "DocumentPayload[kind=<redacted>, redactionVersion=<redacted>, filename=<redacted>, contentType=<redacted>, body=<redacted>, metadata=<redacted>]";
    }
}
