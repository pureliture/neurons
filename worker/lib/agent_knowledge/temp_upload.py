from __future__ import annotations

import os
import secrets
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def secure_upload_payload(runtime_dir: Path | str, body: str):
    runtime = Path(runtime_dir)
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    upload_dir = runtime / "tmp-upload"
    upload_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(upload_dir, 0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    for _ in range(10):
        path = upload_dir / f"payload-{os.getpid()}-{secrets.token_hex(8)}.md"
        try:
            fd = os.open(path, flags, 0o600)
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError("could not create unique upload payload")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(path, 0o600)
        yield path
    finally:
        path.unlink(missing_ok=True)
