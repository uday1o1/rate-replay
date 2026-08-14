from __future__ import annotations

import hashlib
import sys
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from ratereplay_persistence.backups import (
    BACKUP_RETENTION,
    BackupError,
    BackupRetentionService,
    BackupRuntimeConfiguration,
    BackupService,
    DatabaseDump,
    PostgresDumpConfiguration,
    PostgresDumpRunner,
)
from ratereplay_persistence.database import DatabaseAtRestConfiguration
from ratereplay_persistence.object_store import (
    EncryptedObjectStore,
    FilesystemObjectStore,
    ObjectStoreConfiguration,
)

NOW = datetime(2026, 8, 14, 10, 11, 12, 345678, tzinfo=UTC)


class _FakeDumper:
    def __init__(self, payload: bytes = b"PGDMP fake custom dump") -> None:
        self._payload = payload

    def dump(self, destination: Path) -> DatabaseDump:
        destination.write_bytes(self._payload)
        return DatabaseDump(
            path=destination,
            content_hash=hashlib.sha256(self._payload).hexdigest(),
            size_bytes=len(self._payload),
            pg_dump_version="pg_dump (PostgreSQL) 16.10",
        )


def _service(tmp_path: Path) -> tuple[BackupService, FilesystemObjectStore]:
    source = FilesystemObjectStore(tmp_path / "source")
    backup_backend = FilesystemObjectStore(tmp_path / "backup")
    backup = EncryptedObjectStore(
        backup_backend,
        current_key_version="backup-key-v1",
        keys={"backup-key-v1": b"b" * 32},
    )
    return (
        BackupService(
            source_objects=source,
            backup_objects=backup,
            database_dumper=_FakeDumper(),
            database_maximum_bytes=1024,
            source_object_maximum_bytes=1024,
        ),
        backup_backend,
    )


def test_backup_is_content_addressed_encrypted_verified_and_excludes_ledger(
    tmp_path: Path,
) -> None:
    source = FilesystemObjectStore(tmp_path / "source")
    source.put_file("owners/one/raw", BytesIO(b"same bytes"), maximum_bytes=1024)
    source.put_file("owners/two/report", BytesIO(b"same bytes"), maximum_bytes=1024)
    backup_backend = FilesystemObjectStore(tmp_path / "backup")
    backup = EncryptedObjectStore(
        backup_backend,
        current_key_version="backup-key-v1",
        keys={"backup-key-v1": b"b" * 32},
    )
    service = BackupService(
        source_objects=source,
        backup_objects=backup,
        database_dumper=_FakeDumper(),
        database_maximum_bytes=1024,
        source_object_maximum_bytes=1024,
    )

    created = service.create(now=NOW)
    verified = service.verify(created.backup_id)

    assert verified == created
    assert created.created_at == NOW
    assert created.expires_at == NOW + BACKUP_RETENTION
    assert created.object_count == 2
    keys = backup_backend.list_prefix(f"backups/{created.backup_id}")
    assert len([key for key in keys if "/objects/" in key]) == 1
    assert len([key for key in keys if "/manifest-" in key]) == 1
    persisted = b"".join((tmp_path / "backup" / key).read_bytes() for key in keys)
    assert b"same bytes" not in persisted
    assert b"PGDMP" not in persisted
    assert b"deletion_ledger_included" not in persisted


def test_backup_retention_deletes_at_exact_thirty_day_deadline(tmp_path: Path) -> None:
    service, backup_backend = _service(tmp_path)
    created = service.create(now=NOW)
    encrypted = EncryptedObjectStore(
        backup_backend,
        current_key_version="backup-key-v1",
        keys={"backup-key-v1": b"b" * 32},
    )
    retention = BackupRetentionService(encrypted)

    before = retention.expire(now=created.expires_at - timedelta(microseconds=1))
    at_deadline = retention.expire(now=created.expires_at)

    assert before.expired_backups == 0
    assert at_deadline.expired_backups == 1
    assert at_deadline.deleted_objects == 2
    assert backup_backend.list_prefix(f"backups/{created.backup_id}") == ()


def test_backup_retention_removes_expired_incomplete_prefix(tmp_path: Path) -> None:
    backup = FilesystemObjectStore(tmp_path / "backup")
    backup_id = "20260715T101112345678Z-0123456789abcdef"
    backup.put_file(
        f"backups/{backup_id}/database.dump",
        BytesIO(b"incomplete"),
        maximum_bytes=1024,
    )

    outcome = BackupRetentionService(backup).expire(now=NOW)

    assert outcome.expired_backups == 1
    assert outcome.deleted_objects == 1


def test_backup_retention_fails_closed_on_invalid_namespace(tmp_path: Path) -> None:
    backup = FilesystemObjectStore(tmp_path / "backup")
    backup.put_file("backups/not-a-backup/object", BytesIO(b"x"), maximum_bytes=1)

    with pytest.raises(BackupError) as raised:
        BackupRetentionService(backup).expire(now=NOW)

    assert raised.value.code == "BACKUP_ID_INVALID"
    assert backup.exists("backups/not-a-backup/object")


def test_failed_backup_is_cleaned_when_source_set_changes(tmp_path: Path) -> None:
    class ChangingStore(FilesystemObjectStore):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self._list_calls = 0

        def list_prefix(self, prefix: str) -> tuple[str, ...]:
            listed = super().list_prefix(prefix)
            if not prefix:
                self._list_calls += 1
                if self._list_calls == 2:
                    self.put_file("late", BytesIO(b"late"), maximum_bytes=4)
                    return super().list_prefix(prefix)
            return listed

    source = ChangingStore(tmp_path / "source")
    source.put_file("initial", BytesIO(b"initial"), maximum_bytes=7)
    backups = FilesystemObjectStore(tmp_path / "backup")
    service = BackupService(
        source_objects=source,
        backup_objects=backups,
        database_dumper=_FakeDumper(),
        database_maximum_bytes=1024,
        source_object_maximum_bytes=1024,
    )

    with pytest.raises(BackupError) as raised:
        service.create(now=NOW)

    assert raised.value.code == "BACKUP_SOURCE_CHANGED"
    assert backups.list_prefix("backups") == ()


def test_backup_normalizes_unexpected_dump_io_failure(tmp_path: Path) -> None:
    class FailingDumper:
        def dump(self, destination: Path) -> DatabaseDump:
            raise OSError("injected private path detail")

    backups = FilesystemObjectStore(tmp_path / "backup")
    service = BackupService(
        source_objects=FilesystemObjectStore(tmp_path / "source"),
        backup_objects=backups,
        database_dumper=FailingDumper(),
    )

    with pytest.raises(BackupError) as raised:
        service.create(now=NOW)

    assert raised.value.code == "BACKUP_IO_FAILED"
    assert "private path detail" not in str(raised.value)
    assert backups.list_prefix("backups") == ()


def test_postgres_dump_runner_uses_custom_format_and_verifies_restore(
    tmp_path: Path,
) -> None:
    dump_script = """
import sys
if "--version" in sys.argv:
    sys.stdout.write("pg_dump (PostgreSQL) 16.10\\n")
else:
    sys.stdout.buffer.write(b"PGDMP verified custom archive")
"""
    restore_script = """
import sys
raise SystemExit(0 if sys.stdin.buffer.read(5) == b"PGDMP" else 1)
"""
    configuration = PostgresDumpConfiguration(
        dump_command=(sys.executable, "-c", dump_script),
        restore_command=(sys.executable, "-c", restore_script),
        process_environment=(("PATH", "/usr/bin"),),
        maximum_bytes=1024,
        timeout_seconds=10,
    )

    dumped = PostgresDumpRunner(configuration).dump(tmp_path / "database.dump")

    assert dumped.content_hash == hashlib.sha256(b"PGDMP verified custom archive").hexdigest()
    assert dumped.pg_dump_version == "pg_dump (PostgreSQL) 16.10"


def test_postgres_dump_runner_removes_invalid_output(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "invalid.dump"
    runner = PostgresDumpRunner(
        PostgresDumpConfiguration(
            dump_command=(
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'not a custom dump')",
            ),
            restore_command=(sys.executable, "-c", "raise SystemExit(1)"),
            process_environment=(),
            maximum_bytes=1024,
        )
    )

    with pytest.raises(BackupError) as raised:
        runner.dump(destination)

    assert raised.value.code == "PG_DUMP_FORMAT_INVALID"
    assert not destination.exists()


def test_postgres_dump_runner_fails_safely_when_command_is_unavailable(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "missing.dump"
    runner = PostgresDumpRunner(
        PostgresDumpConfiguration(
            dump_command=("ratereplay-command-that-does-not-exist",),
            restore_command=("pg_restore",),
            process_environment=(("PATH", "/usr/bin"),),
        )
    )

    with pytest.raises(BackupError) as raised:
        runner.dump(destination)

    assert raised.value.code == "PG_DUMP_UNAVAILABLE"
    assert not destination.exists()


def test_backup_configuration_requires_encryption_and_separate_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = ObjectStoreConfiguration(
        backend="s3",
        filesystem_root=tmp_path / "primary",
        s3_endpoint="primary.internal:9000",
        s3_bucket="primary",
        s3_access_key="shared-user",
        s3_secret_key="primary-secret",
    )
    backup_access = tmp_path / "backup-access"
    backup_secret = tmp_path / "backup-secret"
    backup_access.write_text("shared-user", encoding="utf-8")
    backup_secret.write_text("different-secret", encoding="utf-8")
    keys = tmp_path / "backup-keys"
    keys.mkdir()
    (keys / "backup-key-v1").write_text("62" * 32, encoding="ascii")
    monkeypatch.setenv("RATEREPLAY_BACKUP_OBJECT_STORE_BACKEND", "s3")
    monkeypatch.setenv("RATEREPLAY_BACKUP_S3_ENDPOINT", "backup.internal:9000")
    monkeypatch.setenv("RATEREPLAY_BACKUP_S3_BUCKET", "backups")
    monkeypatch.setenv("RATEREPLAY_BACKUP_S3_ACCESS_KEY_FILE", str(backup_access))
    monkeypatch.setenv("RATEREPLAY_BACKUP_S3_SECRET_KEY_FILE", str(backup_secret))
    monkeypatch.setenv("RATEREPLAY_BACKUP_S3_SECURE", "true")
    monkeypatch.setenv("RATEREPLAY_BACKUP_OBJECT_ENCRYPTION_KEYS_DIR", str(keys))
    monkeypatch.setenv(
        "RATEREPLAY_BACKUP_OBJECT_ENCRYPTION_CURRENT_KEY_VERSION",
        "backup-key-v1",
    )

    with pytest.raises(RuntimeError, match="separate credentials"):
        BackupRuntimeConfiguration.from_environment(
            environment="development",
            primary_store=primary,
            default_root=tmp_path / "backups",
        )

    backup_access.write_text("backup-user", encoding="utf-8")
    configured = BackupRuntimeConfiguration.from_environment(
        environment="development",
        primary_store=primary,
        default_root=tmp_path / "backups",
    )

    assert configured.store.s3_access_key == "backup-user"
    assert configured.store.current_encryption_key_version == "backup-key-v1"


def test_database_volume_encryption_configuration_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="require managed"):
        DatabaseAtRestConfiguration.from_environment(environment="production")

    monkeypatch.setenv("RATEREPLAY_DATABASE_AT_REST_ENCRYPTION", "managed-volume")

    assert (
        DatabaseAtRestConfiguration.from_environment(environment="production").mode
        == "managed-volume"
    )
