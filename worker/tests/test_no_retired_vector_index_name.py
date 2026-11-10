from __future__ import annotations

import subprocess
from pathlib import Path


def test_tracked_paths_and_contents_do_not_contain_retired_index_name() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")

    banned = b"qdr" + b"ant"
    offenders: list[str] = []
    for raw_path in result.stdout.split(b"\0"):
        if not raw_path:
            continue
        relative_path = raw_path.decode("utf-8", errors="replace")
        if banned.decode("ascii") in relative_path.lower():
            offenders.append(relative_path)
            continue
        path = repo_root / relative_path
        try:
            content = path.read_bytes().lower()
        except FileNotFoundError:
            continue
        if banned in content:
            offenders.append(relative_path)

    assert not offenders, "\n".join(offenders)
