package com.local.ragingressqueue.ingest.service;

import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class IdempotencyStoreTest {
    @Test
    void replayWithSameKeyAndContentHashDoesNotConflict() {
        IdempotencyStore store = new IdempotencyStore();

        assertThat(store.conflicts("key-1", "sha256:aaa")).isFalse();
        assertThat(store.conflicts("key-1", "sha256:aaa")).isFalse();
    }

    @Test
    void sameKeyWithDifferentContentHashConflicts() {
        IdempotencyStore store = new IdempotencyStore();

        assertThat(store.conflicts("key-1", "sha256:aaa")).isFalse();
        assertThat(store.conflicts("key-1", "sha256:bbb")).isTrue();
    }

    @Test
    void blankOrNullKeyNeverConflicts() {
        IdempotencyStore store = new IdempotencyStore();

        assertThat(store.conflicts(null, "sha256:aaa")).isFalse();
        assertThat(store.conflicts("   ", "sha256:aaa")).isFalse();
        assertThat(store.conflicts("", "sha256:bbb")).isFalse();
    }
}
