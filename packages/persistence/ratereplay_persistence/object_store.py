"""Filesystem object storage with atomic, bounded writes for local operation."""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO


class ObjectStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredObject:
    content_hash: str
    size_bytes: int


class FilesystemObjectStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _path(self, key: str) -> Path:
        pure = PurePosixPath(key)
        if pure.is_absolute() or ".." in pure.parts or not pure.parts:
            raise ObjectStoreError("INVALID_OBJECT_KEY", "Object key is outside the store")
        candidate = self._root.joinpath(*pure.parts).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise ObjectStoreError("INVALID_OBJECT_KEY", "Object key is outside the store")
        return candidate

    def put_file(self, key: str, source: BinaryIO, *, maximum_bytes: int) -> StoredObject:
        destination = self._path(key)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = destination.parent / f".{destination.name}.{secrets.token_hex(8)}.partial"
        digest = hashlib.sha256()
        size = 0
        try:
            with temporary.open("xb") as output:
                while chunk := source.read(64 * 1024):
                    size += len(chunk)
                    if size > maximum_bytes:
                        raise ObjectStoreError(
                            "OVERSIZED_FILE", "Upload exceeds the adapter size limit"
                        )
                    digest.update(chunk)
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, destination)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return StoredObject(digest.hexdigest(), size)

    def read(self, key: str, *, maximum_bytes: int) -> bytes:
        path = self._path(key)
        try:
            size = path.stat().st_size
        except FileNotFoundError as error:
            raise ObjectStoreError("RAW_OBJECT_MISSING", "Raw object is unavailable") from error
        if size > maximum_bytes:
            raise ObjectStoreError("OVERSIZED_FILE", "Stored object exceeds the adapter limit")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as error:
            raise ObjectStoreError("OBJECT_DELETE_FAILED", "Object could not be deleted") from error

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()
