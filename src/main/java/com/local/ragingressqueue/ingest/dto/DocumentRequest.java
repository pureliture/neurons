package com.local.ragingressqueue.ingest.dto;

import java.util.Map;

public record DocumentRequest(
    String filename,
    String contentType,
    String body,
    Map<String, String> metadata
) {
}
