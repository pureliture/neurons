"""정책 롤백도 새 공표로 구분하면서 기존 v1 policy hash는 유지한다."""

from dataclasses import replace

import pytest

from agent_knowledge.fact_correction_coordinator import GitJsonFactPolicy, GitSemanticFactPolicy
from agent_knowledge.fact_resolution import FactScope
from agent_knowledge.public_safe_util import sha256_text


def policies():
    scope = FactScope("revision-test", "neurons", "refs/heads/fixture", "test", "feature", "status")
    return (
        GitJsonFactPolicy("status", scope, "기능 상태", "typed_payload.status_value",
            "기능 상태: {value}", "facts.json", "/status"),
        GitSemanticFactPolicy("status", scope, "기능 상태", "typed_payload.status_value",
            "기능 상태: {value}", "facts.txt", "등록 파일의 기능 상태",
            (("ready", "파일의 기능 상태는 ready다."), ("failed", "파일의 기능 상태는 failed다.")),
            "repository_content", "unit-only", sha256_text("unit-only")),
    )


def test_default_revision_preserves_v1_identity_and_rollback_has_a_distinct_version():
    # bf96465의 원래 클래스에서 같은 합성 설정으로 실제 계산한 v1 hash다.
    legacy = ("sha256:7213daf673b4ce178e1e6329036581f1f427fd6fd781b389154cdf56c3935c9d",
        "sha256:5bdae06725a030835983cc0d1b03ae12e4a79dc01366d12ef1a0d22a58e89953")
    for original, previous_hash in zip(policies(), legacy):
        identity = original.public_identity()
        assert "publication_revision" not in identity
        assert original.version == previous_hash
        changed = replace(original, max_age_seconds=60, publication_revision=1)
        rollback = replace(original, publication_revision=2)
        assert len({original.version, changed.version, rollback.version}) == 3
        assert rollback.scope == original.scope and rollback.relative_path == original.relative_path
        assert rollback.public_identity()["publication_revision"] == 2


@pytest.mark.parametrize("revision", [True, -1, 1.0, 2147483648, None])
def test_invalid_publication_revision_is_rejected(revision):
    for rule in policies():
        with pytest.raises(ValueError, match="bounded server policy publication revision required"):
            replace(rule, publication_revision=revision)
