from __future__ import annotations

import os
import secrets
from io import BytesIO
from pathlib import Path

import pytest
from minio import Minio
from ratereplay_persistence.object_store import (
    EncryptedObjectStore,
    ObjectStoreError,
    S3ObjectStore,
)

pytestmark = pytest.mark.object_store


def test_minio_adapter_is_strongly_consistent_and_client_encrypted() -> None:
    endpoint = os.getenv("RATEREPLAY_TEST_MINIO_ENDPOINT")
    access_file = os.getenv("RATEREPLAY_TEST_MINIO_ACCESS_KEY_FILE")
    secret_file = os.getenv("RATEREPLAY_TEST_MINIO_SECRET_KEY_FILE")
    if endpoint is None or access_file is None or secret_file is None:
        pytest.skip("MinIO integration configuration is unavailable")
    access_key = Path(access_file).read_text(encoding="utf-8").strip()
    secret_key = Path(secret_file).read_text(encoding="utf-8").strip()
    client = Minio(
        endpoint,
        access_key=access_key,
        secret_key=secret_key,
        secure=False,
    )
    bucket = f"ratereplay-test-{secrets.token_hex(8)}"
    backend = S3ObjectStore(client, bucket, ensure_bucket=True)
    store = EncryptedObjectStore(
        backend,
        current_key_version="object-key-v1",
        keys={"object-key-v1": b"1" * 32},
    )
    key = "owners/opaque/reports/report.json"
    adjacent_key = "owners/opaque2/reports/report.json"
    plaintext = b'sensitive interval marker: {"energy_wh":12345}'
    try:
        stored = store.put_file(key, BytesIO(plaintext), maximum_bytes=1024)
        store.put_file(adjacent_key, BytesIO(b"adjacent owner"), maximum_bytes=1024)

        assert stored.size_bytes == len(plaintext)
        assert store.exists(key)
        assert store.list_prefix("owners/opaque") == (key,)
        with store.open_file(key, maximum_bytes=1024) as source:
            assert source.read() == plaintext
        response = client.get_object(bucket, key)
        try:
            encrypted_payload = response.read()
        finally:
            response.close()
            response.release_conn()
        assert plaintext not in encrypted_payload
        assert b"energy_wh" not in encrypted_payload

        store.delete(key)
        store.delete(adjacent_key)

        assert not store.exists(key)
        assert store.list_prefix("owners/opaque") == ()
        with (
            pytest.raises(ObjectStoreError) as raised,
            store.open_file(key, maximum_bytes=1024),
        ):
            pass
        assert raised.value.code == "RAW_OBJECT_MISSING"
    finally:
        for item in client.list_objects(bucket, recursive=True):
            if item.object_name is not None:
                client.remove_object(bucket, item.object_name)
        client.remove_bucket(bucket)
