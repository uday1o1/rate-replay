from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from minio import Minio
from ratereplay_persistence.backups import (
    BackupRetentionService,
    BackupService,
    PostgresDumpConfiguration,
    PostgresDumpRunner,
)
from ratereplay_persistence.object_store import EncryptedObjectStore, S3ObjectStore

pytestmark = pytest.mark.backup


def test_real_postgres_backup_is_verified_encrypted_and_expires() -> None:
    configuration = _configuration()
    if configuration is None:
        pytest.skip("Backup integration configuration is unavailable")
    primary_client, backup_client, dump_configuration = configuration
    suffix = secrets.token_hex(8)
    primary_bucket = f"ratereplay-primary-{suffix}"
    backup_bucket = f"ratereplay-backup-{suffix}"
    primary_backend = S3ObjectStore(primary_client, primary_bucket, ensure_bucket=True)
    backup_backend = S3ObjectStore(backup_client, backup_bucket, ensure_bucket=True)
    primary = EncryptedObjectStore(
        primary_backend,
        current_key_version="object-key-v1",
        keys={"object-key-v1": b"p" * 32},
    )
    backup = EncryptedObjectStore(
        backup_backend,
        current_key_version="backup-key-v1",
        keys={"backup-key-v1": b"b" * 32},
    )
    marker = b"RateReplay backup integration plaintext marker"
    primary.put_file("qualification/object", BytesIO(marker), maximum_bytes=1024)
    service = BackupService(
        source_objects=primary,
        backup_objects=backup,
        database_dumper=PostgresDumpRunner(dump_configuration),
        database_maximum_bytes=64 * 1024 * 1024,
        source_object_maximum_bytes=1024,
    )
    try:
        created = service.create(now=datetime(2026, 8, 14, tzinfo=UTC))

        assert created.object_count == 1
        assert created.total_plaintext_bytes > len(marker)
        assert service.verify(created.backup_id) == created
        raw_keys = backup_backend.list_prefix(f"backups/{created.backup_id}")
        assert len(raw_keys) == 3
        raw_payload = b""
        for key in raw_keys:
            with backup_backend.open_file(key, maximum_bytes=64 * 1024 * 1024) as source:
                raw_payload += source.read()
        assert b"PGDMP" not in raw_payload
        assert marker not in raw_payload
        assert b"deletion_ledger_included" not in raw_payload

        expired = BackupRetentionService(backup).expire(now=created.expires_at)

        assert expired.expired_backups == 1
        assert expired.deleted_objects == 3
        assert backup_backend.list_prefix(f"backups/{created.backup_id}") == ()
    finally:
        _empty_and_remove(primary_client, primary_bucket)
        _empty_and_remove(backup_client, backup_bucket)


def _configuration() -> tuple[Minio, Minio, PostgresDumpConfiguration] | None:
    required = {
        name: os.getenv(name)
        for name in (
            "RATEREPLAY_TEST_MINIO_ENDPOINT",
            "RATEREPLAY_TEST_MINIO_ACCESS_KEY_FILE",
            "RATEREPLAY_TEST_MINIO_SECRET_KEY_FILE",
            "RATEREPLAY_TEST_BACKUP_MINIO_ENDPOINT",
            "RATEREPLAY_TEST_BACKUP_MINIO_ACCESS_KEY_FILE",
            "RATEREPLAY_TEST_BACKUP_MINIO_SECRET_KEY_FILE",
            "RATEREPLAY_TEST_BACKUP_PGDUMP_COMMAND_JSON",
            "RATEREPLAY_TEST_BACKUP_PGDUMP_VERSION_COMMAND_JSON",
            "RATEREPLAY_TEST_BACKUP_PGRESTORE_COMMAND_JSON",
        )
    }
    if any(value is None for value in required.values()):
        return None
    primary = Minio(
        required["RATEREPLAY_TEST_MINIO_ENDPOINT"],  # type: ignore[arg-type]
        access_key=_secret(required["RATEREPLAY_TEST_MINIO_ACCESS_KEY_FILE"]),
        secret_key=_secret(required["RATEREPLAY_TEST_MINIO_SECRET_KEY_FILE"]),
        secure=False,
    )
    backup = Minio(
        required["RATEREPLAY_TEST_BACKUP_MINIO_ENDPOINT"],  # type: ignore[arg-type]
        access_key=_secret(required["RATEREPLAY_TEST_BACKUP_MINIO_ACCESS_KEY_FILE"]),
        secret_key=_secret(required["RATEREPLAY_TEST_BACKUP_MINIO_SECRET_KEY_FILE"]),
        secure=False,
    )
    return (
        primary,
        backup,
        PostgresDumpConfiguration(
            dump_command=tuple(
                json.loads(required["RATEREPLAY_TEST_BACKUP_PGDUMP_COMMAND_JSON"])  # type: ignore[arg-type]
            ),
            restore_command=tuple(
                json.loads(required["RATEREPLAY_TEST_BACKUP_PGRESTORE_COMMAND_JSON"])  # type: ignore[arg-type]
            ),
            version_command=tuple(
                json.loads(
                    required["RATEREPLAY_TEST_BACKUP_PGDUMP_VERSION_COMMAND_JSON"]  # type: ignore[arg-type]
                )
            ),
            process_environment=(("PATH", os.environ["PATH"]),),
            maximum_bytes=64 * 1024 * 1024,
            timeout_seconds=60,
        ),
    )


def _secret(path: str | None) -> str:
    assert path is not None
    return Path(path).read_text(encoding="utf-8").strip()


def _empty_and_remove(client: Minio, bucket: str) -> None:
    for item in client.list_objects(bucket, recursive=True):
        if item.object_name is not None:
            client.remove_object(bucket, item.object_name)
    client.remove_bucket(bucket)
