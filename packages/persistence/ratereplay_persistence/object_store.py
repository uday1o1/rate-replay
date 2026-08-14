"""Filesystem object storage with atomic, bounded writes for local operation."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import struct
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Final, Protocol, cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from minio import Minio
from minio.error import InvalidResponseError, S3Error, ServerError
from urllib3.exceptions import HTTPError

from ratereplay_persistence.keyrings import KEY_VERSION, KeyringError, load_keyring


class ObjectStoreError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredObject:
    content_hash: str
    size_bytes: int


class ObjectStore(Protocol):
    def put_file(self, key: str, source: BinaryIO, *, maximum_bytes: int) -> StoredObject: ...

    def content_hash(self, key: str, *, maximum_bytes: int) -> str: ...

    def open_file(
        self,
        key: str,
        *,
        maximum_bytes: int,
    ) -> AbstractContextManager[BinaryIO]: ...

    def delete(self, key: str) -> None: ...

    def exists(self, key: str) -> bool: ...

    def list_prefix(self, prefix: str) -> tuple[str, ...]: ...


class _BinaryReader(Protocol):
    def read(self, size: int = -1) -> bytes: ...


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

    def content_hash(self, key: str, *, maximum_bytes: int) -> str:
        digest = hashlib.sha256()
        with self.open_file(key, maximum_bytes=maximum_bytes) as source:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @contextmanager
    def open_file(self, key: str, *, maximum_bytes: int) -> Iterator[BinaryIO]:
        path = self._path(key)
        try:
            size = path.stat().st_size
        except FileNotFoundError as error:
            raise ObjectStoreError("RAW_OBJECT_MISSING", "Raw object is unavailable") from error
        if size > maximum_bytes:
            raise ObjectStoreError("OVERSIZED_FILE", "Stored object exceeds the adapter limit")
        with path.open("rb") as source:
            yield source

    def delete(self, key: str) -> None:
        try:
            self._path(key).unlink(missing_ok=True)
        except OSError as error:
            raise ObjectStoreError("OBJECT_DELETE_FAILED", "Object could not be deleted") from error

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def list_prefix(self, prefix: str) -> tuple[str, ...]:
        """Return a strongly consistent snapshot of every file below a key prefix."""

        root = self._root if not prefix else self._path(prefix)
        if root.is_file():
            return (prefix,)
        if not root.exists():
            return ()
        return tuple(
            sorted(
                path.relative_to(self._root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            )
        )


class S3ObjectStore:
    """Strongly consistent S3-compatible object storage through the MinIO SDK."""

    def __init__(
        self,
        client: Minio,
        bucket: str,
        *,
        ensure_bucket: bool = False,
    ) -> None:
        if not bucket:
            raise ValueError("S3 object-store bucket is required")
        self._client = client
        self._bucket = bucket
        if ensure_bucket:
            try:
                if not self._client.bucket_exists(bucket):
                    self._client.make_bucket(bucket)
            except (S3Error, ServerError, InvalidResponseError, HTTPError) as error:
                raise _s3_error(error) from error

    def put_file(self, key: str, source: BinaryIO, *, maximum_bytes: int) -> StoredObject:
        _validate_key(key)
        digest = hashlib.sha256()
        size = 0
        with SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as staged:
            while chunk := source.read(64 * 1024):
                size += len(chunk)
                if size > maximum_bytes:
                    raise ObjectStoreError(
                        "OVERSIZED_FILE",
                        "Upload exceeds the adapter size limit",
                    )
                digest.update(chunk)
                staged.write(chunk)
            staged.seek(0)
            try:
                self._client.put_object(
                    self._bucket,
                    key,
                    cast(BinaryIO, staged),
                    size,
                    content_type="application/octet-stream",
                )
            except (S3Error, ServerError, InvalidResponseError, HTTPError) as error:
                raise _s3_error(error) from error
        return StoredObject(digest.hexdigest(), size)

    def content_hash(self, key: str, *, maximum_bytes: int) -> str:
        digest = hashlib.sha256()
        with self.open_file(key, maximum_bytes=maximum_bytes) as source:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @contextmanager
    def open_file(self, key: str, *, maximum_bytes: int) -> Iterator[BinaryIO]:
        _validate_key(key)
        response = None
        try:
            response = self._client.get_object(self._bucket, key)
            payload = _read_bounded(response, maximum_bytes=maximum_bytes)
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject"}:
                raise ObjectStoreError(
                    "RAW_OBJECT_MISSING",
                    "Object is unavailable",
                ) from error
            raise _s3_error(error) from error
        except (ServerError, InvalidResponseError, HTTPError) as error:
            raise _s3_error(error) from error
        finally:
            if response is not None:
                response.close()
                response.release_conn()
        with BytesIO(payload) as source:
            yield source

    def delete(self, key: str) -> None:
        _validate_key(key)
        try:
            self._client.remove_object(self._bucket, key)
        except (S3Error, ServerError, InvalidResponseError, HTTPError) as error:
            raise _s3_error(error) from error

    def exists(self, key: str) -> bool:
        _validate_key(key)
        try:
            self._client.stat_object(self._bucket, key)
            return True
        except S3Error as error:
            if error.code in {"NoSuchKey", "NoSuchObject"}:
                return False
            raise _s3_error(error) from error
        except (ServerError, InvalidResponseError, HTTPError) as error:
            raise _s3_error(error) from error

    def list_prefix(self, prefix: str) -> tuple[str, ...]:
        if prefix:
            _validate_key(prefix)
        try:
            boundary = f"{prefix.rstrip('/')}/" if prefix else ""
            return tuple(
                sorted(
                    item.object_name
                    for item in self._client.list_objects(
                        self._bucket,
                        prefix=prefix or None,
                        recursive=True,
                    )
                    if item.object_name is not None
                    and (
                        not prefix
                        or item.object_name == prefix
                        or item.object_name.startswith(boundary)
                    )
                )
            )
        except (S3Error, ServerError, InvalidResponseError, HTTPError) as error:
            raise _s3_error(error) from error


ENCRYPTED_OBJECT_SCHEMA: Final = "ratereplay-encrypted-object-v1"
_ENCRYPTED_OBJECT_MAGIC: Final = b"RateReplay.EncryptedObject.v1\x00"
_MAXIMUM_ENCRYPTION_HEADER: Final = 4096
_AES_GCM_TAG_BYTES: Final = 16
_MAXIMUM_ENCRYPTION_OVERHEAD: Final = (
    len(_ENCRYPTED_OBJECT_MAGIC) + 4 + _MAXIMUM_ENCRYPTION_HEADER + _AES_GCM_TAG_BYTES
)


class EncryptedObjectStore:
    """Versioned authenticated encryption wrapper with explicit read key rotation."""

    def __init__(
        self,
        backend: ObjectStore,
        *,
        current_key_version: str,
        keys: dict[str, bytes],
    ) -> None:
        if current_key_version not in keys:
            raise ValueError("Current object encryption key version is unavailable")
        if any(not _valid_key_version(version) for version in keys):
            raise ValueError("Object encryption key versions contain invalid characters")
        if any(len(key) != 32 for key in keys.values()):
            raise ValueError("Object encryption keys must contain exactly 32 bytes")
        self._backend = backend
        self._current_key_version = current_key_version
        self._keys = {version: bytes(key) for version, key in keys.items()}

    def put_file(self, key: str, source: BinaryIO, *, maximum_bytes: int) -> StoredObject:
        _validate_key(key)
        nonce = os.urandom(12)
        header = json.dumps(
            {
                "key_version": self._current_key_version,
                "nonce": nonce.hex(),
                "schema_version": ENCRYPTED_OBJECT_SCHEMA,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        associated_data = _associated_data(key, header)
        encryptor = Cipher(
            algorithms.AES(self._keys[self._current_key_version]),
            modes.GCM(nonce),
        ).encryptor()
        encryptor.authenticate_additional_data(associated_data)
        digest = hashlib.sha256()
        size = 0
        with SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as envelope:
            envelope.write(_ENCRYPTED_OBJECT_MAGIC)
            envelope.write(struct.pack(">I", len(header)))
            envelope.write(header)
            while chunk := source.read(64 * 1024):
                size += len(chunk)
                if size > maximum_bytes:
                    raise ObjectStoreError(
                        "OVERSIZED_FILE",
                        "Object exceeds the adapter limit",
                    )
                digest.update(chunk)
                envelope.write(encryptor.update(chunk))
            envelope.write(encryptor.finalize())
            envelope.write(encryptor.tag)
            envelope.seek(0)
            self._backend.put_file(
                key,
                cast(BinaryIO, envelope),
                maximum_bytes=maximum_bytes + _MAXIMUM_ENCRYPTION_OVERHEAD,
            )
        return StoredObject(digest.hexdigest(), size)

    def content_hash(self, key: str, *, maximum_bytes: int) -> str:
        digest = hashlib.sha256()
        with self.open_file(key, maximum_bytes=maximum_bytes) as source:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    @contextmanager
    def open_file(self, key: str, *, maximum_bytes: int) -> Iterator[BinaryIO]:
        _validate_key(key)
        with self._backend.open_file(
            key,
            maximum_bytes=maximum_bytes + _MAXIMUM_ENCRYPTION_OVERHEAD,
        ) as source:
            envelope = _read_bounded(
                source,
                maximum_bytes=maximum_bytes + _MAXIMUM_ENCRYPTION_OVERHEAD,
            )
        plaintext = self._decrypt(key, envelope, maximum_bytes=maximum_bytes)
        with BytesIO(plaintext) as source:
            yield source

    def delete(self, key: str) -> None:
        self._backend.delete(key)

    def exists(self, key: str) -> bool:
        return self._backend.exists(key)

    def list_prefix(self, prefix: str) -> tuple[str, ...]:
        return self._backend.list_prefix(prefix)

    def _decrypt(self, key: str, envelope: bytes, *, maximum_bytes: int) -> bytes:
        prefix_size = len(_ENCRYPTED_OBJECT_MAGIC) + 4
        if len(envelope) < prefix_size or not envelope.startswith(_ENCRYPTED_OBJECT_MAGIC):
            raise ObjectStoreError(
                "OBJECT_ENCRYPTION_INVALID",
                "Encrypted object envelope is invalid",
            )
        header_size = struct.unpack(
            ">I",
            envelope[len(_ENCRYPTED_OBJECT_MAGIC) : prefix_size],
        )[0]
        if header_size > _MAXIMUM_ENCRYPTION_HEADER:
            raise ObjectStoreError(
                "OBJECT_ENCRYPTION_INVALID",
                "Encrypted object header exceeds its fixed limit",
            )
        header_end = prefix_size + header_size
        if header_end + _AES_GCM_TAG_BYTES > len(envelope):
            raise ObjectStoreError(
                "OBJECT_ENCRYPTION_INVALID",
                "Encrypted object envelope is truncated",
            )
        try:
            header_bytes = envelope[prefix_size:header_end]
            header = json.loads(header_bytes.decode("ascii"))
            if (
                not isinstance(header, dict)
                or header.get("schema_version") != ENCRYPTED_OBJECT_SCHEMA
                or not isinstance(header.get("key_version"), str)
                or not isinstance(header.get("nonce"), str)
            ):
                raise ValueError
            key_version = header["key_version"]
            nonce = bytes.fromhex(header["nonce"])
            encryption_key = self._keys[key_version]
            if len(nonce) != 12:
                raise ValueError
        except (UnicodeError, ValueError, KeyError, TypeError) as error:
            raise ObjectStoreError(
                "OBJECT_ENCRYPTION_INVALID",
                "Encrypted object header cannot be verified",
            ) from error
        try:
            plaintext = AESGCM(encryption_key).decrypt(
                nonce,
                envelope[header_end:],
                _associated_data(key, header_bytes),
            )
        except InvalidTag as error:
            raise ObjectStoreError(
                "OBJECT_DECRYPT_FAILED",
                "Encrypted object authentication failed",
            ) from error
        if len(plaintext) > maximum_bytes:
            raise ObjectStoreError(
                "OVERSIZED_FILE",
                "Decrypted object exceeds the adapter limit",
            )
        return plaintext


@dataclass(frozen=True, slots=True)
class ObjectStoreConfiguration:
    backend: str
    filesystem_root: Path
    s3_endpoint: str | None = None
    s3_bucket: str | None = None
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_secure: bool = True
    current_encryption_key_version: str | None = None
    encryption_keys: tuple[tuple[str, bytes], ...] = ()

    @classmethod
    def from_environment(
        cls,
        *,
        environment: str,
        default_root: Path,
        namespace: str = "RATEREPLAY",
    ) -> ObjectStoreConfiguration:
        if not namespace or any(
            not (character.isupper() or character.isdigit() or character == "_")
            for character in namespace
        ):
            raise ValueError("Object-store environment namespace is invalid")
        backend_variable = f"{namespace}_OBJECT_STORE_BACKEND"
        root_variable = f"{namespace}_OBJECT_STORE_ROOT"
        backend = os.getenv(backend_variable, "filesystem")
        filesystem_root = Path(os.getenv(root_variable, str(default_root)))
        if backend not in {"filesystem", "s3"}:
            raise RuntimeError(f"{backend_variable} must be filesystem or s3")
        if environment in {"production", "staging"} and backend != "s3":
            raise RuntimeError("Production and staging require the S3 object-store backend")
        s3_endpoint: str | None = None
        s3_bucket: str | None = None
        s3_access_key: str | None = None
        s3_secret_key: str | None = None
        s3_secure = True
        if backend == "s3":
            s3_endpoint = _required_environment(f"{namespace}_S3_ENDPOINT")
            s3_bucket = _required_environment(f"{namespace}_S3_BUCKET")
            s3_access_key = _read_text_secret(f"{namespace}_S3_ACCESS_KEY_FILE")
            s3_secret_key = _read_text_secret(f"{namespace}_S3_SECRET_KEY_FILE")
            s3_secure = _environment_boolean(f"{namespace}_S3_SECURE", default=True)
            if environment in {"production", "staging"} and not s3_secure:
                raise RuntimeError("Production and staging require TLS to the S3 object store")
        keys_directory_variable = f"{namespace}_OBJECT_ENCRYPTION_KEYS_DIR"
        current_key_variable = f"{namespace}_OBJECT_ENCRYPTION_CURRENT_KEY_VERSION"
        key_directory = os.getenv(keys_directory_variable)
        current_key_version = os.getenv(current_key_variable)
        encryption_keys: tuple[tuple[str, bytes], ...] = ()
        if key_directory is not None:
            if current_key_version is None:
                raise RuntimeError(f"{current_key_variable} is required")
            encryption_keys = _load_encryption_keyring(
                Path(key_directory),
                current_version=current_key_version,
            )
        elif environment in {"production", "staging"}:
            raise RuntimeError(f"{keys_directory_variable} is required")
        elif current_key_version is not None:
            raise RuntimeError("Object encryption key version requires an encryption key directory")
        return cls(
            backend=backend,
            filesystem_root=filesystem_root,
            s3_endpoint=s3_endpoint,
            s3_bucket=s3_bucket,
            s3_access_key=s3_access_key,
            s3_secret_key=s3_secret_key,
            s3_secure=s3_secure,
            current_encryption_key_version=current_key_version,
            encryption_keys=encryption_keys,
        )

    @classmethod
    def filesystem(cls, root: Path) -> ObjectStoreConfiguration:
        return cls(backend="filesystem", filesystem_root=root)

    def build(self, *, ensure_bucket: bool = False) -> ObjectStore:
        if self.backend == "filesystem":
            backend: ObjectStore = FilesystemObjectStore(self.filesystem_root)
        elif (
            self.backend == "s3"
            and self.s3_endpoint is not None
            and self.s3_bucket is not None
            and self.s3_access_key is not None
            and self.s3_secret_key is not None
        ):
            backend = S3ObjectStore(
                Minio(
                    self.s3_endpoint,
                    access_key=self.s3_access_key,
                    secret_key=self.s3_secret_key,
                    secure=self.s3_secure,
                ),
                self.s3_bucket,
                ensure_bucket=ensure_bucket,
            )
        else:
            raise RuntimeError("Object-store configuration is incomplete")
        if self.current_encryption_key_version is None:
            return backend
        return EncryptedObjectStore(
            backend,
            current_key_version=self.current_encryption_key_version,
            keys=dict(self.encryption_keys),
        )


def _read_bounded(source: _BinaryReader, *, maximum_bytes: int) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while chunk := source.read(min(64 * 1024, maximum_bytes + 1 - size)):
        chunks.append(chunk)
        size += len(chunk)
        if size > maximum_bytes:
            raise ObjectStoreError("OVERSIZED_FILE", "Object exceeds the adapter limit")
    return b"".join(chunks)


def _validate_key(key: str) -> None:
    pure = PurePosixPath(key)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ObjectStoreError("INVALID_OBJECT_KEY", "Object key is outside the store")


def _valid_key_version(version: str) -> bool:
    return KEY_VERSION.fullmatch(version) is not None


def _associated_data(key: str, header: bytes) -> bytes:
    return b"RateReplay.EncryptedObjectAAD.v1\x00" + key.encode("utf-8") + b"\x00" + header


def _s3_error(error: object) -> ObjectStoreError:
    code = getattr(error, "code", None)
    if code == "AccessDenied":
        return ObjectStoreError("OBJECT_STORE_ACCESS_DENIED", "Object-store access was denied")
    return ObjectStoreError("OBJECT_STORE_UNAVAILABLE", "Object-store operation failed")


def _required_environment(variable: str) -> str:
    value = os.getenv(variable)
    if value is None or not value:
        raise RuntimeError(f"{variable} is required")
    return value


def _read_text_secret(variable: str) -> str:
    path = _required_environment(variable)
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as error:
        raise RuntimeError(f"{variable} cannot be read") from error
    if not value:
        raise RuntimeError(f"{variable} cannot be empty")
    return value


def _environment_boolean(variable: str, *, default: bool) -> bool:
    value = os.getenv(variable)
    if value is None:
        return default
    normalized = value.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise RuntimeError(f"{variable} must be true or false")


def _load_encryption_keyring(
    directory: Path,
    *,
    current_version: str,
) -> tuple[tuple[str, bytes], ...]:
    try:
        keyring = load_keyring(directory, current_version=current_version)
    except KeyringError as error:
        raise RuntimeError("Object encryption keyring is invalid or unavailable") from error
    return tuple(keyring.keys.items())
