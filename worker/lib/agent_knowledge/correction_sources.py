"""수집 지연과 별개로 immutable Git source를 독립 검증하는 adapter.

설정된 server repository만 읽는다. client의 설명·hash만으로 source를
인증하지 않으며, 관측 시각 대신 실제 commit ancestry를 제공한다.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Mapping

from .public_safe_util import ensure_public_safe, hash_payload, require_sha256, sha256_text
from .redaction import redact_public_ingress_text


class SourceUnavailable(ValueError):
    def __init__(self, reason_code: str):
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class GitSourcePolicy:
    repository: str
    root: Path
    branch_ref: str = "HEAD"
    max_bytes: int = 65536

    def __post_init__(self):
        if not isinstance(self.repository, str) or not self.repository or len(self.repository) > 240:
            raise ValueError("repository identity required")
        ensure_public_safe(self.repository, "repository")
        if not re.fullmatch(r"HEAD|refs/heads/[A-Za-z0-9_./-]+", self.branch_ref):
            raise ValueError("configured branch reference required")
        if type(self.max_bytes) is not int or not 1 <= self.max_bytes <= 262144:
            raise ValueError("bounded source size required")
        object.__setattr__(self, "root", Path(self.root).resolve())


@dataclass(frozen=True)
class VerifiedGitSource:
    repository: str
    revision: str
    blob_id: str
    relative_path_hash: str
    content_hash: str
    redacted_content_hash: str
    redacted_text: str
    observed_at: str
    head_revision: str
    branch_ref_hash: str

    @property
    def current_verified(self) -> bool:
        return self.revision == self.head_revision

    @property
    def verification_id(self) -> str:
        return hash_payload(self.public_proof())

    @property
    def content_identity(self) -> str:
        proof = self.public_proof()
        return hash_payload({key: value for key, value in proof.items() if key not in {"observed_at", "head_revision", "current_verified"}})

    def public_proof(self) -> dict:
        return {"adapter": "git_blob.v1", "repository": self.repository, "revision": self.revision,
            "blob_id": self.blob_id, "relative_path_hash": self.relative_path_hash,
            "content_hash": self.content_hash, "redacted_content_hash": self.redacted_content_hash,
            "observed_at": self.observed_at, "head_revision": self.head_revision,
            "branch_ref_hash": self.branch_ref_hash, "current_verified": self.current_verified}


class GitSourceReader:
    def __init__(self, policies: Mapping[str, GitSourcePolicy]):
        self.policies = dict(policies)
        if not self.policies or any(key != policy.repository for key, policy in self.policies.items()):
            raise ValueError("explicit repository policies required")

    def read(self, *, repository: str, revision: str, relative_path: str,
             expected_content_hash: str) -> VerifiedGitSource:
        require_sha256(expected_content_hash, "expected source content hash")
        return self._read(repository=repository, revision=revision, relative_path=relative_path,
            expected_content_hash=expected_content_hash)

    def read_current(self, *, repository: str, relative_path: str) -> VerifiedGitSource:
        """server 정책이 지정한 현재 source를 독립적으로 읽는다."""
        policy = self._policy(repository)
        return self._read(repository=repository, revision=self._head(policy), relative_path=relative_path,
            expected_content_hash=None)

    def _read(self, *, repository, revision, relative_path, expected_content_hash):
        policy = self._policy(repository)
        _revision(revision)
        _relative_path(relative_path)
        head = self._head(policy)
        branch = self._branch_identity(policy)
        resolved = self._git(policy, ["rev-parse", "--verify", revision + "^{commit}"]).strip().decode()
        if resolved != revision:
            raise SourceUnavailable("source_revision_not_exact")
        # caller path를 glob이나 특수 pathspec으로 해석하지 않는다.
        entries = self._git(policy, ["ls-tree", "-z", "--full-tree", revision, "--", relative_path]).split(b"\0")
        entries = [entry for entry in entries if entry]
        if len(entries) != 1:
            raise SourceUnavailable("source_path_unavailable")
        try:
            meta, name = entries[0].split(b"\t", 1)
            mode, kind, blob = meta.decode().split()
            if name.decode() != relative_path or mode not in {"100644", "100755"} or kind != "blob":
                raise ValueError("not an admitted regular file")
        except (ValueError, UnicodeError):
            raise SourceUnavailable("source_path_not_regular_file") from None
        size = self._git(policy, ["cat-file", "-s", blob]).strip()
        if not size.isdigit() or int(size) > policy.max_bytes:
            raise SourceUnavailable("source_size_exceeded")
        raw = self._git(policy, ["cat-file", "blob", blob])
        if len(raw) != int(size) or len(raw) > policy.max_bytes:
            raise SourceUnavailable("source_content_unverifiable")
        try:
            text = raw.decode("utf-8")
        except UnicodeError:
            raise SourceUnavailable("source_encoding_unsupported") from None
        if "\0" in text:
            raise SourceUnavailable("source_encoding_unsupported")
        if expected_content_hash is not None and sha256_text(text) != expected_content_hash:
            raise SourceUnavailable("source_hash_mismatch")
        expected_content_hash = sha256_text(text)
        redacted = redact_public_ingress_text(text)
        ensure_public_safe(redacted, "VerifiedGitSource")
        if self._head(policy) != head or self._branch_identity(policy) != branch:
            raise SourceUnavailable("source_head_changed_during_read")
        return VerifiedGitSource(repository, revision, blob, sha256_text(relative_path), expected_content_hash,
            sha256_text(redacted), redacted, datetime.now(timezone.utc).isoformat(), head, sha256_text(branch))

    def assert_still_current(self, source: VerifiedGitSource) -> None:
        policy = self._policy(source.repository)
        if (sha256_text(self._branch_identity(policy)) != source.branch_ref_hash or not source.current_verified
                or self._head(policy) != source.revision):
            raise SourceUnavailable("source_revision_not_current")

    def compare_revisions(self, *, repository: str, previous: str, incoming: str) -> str:
        """같은 repository의 실제 ancestry를 확인한다. 시각·도착 순서는 쓰지 않는다."""
        policy = self._policy(repository)
        _revision(previous); _revision(incoming)
        for revision in (previous, incoming):
            self._git(policy, ["cat-file", "-e", revision + "^{commit}"])
        if previous == incoming:
            return "same"
        if self._ancestor(policy, previous, incoming):
            return "incoming_after"
        if self._ancestor(policy, incoming, previous):
            return "incoming_before"
        return "incomparable"

    def _policy(self, repository):
        try:
            return self.policies[repository]
        except (KeyError, TypeError):
            raise SourceUnavailable("source_repository_not_allowed") from None

    def _head(self, policy):
        value = self._git(policy, ["rev-parse", "--verify", policy.branch_ref + "^{commit}"]).strip().decode()
        _revision(value)
        return value

    def _branch_identity(self, policy):
        if policy.branch_ref != "HEAD":
            return policy.branch_ref
        result = self._run(policy, ["symbolic-ref", "--quiet", "HEAD"])
        if result.returncode == 1:
            return "detached"
        if result.returncode:
            raise SourceUnavailable("source_branch_unavailable")
        return result.stdout.strip().decode()

    def _ancestor(self, policy, previous, incoming):
        result = self._run(policy, ["merge-base", "--is-ancestor", previous, incoming])
        if result.returncode not in (0, 1):
            raise SourceUnavailable("source_ancestry_unavailable")
        return result.returncode == 0

    def _git(self, policy, arguments):
        result = self._run(policy, arguments)
        if result.returncode:
            raise SourceUnavailable("source_revision_unavailable")
        return result.stdout

    @staticmethod
    def _run(policy, arguments):
        try:
            return subprocess.run(["git", "--no-pager", "--no-replace-objects", "--literal-pathspecs", "-C", str(policy.root), *arguments],
                capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            # source root나 git stderr를 공개 오류에 포함하지 않는다.
            raise SourceUnavailable("source_repository_unavailable") from None


def _revision(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value):
        raise ValueError("immutable full commit digest required")


def _relative_path(value):
    if (not isinstance(value, str) or not value or len(value) > 1024 or value.startswith("/")
            or "\\" in value or any(ord(char) < 32 for char in value)
            or any(part in {"", ".", "..", ".git"} for part in value.split("/"))
            or PurePosixPath(value).as_posix() != value):
        raise ValueError("bounded repository-relative regular file path required")
