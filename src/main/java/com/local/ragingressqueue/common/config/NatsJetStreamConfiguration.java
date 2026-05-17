package com.local.ragingressqueue.common.config;
import com.local.ragingressqueue.adapter.infra.nats.SubjectRouter;
import com.local.ragingressqueue.adapter.infra.nats.IngestJobMessageCodec;

import io.nats.client.Connection;
import io.nats.client.JetStream;
import io.nats.client.JetStreamManagement;
import io.nats.client.Nats;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;

import java.io.IOException;

@Configuration
@Profile({"api", "worker"})
public class NatsJetStreamConfiguration {
    @Bean(destroyMethod = "close")
    Connection natsConnection(@Value("${rag-ingress.nats.url}") String natsUrl) throws IOException, InterruptedException {
        return Nats.connect(natsUrl);
    }

    @Bean
    JetStreamManagement jetStreamManagement(Connection connection) throws IOException {
        return connection.jetStreamManagement();
    }

    @Bean
    JetStream jetStream(JetStreamManagement jetStreamManagement) {
        return jetStreamManagement.jetStream();
    }

    @Bean
    SubjectRouter subjectRouter() {
        return new SubjectRouter();
    }

    @Bean
    IngestJobMessageCodec ingestJobMessageCodec() {
        return new IngestJobMessageCodec();
    }
}
