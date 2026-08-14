"""Content identity for the exact locked application environment."""

from __future__ import annotations

import hashlib
from pathlib import Path


def environment_lock_hash(repository_root: Path) -> str:
    """Hash every runtime lock with domain separation and unambiguous lengths."""

    digest = hashlib.sha256(b"RateReplay.EnvironmentLocks.v1\x00")
    for name in ("uv.lock", "pnpm-lock.yaml"):
        content = (repository_root / name).read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name.encode("ascii"))
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()
