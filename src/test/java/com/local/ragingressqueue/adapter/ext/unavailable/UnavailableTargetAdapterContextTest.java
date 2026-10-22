package com.local.ragingressqueue.adapter.ext.unavailable;

import com.local.ragingressqueue.delivery.domain.DeliveryResult;
import com.local.ragingressqueue.delivery.domain.TargetPressure;
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
}
