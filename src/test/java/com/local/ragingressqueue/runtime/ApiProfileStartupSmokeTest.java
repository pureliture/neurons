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
import org.springframework.test.context.bean.override.convention.TestBean;

import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;

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
    @TestBean(name = "natsConnection", methodName = "fakeConnection", enforceOverride = true)
    Connection connection;

    @TestBean(name = "jetStreamManagement", methodName = "fakeJetStreamManagement", enforceOverride = true)
    JetStreamManagement jetStreamManagement;

    @TestBean(name = "jetStream", methodName = "fakeJetStream", enforceOverride = true)
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

    static Connection fakeConnection() {
        return noCallProxy(Connection.class);
    }

    static JetStreamManagement fakeJetStreamManagement() {
        return noCallProxy(JetStreamManagement.class);
    }

    static JetStream fakeJetStream() {
        return noCallProxy(JetStream.class);
    }

    private static <T> T noCallProxy(Class<T> type) {
        InvocationHandler handler = (proxy, method, args) -> {
            if (method.getDeclaringClass() == Object.class) {
                return objectMethodValue(proxy, method, args, type);
            }
            throw new AssertionError("Unexpected NATS call in api profile startup smoke test: " + method);
        };
        Object proxy = Proxy.newProxyInstance(type.getClassLoader(), new Class<?>[] {type}, handler);
        return type.cast(proxy);
    }

    private static Object objectMethodValue(Object proxy, Method method, Object[] args, Class<?> type) {
        return switch (method.getName()) {
            case "equals" -> proxy == args[0];
            case "hashCode" -> System.identityHashCode(proxy);
            case "toString" -> "fake-" + type.getSimpleName();
            default -> throw new AssertionError("Unexpected Object method: " + method);
        };
    }
}
