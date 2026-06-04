package com.local.ragingressqueue.adapter.ext.ragflow;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.jupiter.api.Test;

import static org.assertj.core.api.Assertions.assertThat;

class HttpRagFlowGatewayMatchTest {
    private final ObjectMapper mapper = new ObjectMapper();

    @Test
    void matchesWhenDocumentNameCarriesTheHashSuffixToken() throws Exception {
        JsonNode docs = mapper.readTree("[{\"name\":\"chunk-48daba68a6f6.md\"}]");

        assertThat(HttpRagFlowGateway.matchesContentHashFragment(docs, "48daba68a6f6")).isTrue();
    }

    @Test
    void rejectsSubstringHitThatIsNotADelimitedHashSuffix() {
        // keyword search may surface a name that merely contains the hex run; that is not our suffix.
        JsonNode docs = arrayOfNames("report-48daba68a6f6extra.md");

        assertThat(HttpRagFlowGateway.matchesContentHashFragment(docs, "48daba68a6f6")).isFalse();
    }

    @Test
    void rejectsWhenFragmentAppearsOnlyInBodyNotInName() throws Exception {
        // A genuine new document must not be skipped because some unrelated doc's content matched.
        JsonNode docs = mapper.readTree(
            "[{\"name\":\"unrelated-document.md\",\"content\":\"prefix 48daba68a6f6 suffix\"}]"
        );

        assertThat(HttpRagFlowGateway.matchesContentHashFragment(docs, "48daba68a6f6")).isFalse();
    }

    @Test
    void matchesNoExtensionNameEndingWithHashSuffix() {
        JsonNode docs = arrayOfNames("chunk-48daba68a6f6");

        assertThat(HttpRagFlowGateway.matchesContentHashFragment(docs, "48daba68a6f6")).isTrue();
    }

    @Test
    void returnsFalseForEmptyDocsOrBlankFragment() {
        assertThat(HttpRagFlowGateway.matchesContentHashFragment(arrayOfNames(), "48daba68a6f6")).isFalse();
        assertThat(HttpRagFlowGateway.matchesContentHashFragment(arrayOfNames("chunk-48daba68a6f6.md"), "")).isFalse();
    }

    private JsonNode arrayOfNames(String... names) {
        var array = mapper.createArrayNode();
        for (String name : names) {
            array.add(mapper.createObjectNode().put("name", name));
        }
        return array;
    }
}
