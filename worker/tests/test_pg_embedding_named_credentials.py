"""정확한 자격증명 키의 egress 경계 회귀; 공급자는 합성 spy만 사용한다."""
import pytest

from agent_knowledge.couchdb_source.document_model import sha256_hash
from test_pg_projection_privacy import projection_boundary, recall_boundary


KEYS = ["AWS_ACCESS_KEY_ID", "ACCESS_KEY", "DOCKER_AUTH_CONFIG", "credential"]


@pytest.mark.parametrize("key", KEYS)
@pytest.mark.parametrize("form", ["{key}=synthetic-x", '{{"{key}":\n "synthetic x"}}', "'{key}': 'synthetic x'"])
@pytest.mark.parametrize("surface", ["document", "query"])
def test_named_credentials_never_reach_embedding(monkeypatch, key, form, surface):
    text = form.format(key=key)
    doc, source = {}, None
    if surface == "document":
        projector, store, provider, doc, _ = projection_boundary(text)
        call = lambda: projector.project(target_profile="session-memory", document=doc)
    else:
        search, store, source, provider = recall_boundary(monkeypatch)
        call = lambda: search(text, "/project/privacy-test")
    with pytest.raises(ValueError, match="^PG embedding input rejected by secret egress policy$"):
        call()
    provider.embed.assert_not_called()
    if surface == "document":
        store.insert_chunk.assert_not_called()
        store.transaction.assert_not_called()
        assert doc["body"] == text and doc["content_hash"] == sha256_hash(text)
    else:
        assert source is not None
        assert store.mock_calls == [] and source.mock_calls == []


ALLOWED = [
    "AWS_ACCESS_KEY_ID ACCESS_KEY DOCKER_AUTH_CONFIG credential rotation documentation",
    "auth_config=enabled oauth_config=enabled",
    "credential_type=workload aws_access_key_id_help=docs access_key_count=2",
    "my_credential=description MY_ACCESS_KEY=description MY_DOCKER_AUTH_CONFIG=enabled",
    "credentialless=value access_keyring=local",
] + [f'{key}=<redacted:secret>' for key in KEYS] + [f'{{"{key}": ""}}' for key in KEYS]


@pytest.mark.parametrize("text", ALLOWED)
@pytest.mark.parametrize("surface", ["document", "query"])
def test_ordinary_words_and_redacted_named_values_preserved(monkeypatch, text, surface):
    if surface == "document":
        projector, store, provider, doc, _ = projection_boundary(text)
        projector.project(target_profile="session-memory", document=doc)
        saved = store.insert_chunk.call_args.args[0]
        assert saved.content_markdown.encode() == text.encode()
        assert saved.content_hash == sha256_hash(text)
    else:
        search, _, _, provider = recall_boundary(monkeypatch)
        assert search(text, "/project/privacy-test") == []
    provider.embed.assert_called_once_with(text)
