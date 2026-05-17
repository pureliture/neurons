package com.local.ragingressqueue.queue;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.local.ragingressqueue.core.IngestJob;

import java.io.IOException;

public class IngestJobMessageCodec {
    private final ObjectMapper objectMapper;

    public IngestJobMessageCodec() {
        this(new ObjectMapper());
    }

    public IngestJobMessageCodec(ObjectMapper objectMapper) {
        this.objectMapper = objectMapper;
    }

    public byte[] encode(IngestJob job) {
        try {
            return objectMapper.writeValueAsBytes(job);
        } catch (JsonProcessingException error) {
            throw new IllegalArgumentException("failed to encode ingest job", error);
        }
    }

    public IngestJob decode(byte[] payload) {
        try {
            return objectMapper.readValue(payload, IngestJob.class);
        } catch (IOException error) {
            throw new IllegalArgumentException("failed to decode ingest job", error);
        }
    }
}
