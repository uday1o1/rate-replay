from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, cast

import pytest
from minio import Minio
from minio.datatypes import Object
from minio.error import S3Error
from ratereplay_persistence.object_store import (
    EncryptedObjectStore,
    FilesystemObjectStore,
    ObjectStoreConfiguration,
    ObjectStoreError,
    S3ObjectStore,
)
from urllib3 import HTTPConnectionPool
from urllib3.exceptions import MaxRetryError
from urllib3.response import BaseHTTPResponse


class _ObjectResponse(BytesIO):
    def release_conn(self) -> None:
        pass


class _MemoryS3Client:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.bucket_created = False

    def bucket_exists(self, bucket: str) -> bool:
        return self.bucket_created

    def make_bucket(self, bucket: str) -> None:
        self.bucket_created = True

    def put_object(
        self,
        bucket: str,
        key: str,
        data: BinaryIO,
        length: int,
        *,
        content_type: str,
    ) -> None:
        self.objects[key] = data.read(length)

    def get_object(self, bucket: str, key: str) -> _ObjectResponse:
        try:
            return _ObjectResponse(self.objects[key])
        except KeyError as error:
            raise _missing_s3_error(key) from error

    def remove_object(self, bucket: str, key: str) -> None:
        self.objects.pop(key, None)

    def stat_object(self, bucket: str, key: str) -> None:
        if key not in self.objects:
            raise _missing_s3_error(key)

    def list_objects(
        self,
        bucket: str,
        *,
        prefix: str | None,
        recursive: bool,
    ) -> tuple[Object, ...]:
        return tuple(
            Object(bucket, key) for key in self.objects if prefix is None or key.startswith(prefix)
        )


def _missing_s3_error(key: str) -> S3Error:
    return S3Error(
        response=cast(BaseHTTPResponse, None),
        code="NoSuchKey",
        message="missing",
        resource=key,
        request_id=None,
        host_id=None,
        object_name=key,
    )


def test_encrypted_store_round_trips_without_persisting_plaintext(tmp_path: Path) -> None:
    backend = FilesystemObjectStore(tmp_path / "objects")
    encrypted = EncryptedObjectStore(
        backend,
        current_key_version="object-key-v1",
        keys={"object-key-v1": b"1" * 32},
    )
    plaintext = b"sensitive interval payload 123456789"

    stored = encrypted.put_file(
        "owners/opaque/artifacts/report",
        BytesIO(plaintext),
        maximum_bytes=1024,
    )

    assert stored.content_hash == hashlib.sha256(plaintext).hexdigest()
    assert stored.size_bytes == len(plaintext)
    assert encrypted.exists("owners/opaque/artifacts/report")
    assert encrypted.list_prefix("owners/opaque") == ("owners/opaque/artifacts/report",)
    assert (
        encrypted.content_hash(
            "owners/opaque/artifacts/report",
            maximum_bytes=1024,
        )
        == hashlib.sha256(plaintext).hexdigest()
    )
    with encrypted.open_file(
        "owners/opaque/artifacts/report",
        maximum_bytes=1024,
    ) as source:
        assert source.read() == plaintext
    with backend.open_file(
        "owners/opaque/artifacts/report",
        maximum_bytes=2048,
    ) as source:
        envelope = source.read()
    assert plaintext not in envelope
    assert b"sensitive interval" not in envelope


def test_encrypted_store_reads_old_key_after_rotation_and_rejects_retirement(
    tmp_path: Path,
) -> None:
    backend = FilesystemObjectStore(tmp_path / "objects")
    original = EncryptedObjectStore(
        backend,
        current_key_version="object-key-v1",
        keys={"object-key-v1": b"1" * 32},
    )
    original.put_file("old", BytesIO(b"old payload"), maximum_bytes=1024)
    rotated = EncryptedObjectStore(
        backend,
        current_key_version="object-key-v2",
        keys={"object-key-v1": b"1" * 32, "object-key-v2": b"2" * 32},
    )
    rotated.put_file("new", BytesIO(b"new payload"), maximum_bytes=1024)

    with rotated.open_file("old", maximum_bytes=1024) as source:
        assert source.read() == b"old payload"
    with rotated.open_file("new", maximum_bytes=1024) as source:
        assert source.read() == b"new payload"
    retired = EncryptedObjectStore(
        backend,
        current_key_version="object-key-v2",
        keys={"object-key-v2": b"2" * 32},
    )
    with (
        pytest.raises(ObjectStoreError) as raised,
        retired.open_file("old", maximum_bytes=1024),
    ):
        pass
    assert raised.value.code == "OBJECT_ENCRYPTION_INVALID"


@pytest.mark.parametrize("mutation", ["ciphertext", "key", "object-name"])
def test_encrypted_store_fails_closed_on_authentication_changes(
    tmp_path: Path,
    mutation: str,
) -> None:
    backend = FilesystemObjectStore(tmp_path / "objects")
    encrypted = EncryptedObjectStore(
        backend,
        current_key_version="object-key-v1",
        keys={"object-key-v1": b"1" * 32},
    )
    encrypted.put_file("original", BytesIO(b"private payload"), maximum_bytes=1024)
    target = "original"
    if mutation == "ciphertext":
        with backend.open_file("original", maximum_bytes=2048) as source:
            tampered = bytearray(source.read())
        tampered[-1] ^= 1
        backend.put_file("original", BytesIO(tampered), maximum_bytes=2048)
    elif mutation == "key":
        encrypted = EncryptedObjectStore(
            backend,
            current_key_version="object-key-v1",
            keys={"object-key-v1": b"2" * 32},
        )
    else:
        with backend.open_file("original", maximum_bytes=2048) as source:
            copied_envelope = source.read()
        backend.put_file("copied", BytesIO(copied_envelope), maximum_bytes=2048)
        target = "copied"

    with (
        pytest.raises(ObjectStoreError) as raised,
        encrypted.open_file(target, maximum_bytes=1024),
    ):
        pass
    assert raised.value.code == "OBJECT_DECRYPT_FAILED"


def test_encrypted_store_enforces_plaintext_limit_before_write(tmp_path: Path) -> None:
    backend = FilesystemObjectStore(tmp_path / "objects")
    encrypted = EncryptedObjectStore(
        backend,
        current_key_version="object-key-v1",
        keys={"object-key-v1": b"1" * 32},
    )

    with pytest.raises(ObjectStoreError) as raised:
        encrypted.put_file("oversized", BytesIO(b"1234"), maximum_bytes=3)

    assert raised.value.code == "OVERSIZED_FILE"
    assert not backend.exists("oversized")


def test_filesystem_store_enforces_bounds_and_cleans_partial_writes(tmp_path: Path) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")

    with pytest.raises(ObjectStoreError) as raised:
        store.put_file("raw/oversized.xml", BytesIO(b"1234"), maximum_bytes=3)
    assert raised.value.code == "OVERSIZED_FILE"
    assert store.list_prefix("raw") == ()
    assert list((tmp_path / "objects" / "raw").glob("*.partial")) == []

    store.put_file("raw/source.xml", BytesIO(b"1234"), maximum_bytes=4)
    assert store.list_prefix("raw/source.xml") == ("raw/source.xml",)
    assert (
        store.content_hash("raw/source.xml", maximum_bytes=4) == hashlib.sha256(b"1234").hexdigest()
    )
    with pytest.raises(ObjectStoreError) as raised:
        store.open_file("raw/source.xml", maximum_bytes=3).__enter__()
    assert raised.value.code == "OVERSIZED_FILE"
    store.delete("raw/source.xml")
    store.delete("raw/source.xml")
    with pytest.raises(ObjectStoreError) as raised:
        store.open_file("raw/source.xml", maximum_bytes=4).__enter__()
    assert raised.value.code == "RAW_OBJECT_MISSING"


@pytest.mark.parametrize("key", ["", "/absolute", "raw/../escape"])
def test_filesystem_store_rejects_unsafe_keys(tmp_path: Path, key: str) -> None:
    store = FilesystemObjectStore(tmp_path / "objects")

    with pytest.raises(ObjectStoreError) as raised:
        store.exists(key)

    assert raised.value.code == "INVALID_OBJECT_KEY"


def test_s3_adapter_supports_bounded_crud_and_prefix_isolation() -> None:
    client = _MemoryS3Client()
    store = S3ObjectStore(cast(Minio, client), "ratereplay-private", ensure_bucket=True)

    stored = store.put_file("owners/one/raw", BytesIO(b"payload"), maximum_bytes=7)
    store.put_file("owners/one-other/raw", BytesIO(b"adjacent"), maximum_bytes=8)

    assert client.bucket_created
    assert stored.content_hash == hashlib.sha256(b"payload").hexdigest()
    assert store.exists("owners/one/raw")
    assert store.content_hash("owners/one/raw", maximum_bytes=7) == stored.content_hash
    assert store.list_prefix("") == ("owners/one-other/raw", "owners/one/raw")
    assert store.list_prefix("owners/one") == ("owners/one/raw",)
    with store.open_file("owners/one/raw", maximum_bytes=7) as source:
        assert source.read() == b"payload"
    with pytest.raises(ObjectStoreError) as raised:
        store.open_file("owners/one/raw", maximum_bytes=6).__enter__()
    assert raised.value.code == "OVERSIZED_FILE"
    with pytest.raises(ObjectStoreError) as raised:
        store.put_file("owners/one/large", BytesIO(b"12"), maximum_bytes=1)
    assert raised.value.code == "OVERSIZED_FILE"

    store.delete("owners/one/raw")

    assert not store.exists("owners/one/raw")
    with pytest.raises(ObjectStoreError) as raised:
        store.open_file("owners/one/raw", maximum_bytes=7).__enter__()
    assert raised.value.code == "RAW_OBJECT_MISSING"


def test_s3_adapter_requires_a_bucket() -> None:
    with pytest.raises(ValueError, match="bucket is required"):
        S3ObjectStore(cast(Minio, _MemoryS3Client()), "")


def test_s3_adapter_maps_transport_failures_to_safe_domain_error() -> None:
    class UnavailableClient:
        def stat_object(self, bucket: str, key: str) -> None:
            raise MaxRetryError(
                HTTPConnectionPool("objects.invalid"),
                f"https://objects.invalid/{bucket}/{key}",
            )

    store = S3ObjectStore(UnavailableClient(), "ratereplay-private")  # type: ignore[arg-type]

    with pytest.raises(ObjectStoreError) as raised:
        store.exists("owners/opaque/raw/source.xml")

    assert raised.value.code == "OBJECT_STORE_UNAVAILABLE"
    assert "objects.invalid" not in str(raised.value)


def test_encrypted_store_rejects_invalid_key_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="invalid characters"):
        EncryptedObjectStore(
            FilesystemObjectStore(tmp_path / "objects"),
            current_key_version="unsafe/version",
            keys={"unsafe/version": b"1" * 32},
        )


def test_object_store_configuration_loads_versioned_hex_keyring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keyring = tmp_path / "keys"
    keyring.mkdir()
    (keyring / "object-key-v1").write_text("31" * 32 + "\n", encoding="ascii")
    monkeypatch.setenv("RATEREPLAY_OBJECT_ENCRYPTION_KEYS_DIR", str(keyring))
    monkeypatch.setenv(
        "RATEREPLAY_OBJECT_ENCRYPTION_CURRENT_KEY_VERSION",
        "object-key-v1",
    )

    configuration = ObjectStoreConfiguration.from_environment(
        environment="development",
        default_root=tmp_path / "objects",
    )
    store = configuration.build()
    stored = store.put_file("encrypted", BytesIO(b"payload"), maximum_bytes=1024)

    assert stored.size_bytes == 7
    with store.open_file("encrypted", maximum_bytes=1024) as source:
        assert source.read() == b"payload"
    assert b"payload" not in (tmp_path / "objects" / "encrypted").read_bytes()


def test_production_object_store_rejects_unencrypted_or_insecure_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="require the S3"):
        ObjectStoreConfiguration.from_environment(
            environment="production",
            default_root=tmp_path / "objects",
        )
    access = tmp_path / "access"
    secret = tmp_path / "secret"
    access.write_text("access-key", encoding="utf-8")
    secret.write_text("secret-key", encoding="utf-8")
    monkeypatch.setenv("RATEREPLAY_OBJECT_STORE_BACKEND", "s3")
    monkeypatch.setenv("RATEREPLAY_S3_ENDPOINT", "objects.internal:9000")
    monkeypatch.setenv("RATEREPLAY_S3_BUCKET", "ratereplay-private")
    monkeypatch.setenv("RATEREPLAY_S3_ACCESS_KEY_FILE", str(access))
    monkeypatch.setenv("RATEREPLAY_S3_SECRET_KEY_FILE", str(secret))
    monkeypatch.setenv("RATEREPLAY_S3_SECURE", "false")

    with pytest.raises(RuntimeError, match="require TLS"):
        ObjectStoreConfiguration.from_environment(
            environment="production",
            default_root=tmp_path / "objects",
        )


def test_object_store_configuration_rejects_incomplete_encryption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATEREPLAY_OBJECT_ENCRYPTION_CURRENT_KEY_VERSION", "object-key-v1")
    with pytest.raises(RuntimeError, match="requires an encryption key directory"):
        ObjectStoreConfiguration.from_environment(
            environment="development",
            default_root=tmp_path / "objects",
        )

    monkeypatch.delenv("RATEREPLAY_OBJECT_ENCRYPTION_CURRENT_KEY_VERSION")
    keyring = tmp_path / "keys"
    keyring.mkdir()
    monkeypatch.setenv("RATEREPLAY_OBJECT_ENCRYPTION_KEYS_DIR", str(keyring))
    with pytest.raises(RuntimeError, match="CURRENT_KEY_VERSION is required"):
        ObjectStoreConfiguration.from_environment(
            environment="development",
            default_root=tmp_path / "objects",
        )


def test_object_store_configuration_rejects_invalid_backend_and_boolean(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATEREPLAY_OBJECT_STORE_BACKEND", "tape")
    with pytest.raises(RuntimeError, match="must be filesystem or s3"):
        ObjectStoreConfiguration.from_environment(
            environment="development",
            default_root=tmp_path / "objects",
        )

    access = tmp_path / "access"
    secret = tmp_path / "secret"
    access.write_text("access-key", encoding="utf-8")
    secret.write_text("secret-key", encoding="utf-8")
    monkeypatch.setenv("RATEREPLAY_OBJECT_STORE_BACKEND", "s3")
    monkeypatch.setenv("RATEREPLAY_S3_ENDPOINT", "objects.internal:9000")
    monkeypatch.setenv("RATEREPLAY_S3_BUCKET", "ratereplay-private")
    monkeypatch.setenv("RATEREPLAY_S3_ACCESS_KEY_FILE", str(access))
    monkeypatch.setenv("RATEREPLAY_S3_SECRET_KEY_FILE", str(secret))
    monkeypatch.setenv("RATEREPLAY_S3_SECURE", "sometimes")
    with pytest.raises(RuntimeError, match="must be true or false"):
        ObjectStoreConfiguration.from_environment(
            environment="development",
            default_root=tmp_path / "objects",
        )
