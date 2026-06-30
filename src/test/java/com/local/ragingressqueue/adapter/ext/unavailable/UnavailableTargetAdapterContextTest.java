package com.local.ragingressqueue.adapter.ext.unavailable;

import com.local.ragingressqueue.adapter.ext.retired_index_bridge.RetiredIndexBridgeTargetAdapter;
import com.local.ragingressqueue.adapter.ext.retired_index_bridge.RetiredIndexBridgeDocumentRef;
import com.local.ragingressqueue.adapter.ext.retired_index_bridge.RetiredIndexBridgeDocumentSummary;
import com.local.ragingressqueue.adapter.ext.retired_index_bridge.RetiredIndexBridgeGateway;
import com.local.ragingressqueue.adapter.ext.retired_index_bridge.RetiredIndexBridgePressureSnapshot;
import com.local.ragingressqueue.delivery.domain.DeliveryResult;
import com.local.ragingressqueue.delivery.domain.TargetPressure;
import com.local.ragingressqueue.ingest.domain.DocumentPayload;
import com.local.ragingressqueue.ingest.domain.IngestJob;
import com.local.ragingressqueue.queue.port.QueueStatusProvider;
import com.local.ragingressqueue.queue.port.QueueStatusSnapshot;
import com.local.ragingressqueue.status.service.StatusService;
import com.local.ragingressqueue.target.port.RagTargetAdapter;
import com.local.ragingressqueue.target.port.TargetPressureSnapshot;
import com.local.ragingressqueue.target.port.TargetStatusSnapshot;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.runner.ApplicationContextRunner;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Import;

import java.util.Collection;
import java.util.List;
import java.util.Map;

import static org.assertj.core.api.Assertions.assertThat;

class UnavailableTargetAdapterContextTest {
    private final ApplicationContextRunner apiStatusContextRunner = new ApplicationContextRunner()
        .withInitializer(context -> context.getEnvironment().setActiveProfiles("api"))
        .withUserConfiguration(ApiStatusContext.class);

    @Test
    void apiProfileStatusContextLoadsWithFallbackTargetAdapter() {
        apiStatusContextRunner.run(context -> {
            assertThat(context).hasSingleBean(StatusService.class);
            assertThat(context).hasSingleBean(RagTargetAdapter.class);
            assertThat(context).hasSingleBean(UnavailableTargetAdapter.class);

            Map<String, Object> status = context.getBean(StatusService.class).currentStatus();

            assertThat(status).containsEntry("externalStatus", "not_configured");
            assertThat(status.get("target")).isEqualTo(Map.of(
                "name", "retired_index_bridge",
                "pressure", "CLOSED",
                "running", 0,
                "unstart", 0,
                "sampled", 0,
                "reason", "not_configured"
            ));
        });
    }

    @Test
    void apiProfileStatusContextPrefersConfiguredTargetAdapter() {
        apiStatusContextRunner
            .withBean(RagTargetAdapter.class, FakeRagTargetAdapter::new)
            .run(context -> {
                assertThat(context).hasSingleBean(StatusService.class);
                assertThat(context).hasSingleBean(RagTargetAdapter.class);
                assertThat(context).doesNotHaveBean(UnavailableTargetAdapter.class);

                Map<String, Object> status = context.getBean(StatusService.class).currentStatus();

                assertThat(status).containsEntry("externalStatus", "configured");
                assertThat(status.get("target")).isEqualTo(Map.of(
                    "name", "retired_index_bridge",
                    "pressure", "OPEN",
                    "running", 0,
                    "unstart", 0,
                    "sampled", 100
                ));
            });
    }

    @Test
    void workerProfileDoesNotRegisterFallbackTargetAdapter() {
        new ApplicationContextRunner()
            .withInitializer(context -> context.getEnvironment().setActiveProfiles("api", "worker"))
            .withUserConfiguration(FallbackOnlyContext.class)
            .run(context -> assertThat(context).doesNotHaveBean(UnavailableTargetAdapter.class));
    }

    @Test
    void retiredIndexBridgeProfileDoesNotRegisterFallbackTargetAdapter() {
        new ApplicationContextRunner()
            .withInitializer(context -> context.getEnvironment().setActiveProfiles("api", "retired-index-bridge"))
            .withUserConfiguration(FallbackOnlyContext.class)
            .run(context -> assertThat(context).doesNotHaveBean(UnavailableTargetAdapter.class));
    }

    @Test
    void retiredIndexBridgeProfileUsesRealTargetAdapter() {
        new ApplicationContextRunner()
            .withInitializer(context -> context.getEnvironment().setActiveProfiles("api", "retired-index-bridge"))
            .withUserConfiguration(RetiredIndexBridgeContext.class)
            .run(context -> {
                assertThat(context).hasSingleBean(RagTargetAdapter.class);
                assertThat(context).hasSingleBean(RetiredIndexBridgeTargetAdapter.class);
                assertThat(context).doesNotHaveBean(UnavailableTargetAdapter.class);
            });
    }

    @Configuration(proxyBeanMethods = false)
    @Import({StatusService.class, UnavailableTargetAdapterConfiguration.class})
    static class ApiStatusContext {
        @Bean
        QueueStatusProvider queueStatusProvider() {
            return QueueStatusSnapshot::unavailable;
        }
    }

    @Configuration(proxyBeanMethods = false)
    @Import(UnavailableTargetAdapterConfiguration.class)
    static class FallbackOnlyContext {
    }

    @Configuration(proxyBeanMethods = false)
    @Import({UnavailableTargetAdapterConfiguration.class, RetiredIndexBridgeTargetAdapter.class})
    static class RetiredIndexBridgeContext {
        @Bean
        RetiredIndexBridgeGateway retiredIndexBridgeGateway() {
            return new FakeRetiredIndexBridgeGateway();
        }
    }

    private static final class FakeRagTargetAdapter implements RagTargetAdapter {
        @Override
        public TargetPressureSnapshot pressureSnapshot(String targetProfile) {
            return new TargetPressureSnapshot(TargetPressure.OPEN, 0, 0, 100, null);
        }

        @Override
        public DeliveryResult deliver(IngestJob job, String targetProfile) {
            throw new UnsupportedOperationException("not used");
        }

        @Override
        public TargetStatusSnapshot getStatus(IngestJob job, String targetProfile) {
            throw new UnsupportedOperationException("not used");
        }
    }

    private static final class FakeRetiredIndexBridgeGateway implements RetiredIndexBridgeGateway {
        @Override
        public RetiredIndexBridgeDocumentRef uploadDocument(
            String baseUrl,
            String apiKey,
            String datasetId,
            DocumentPayload payload
        ) {
            throw new UnsupportedOperationException("not used");
        }

        @Override
        public void updateMetadata(String baseUrl, String apiKey, String datasetId, String documentId, Map<String, String> metadata) {
            throw new UnsupportedOperationException("not used");
        }

        @Override
        public void requestParse(String baseUrl, String apiKey, String datasetId, String documentId) {
            throw new UnsupportedOperationException("not used");
        }

        @Override
        public RetiredIndexBridgePressureSnapshot pressureSnapshot(String baseUrl, String apiKey, String datasetId) {
            throw new UnsupportedOperationException("not used");
        }

        @Override
        public boolean findByContentHash(String baseUrl, String apiKey, String datasetId, String contentHashFragment) {
            throw new UnsupportedOperationException("not used");
        }

        @Override
        public List<RetiredIndexBridgeDocumentSummary> listDocumentsByKeyword(
            String baseUrl,
            String apiKey,
            String datasetId,
            String keyword
        ) {
            throw new UnsupportedOperationException("not used");
        }

        @Override
        public void deleteDocuments(String baseUrl, String apiKey, String datasetId, Collection<String> documentIds) {
            throw new UnsupportedOperationException("not used");
        }
    }
}
