package com.local.ragingressqueue.architecture;

import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.classes;
import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;

import com.local.ragingressqueue.queue.port.IngestConsumer;
import com.local.ragingressqueue.queue.port.IngestPublisher;
import com.local.ragingressqueue.queue.port.QueueStatusProvider;
import com.local.ragingressqueue.target.port.RagTargetAdapter;
import com.tngtech.archunit.core.importer.ImportOption;
import com.tngtech.archunit.junit.AnalyzeClasses;
import com.tngtech.archunit.junit.ArchTest;
import com.tngtech.archunit.lang.ArchRule;

/**
 * ADR-0002 「계층 의존성 규칙」을 코드로 강제한다. 5개 규칙은 ADR-0002와 1:1 대응한다.
 *
 * <p>Rule 5(common 순수성)는 ADR 원안을 그대로 적용하면 현 코드가 위반한다. 합성 루트
 * {@code common.config}(Spring assembly)와 feature domain value-type 사용은 의도된 편차로
 * 인정하고 규칙에서 제외한다. 근거는 ADR-0002 "Status Update — 구현 현실" 절에 명문화돼 있다.
 */
@AnalyzeClasses(
        packages = "com.local.ragingressqueue",
        importOptions = ImportOption.DoNotIncludeTests.class)
class ArchitectureRulesTest {

    /** Rule 1 — 서비스는 컨트롤러를 알 수 없다. */
    @ArchTest
    static final ArchRule service_should_not_depend_on_api =
            noClasses()
                    .that().resideInAPackage("..service..")
                    .should().dependOnClassesThat().resideInAPackage("..api..")
                    .because("service는 api(표현 계층)를 역참조하면 안 된다 (ADR-0002 Rule 1)");

    /** Rule 2 — 도메인은 어댑터를 알 수 없다. */
    @ArchTest
    static final ArchRule domain_should_not_depend_on_adapter =
            noClasses()
                    .that().resideInAPackage("..domain..")
                    .should().dependOnClassesThat().resideInAPackage("..adapter..")
                    .because("domain은 순수해야 하며 adapter를 알면 안 된다 (ADR-0002 Rule 2)");

    /** Rule 3 — 포트는 어댑터를 알 수 없다. */
    @ArchTest
    static final ArchRule port_should_not_depend_on_adapter =
            noClasses()
                    .that().resideInAPackage("..port..")
                    .should().dependOnClassesThat().resideInAPackage("..adapter..")
                    .because("port는 기술 중립 계약이며 adapter를 역참조하면 안 된다 (ADR-0002 Rule 3)");

    /** Rule 4 — 어댑터는 포트를 구현한다(포트 구현체는 adapter에, 포트 자체는 port 패키지에). */
    @ArchTest
    static final ArchRule port_implementations_reside_in_adapter =
            classes()
                    .that().areAssignableTo(RagTargetAdapter.class)
                    .or().areAssignableTo(IngestPublisher.class)
                    .or().areAssignableTo(IngestConsumer.class)
                    .or().areAssignableTo(QueueStatusProvider.class)
                    .should().resideInAnyPackage("..adapter..", "..port..")
                    .because("port 구현체는 adapter 패키지에 위치해야 한다 (ADR-0002 Rule 4)");

    /**
     * Rule 5 — 공통은 순수해야 한다(feature service/api/worker 역참조 금지).
     *
     * <p>의도된 편차로 제외: (1) 합성 루트 {@code common.config}는 Spring bean 조립을 위해
     * feature/adapter를 참조해야 한다, (2) feature domain value-type 공유는 허용한다
     * (예: {@code common.logging.SafeJobSummary} → {@code ingest.domain}). ADR-0002 참조.
     */
    @ArchTest
    static final ArchRule common_should_not_depend_on_feature_logic =
            noClasses()
                    .that().resideInAPackage("..common..")
                    .and().resideOutsideOfPackage("..common.config..")
                    .should().dependOnClassesThat().resideInAnyPackage(
                            "..ingest.api..", "..ingest.service..",
                            "..delivery.worker..", "..delivery.service..",
                            "..status.api..", "..status.service..")
                    .because("common(합성 루트 제외)은 feature의 service/api/worker 로직을 알면 안 된다 (ADR-0002 Rule 5)");
}
