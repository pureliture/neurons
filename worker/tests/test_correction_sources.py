"""실제 immutable Git object·branch·ancestry로 근거 검증 경계를 확인한다."""
from pathlib import Path
import os
import subprocess

import pytest

from agent_knowledge.correction_sources import GitSourcePolicy, GitSourceReader, SourceUnavailable
from agent_knowledge.public_safe_util import sha256_text


def git(root, *args, stamp=None):
    env = dict(os.environ)
    if stamp:
        env.update(GIT_AUTHOR_DATE=stamp, GIT_COMMITTER_DATE=stamp)
    result = subprocess.run(["git", "-c", "commit.gpgsign=false", "-c", "user.name=fixture",
        "-c", "user.email=fixture@example.invalid", "-C", str(root), *args],
        capture_output=True, text=True, env=env, check=True, timeout=10)
    return result.stdout.strip()


@pytest.fixture
def repository(tmp_path):
    root = tmp_path / "repository"; root.mkdir()
    git(root, "init", "-b", "fixture")
    (root / "fact.txt").write_text("기능이 아직 없다.\n")
    git(root, "add", "fact.txt")
    git(root, "commit", "-m", "초기 테스트 상태", stamp="2026-01-02T00:00:00+00:00")
    first = git(root, "rev-parse", "HEAD")
    (root / "fact.txt").write_text("기능 구현이 존재한다.\n")
    git(root, "add", "fact.txt")
    # clock이 뒤로 가도 실제 commit 인과는 바뀌지 않는다.
    git(root, "commit", "-m", "후속 테스트 상태", stamp="2025-01-01T00:00:00+00:00")
    second = git(root, "rev-parse", "HEAD")
    return root, first, second


def reader(root, **kwargs):
    return GitSourceReader({"fixture": GitSourcePolicy("fixture", root, **kwargs)})


def read_current(repository):
    root, _, revision = repository
    return reader(root).read(repository="fixture", revision=revision, relative_path="fact.txt",
        expected_content_hash=sha256_text("기능 구현이 존재한다.\n"))


def test_real_git_blob_hash_current_head_and_safe_provenance(repository):
    source = read_current(repository)
    assert source.current_verified and source.redacted_text == "기능 구현이 존재한다.\n"
    assert source.redacted_content_hash == source.content_hash
    assert str(repository[0]) not in str(source.public_proof())
    assert "fact.txt" not in str(source.public_proof())
    reader(repository[0]).assert_still_current(source)
    observed_again = read_current(repository)
    assert source.content_identity == observed_again.content_identity


def test_ancestry_does_not_treat_recent_intake_or_newer_clock_as_later_fact(repository):
    root, first, second = repository
    r = reader(root)
    assert r.compare_revisions(repository="fixture", previous=first, incoming=second) == "incoming_after"
    assert r.compare_revisions(repository="fixture", previous=second, incoming=first) == "incoming_before"
    assert r.compare_revisions(repository="fixture", previous=second, incoming=second) == "same"
    old = r.read(repository="fixture", revision=first, relative_path="fact.txt", expected_content_hash=sha256_text("기능이 아직 없다.\n"))
    assert not old.current_verified
    with pytest.raises(SourceUnavailable, match="source_revision_not_current"):
        r.assert_still_current(old)


def test_diverged_commits_are_incomparable(repository):
    root, first, second = repository
    git(root, "checkout", "-b", "parallel", first)
    (root / "fact.txt").write_text("다른 branch의 기능이다.\n")
    git(root, "add", "fact.txt"); git(root, "commit", "-m", "다른 branch 테스트")
    incoming = git(root, "rev-parse", "HEAD")
    assert reader(root).compare_revisions(repository="fixture", previous=second, incoming=incoming) == "incomparable"


def test_branch_change_invalidates_proof_even_with_same_commit(repository):
    source = read_current(repository)
    git(repository[0], "checkout", "-b", "other-scope")
    with pytest.raises(SourceUnavailable, match="source_revision_not_current"):
        reader(repository[0]).assert_still_current(source)


@pytest.mark.parametrize("path", ["../fact.txt", "/etc/passwd", ".git/config", "a/../fact.txt", "a//b", "fact.txt\n", "a\\b"])
def test_paths_cannot_escape_policy_or_select_private_git_metadata(repository, path):
    root, _, revision = repository
    with pytest.raises(ValueError):
        reader(root).read(repository="fixture", revision=revision, relative_path=path, expected_content_hash=sha256_text("x"))


def test_hash_mismatch_missing_repository_and_size_are_not_verified(repository):
    root, _, revision = repository
    with pytest.raises(SourceUnavailable, match="source_hash_mismatch"):
        reader(root).read(repository="fixture", revision=revision, relative_path="fact.txt", expected_content_hash=sha256_text("invented"))
    with pytest.raises(SourceUnavailable, match="source_repository_not_allowed"):
        reader(root).read(repository="unconfigured", revision=revision, relative_path="fact.txt", expected_content_hash=sha256_text("invented"))
    with pytest.raises(SourceUnavailable, match="source_size_exceeded"):
        reader(root, max_bytes=1).read(repository="fixture", revision=revision, relative_path="fact.txt", expected_content_hash=sha256_text("x"))


def test_symlink_is_not_followed_and_working_tree_changes_are_not_source_evidence(repository):
    root, _, revision = repository
    (root / "link.txt").symlink_to("fact.txt")
    git(root, "add", "link.txt"); git(root, "commit", "-m", "symlink 테스트")
    current = git(root, "rev-parse", "HEAD")
    with pytest.raises(SourceUnavailable, match="source_path_not_regular_file"):
        reader(root).read(repository="fixture", revision=current, relative_path="link.txt", expected_content_hash=sha256_text("fact.txt"))
    (root / "fact.txt").write_text("미커밋 내용은 증거가 아니다.")
    verified = reader(root).read(repository="fixture", revision=revision, relative_path="fact.txt", expected_content_hash=sha256_text("기능 구현이 존재한다.\n"))
    assert verified.redacted_text == "기능 구현이 존재한다.\n"
