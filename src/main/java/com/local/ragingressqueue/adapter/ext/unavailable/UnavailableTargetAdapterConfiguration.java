package com.local.ragingressqueue.adapter.ext.unavailable;

import com.local.ragingressqueue.target.port.RagTargetAdapter;
import org.springframework.boot.autoconfigure.condition.ConditionalOnMissingBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;

@Configuration(proxyBeanMethods = false)
@Profile("api & !worker & !retired-index-bridge")
public class UnavailableTargetAdapterConfiguration {
    @Bean
    @ConditionalOnMissingBean(RagTargetAdapter.class)
    RagTargetAdapter unavailableTargetAdapter() {
        return new UnavailableTargetAdapter();
    }
}
