package com.local.ragingressqueue.adapter.ext.ragflow;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;

import static org.assertj.core.api.Assertions.assertThat;

class RecentDeliveryCacheTest {
    @Test
    void recordedFragmentIsSeenWithinTtl() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.record("ds_1", "48daba68a6f6");

        assertThat(cache.seen("ds_1", "48daba68a6f6")).isTrue();
    }

    @Test
    void fragmentIsScopedPerDataset() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.record("ds_1", "48daba68a6f6");

        assertThat(cache.seen("ds_other", "48daba68a6f6")).isFalse();
    }

    @Test
    void entryExpiresAfterTtl() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.record("ds_1", "48daba68a6f6");
        clock.advance(Duration.ofMinutes(10).plusSeconds(1));

        assertThat(cache.seen("ds_1", "48daba68a6f6")).isFalse();
    }

    @Test
    void blankFragmentIsNeverSeenAndNeverRecorded() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.record("ds_1", "");

        assertThat(cache.seen("ds_1", "")).isFalse();
    }

    @Test
    void oldestEntriesAreEvictedWhenMaxSizeExceeded() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 2, clock);

        cache.record("ds_1", "aaaaaaaaaaaa");
        clock.advance(Duration.ofSeconds(1));
        cache.record("ds_1", "bbbbbbbbbbbb");
        clock.advance(Duration.ofSeconds(1));
        cache.record("ds_1", "cccccccccccc");

        assertThat(cache.seen("ds_1", "aaaaaaaaaaaa")).isFalse();
        assertThat(cache.seen("ds_1", "bbbbbbbbbbbb")).isTrue();
        assertThat(cache.seen("ds_1", "cccccccccccc")).isTrue();
    }

    private static final class MutableClock extends java.time.Clock {
        private Instant now;

        private MutableClock(Instant start) {
            this.now = start;
        }

        private void advance(Duration delta) {
            now = now.plus(delta);
        }

        @Override
        public Instant instant() {
            return now;
        }

        @Override
        public java.time.ZoneId getZone() {
            return ZoneOffset.UTC;
        }

        @Override
        public java.time.Clock withZone(java.time.ZoneId zone) {
            return this;
        }
    }
}
