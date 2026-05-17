package com.local.ragingressqueue;

import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;

@SpringBootTest(properties = {
    "spring.profiles.active=test",
    "spring.main.web-application-type=none"
})
class RagIngressQueueApplicationTests {
    @Test
    void contextLoads() {
    }
}
