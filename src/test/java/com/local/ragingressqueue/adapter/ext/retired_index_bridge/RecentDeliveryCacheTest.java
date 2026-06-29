package com.local.ragingressqueue.adapter.ext.retired_index_bridge;

import org.junit.jupiter.api.Test;

import java.time.Duration;
import java.time.Instant;
import java.time.ZoneOffset;

import static org.assertj.core.api.Assertions.assertThat;

class RecentDeliveryCacheTest {
    @Test
    void finalizedFragmentIsLookedUpWithinTtl() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.markFinalized("ds_1", "48daba68a6f6", "doc_1");

        RecentDeliveryCache.Entry entry = cache.lookup("ds_1", "48daba68a6f6");
        assertThat(entry).isNotNull();
        assertThat(entry.stage()).isEqualTo(RecentDeliveryCache.Stage.FINALIZED);
        assertThat(entry.documentId()).isEqualTo("doc_1");
    }

    @Test
    void uploadedEntryIsNotFinalizedAndCarriesDocumentId() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.recordUploaded("ds_1", "48daba68a6f6", "doc_42");

        RecentDeliveryCache.Entry entry = cache.lookup("ds_1", "48daba68a6f6");
        assertThat(entry).isNotNull();
        assertThat(entry.stage()).isEqualTo(RecentDeliveryCache.Stage.UPLOADED);
        assertThat(entry.documentId()).isEqualTo("doc_42");
    }

    @Test
    void markMetadataDoneAdvancesStageWhileKeepingDocumentId() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.recordUploaded("ds_1", "48daba68a6f6", "doc_9");
        cache.markMetadataDone("ds_1", "48daba68a6f6", "doc_9");

        RecentDeliveryCache.Entry entry = cache.lookup("ds_1", "48daba68a6f6");
        assertThat(entry.stage()).isEqualTo(RecentDeliveryCache.Stage.METADATA_DONE);
        assertThat(entry.documentId()).isEqualTo("doc_9");
    }

    @Test
    void finalizingAnUploadedEntryFlipsItToFinalized() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.recordUploaded("ds_1", "48daba68a6f6", "doc_7");
        cache.markFinalized("ds_1", "48daba68a6f6", "doc_7");

        assertThat(cache.lookup("ds_1", "48daba68a6f6").stage())
            .isEqualTo(RecentDeliveryCache.Stage.FINALIZED);
    }

    @Test
    void finalizingWithoutADocumentIdPreservesAPreviouslyRecordedOne() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.recordUploaded("ds_1", "48daba68a6f6", "doc_5");
        cache.markFinalized("ds_1", "48daba68a6f6", null);

        assertThat(cache.lookup("ds_1", "48daba68a6f6").documentId()).isEqualTo("doc_5");
    }

    @Test
    void entryIsLiveAtTheExactTtlBoundaryAndExpiresJustAfter() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.markFinalized("ds_1", "48daba68a6f6", "doc_1");

        clock.advance(Duration.ofMinutes(10));
        assertThat(cache.lookup("ds_1", "48daba68a6f6")).isNotNull();

        clock.advance(Duration.ofMillis(1));
        assertThat(cache.lookup("ds_1", "48daba68a6f6")).isNull();
    }

    @Test
    void fragmentIsScopedPerDataset() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.markFinalized("ds_1", "48daba68a6f6", "doc_1");

        assertThat(cache.lookup("ds_other", "48daba68a6f6")).isNull();
    }

    @Test
    void entryExpiresAfterTtl() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.markFinalized("ds_1", "48daba68a6f6", "doc_1");
        clock.advance(Duration.ofMinutes(10).plusSeconds(1));

        assertThat(cache.lookup("ds_1", "48daba68a6f6")).isNull();
    }

    @Test
    void blankFragmentIsNeverLookedUpAndNeverRecorded() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 100, clock);

        cache.recordUploaded("ds_1", "", "doc_1");
        cache.markFinalized("ds_1", "", "doc_1");

        assertThat(cache.lookup("ds_1", "")).isNull();
    }

    @Test
    void oldestEntriesAreEvictedWhenMaxSizeExceeded() {
        MutableClock clock = new MutableClock(Instant.parse("2026-06-04T00:00:00Z"));
        RecentDeliveryCache cache = new RecentDeliveryCache(Duration.ofMinutes(10), 2, clock);

        cache.markFinalized("ds_1", "aaaaaaaaaaaa", "doc_a");
        clock.advance(Duration.ofSeconds(1));
        cache.markFinalized("ds_1", "bbbbbbbbbbbb", "doc_b");
        clock.advance(Duration.ofSeconds(1));
        cache.markFinalized("ds_1", "cccccccccccc", "doc_c");

        assertThat(cache.lookup("ds_1", "aaaaaaaaaaaa")).isNull();
        assertThat(cache.lookup("ds_1", "bbbbbbbbbbbb")).isNotNull();
        assertThat(cache.lookup("ds_1", "cccccccccccc")).isNotNull();
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
