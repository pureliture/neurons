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
    void rejectsFragmentThatMatchesAnInnerDotTokenButNotTheFinalHashSuffix() {
        // A different document whose own hash suffix is "def" but whose original name happened to
        // contain "-abc." (e.g. "test-abc.xyz.md" uploaded as "test-abc.xyz-def.md") must NOT be
        // treated as a match for fragment "abc"; otherwise a genuine new "abc" upload is skipped (data
        // loss). The hash fragment is always the token immediately before the final extension.
        JsonNode docs = arrayOfNames("test-abc.xyz-def.md");

        assertThat(HttpRagFlowGateway.matchesContentHashFragment(docs, "abc")).isFalse();
        assertThat(HttpRagFlowGateway.matchesContentHashFragment(docs, "def")).isTrue();
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
