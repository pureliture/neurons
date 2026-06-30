package com.local.ragingressqueue.runtime;

import com.local.ragingressqueue.RagIngressQueueApplication;
import com.local.ragingressqueue.adapter.ext.unavailable.UnavailableTargetAdapter;
import com.local.ragingressqueue.ingest.api.IngressController;
import com.local.ragingressqueue.target.port.RagTargetAdapter;
import io.nats.client.Connection;
import io.nats.client.JetStream;
import io.nats.client.JetStreamManagement;
import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.bean.override.mockito.MockitoBean;

import static org.assertj.core.api.Assertions.assertThat;

@SpringBootTest(
    classes = RagIngressQueueApplication.class,
    properties = {
        "spring.profiles.active=api",
        "spring.main.web-application-type=none",
        "rag-ingress.nats.provision-on-startup=false"
    }
)
class ApiProfileStartupSmokeTest {
    @MockitoBean
    Connection connection;

    @MockitoBean
    JetStreamManagement jetStreamManagement;

    @MockitoBean
    JetStream jetStream;

    @Autowired
    IngressController controller;

    @Autowired
    RagTargetAdapter targetAdapter;

    @Test
    void apiProfileStartsWithUnavailableTargetAdapterAndHealthzStaysIndependent() {
        assertThat(targetAdapter).isInstanceOf(UnavailableTargetAdapter.class);
        assertThat(controller.healthz()).containsEntry("status", "ok");
    }
}
