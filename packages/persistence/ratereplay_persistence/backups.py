"""Encrypted, content-addressed database and object-store backups."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import signal
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from tempfile import SpooledTemporaryFile, TemporaryDirectory
from typing import BinaryIO, Final, Protocol, cast

from pydantic import BaseModel, ConfigDict, ValidationError

from ratereplay_persistence.object_store import (
    ObjectStore,
    ObjectStoreConfiguration,
    ObjectStoreError,
)

BACKUP_MANIFEST_SCHEMA: Final = "ratereplay-backup-manifest-v1"
BACKUP_RETENTION: Final = timedelta(days=30)
BACKUP_PREFIX: Final = "backups"
BACKUP_MANIFEST_MAX_BYTES: Final = 4 * 1024 * 1024
DEFAULT_DATABASE_DUMP_MAX_BYTES: Final = 10 * 1024 * 1024 * 1024
DEFAULT_SOURCE_OBJECT_MAX_BYTES: Final = 64 * 1024 * 1024
_BACKUP_ID_PATTERN: Final = re.compile(r"^(?P<timestamp>\d{8}T\d{12}Z)-(?P<nonce>[0-9a-f]{16})$")


class BackupError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _CommandTimeout(RuntimeError):
    pass


class DatabaseDumpEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    format: str
    key: str
    content_hash: str
    size_bytes: int
    pg_dump_version: str


class BackupObjectEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_key: str
    backup_key: str
    content_hash: str
    size_bytes: int


class BackupManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: str
    backup_id: str
    created_at: datetime
    expires_at: datetime
    retention_days: int
    deletion_ledger_included: bool
    database: DatabaseDumpEntry
    objects: tuple[BackupObjectEntry, ...]


@dataclass(frozen=True, slots=True)
class DatabaseDump:
    path: Path
    content_hash: str
    size_bytes: int
    pg_dump_version: str


class DatabaseDumper(Protocol):
    def dump(self, destination: Path) -> DatabaseDump: ...


@dataclass(frozen=True, slots=True)
class PostgresDumpConfiguration:
    dump_command: tuple[str, ...]
    restore_command: tuple[str, ...]
    process_environment: tuple[tuple[str, str], ...]
    version_command: tuple[str, ...] = ()
    maximum_bytes: int = DEFAULT_DATABASE_DUMP_MAX_BYTES
    timeout_seconds: int = 1800

    @classmethod
    def from_environment(cls, *, environment: str) -> PostgresDumpConfiguration:
        dump_command = _command_from_environment(
            "RATEREPLAY_BACKUP_PGDUMP_COMMAND_JSON",
            default=("pg_dump",),
        )
        restore_command = _command_from_environment(
            "RATEREPLAY_BACKUP_PGRESTORE_COMMAND_JSON",
            default=("pg_restore",),
        )
        version_command = _command_from_environment(
            "RATEREPLAY_BACKUP_PGDUMP_VERSION_COMMAND_JSON",
            default=dump_command,
        )
        process_environment: dict[str, str] = {}
        inherited_path = os.getenv("PATH")
        if inherited_path:
            process_environment["PATH"] = inherited_path
        mappings = {
            "PGHOST": "RATEREPLAY_BACKUP_PGHOST",
            "PGPORT": "RATEREPLAY_BACKUP_PGPORT",
            "PGDATABASE": "RATEREPLAY_BACKUP_PGDATABASE",
            "PGUSER": "RATEREPLAY_BACKUP_PGUSER",
            "PGPASSFILE": "RATEREPLAY_BACKUP_PGPASSFILE",
            "PGSSLMODE": "RATEREPLAY_BACKUP_PGSSLMODE",
            "PGSSLROOTCERT": "RATEREPLAY_BACKUP_PGSSLROOTCERT",
        }
        for target, source in mappings.items():
            value = os.getenv(source)
            if value:
                process_environment[target] = value
        if environment in {"production", "staging"}:
            for required in ("PGHOST", "PGDATABASE", "PGUSER", "PGPASSFILE"):
                if required not in process_environment:
                    raise RuntimeError(
                        f"RATEREPLAY_BACKUP_{required} is required in production and staging"
                    )
        maximum_bytes = _positive_environment_integer(
            "RATEREPLAY_BACKUP_DATABASE_DUMP_MAX_BYTES",
            default=DEFAULT_DATABASE_DUMP_MAX_BYTES,
        )
        timeout_seconds = _positive_environment_integer(
            "RATEREPLAY_BACKUP_DATABASE_TIMEOUT_SECONDS",
            default=1800,
        )
        return cls(
            dump_command=dump_command,
            restore_command=restore_command,
            process_environment=tuple(sorted(process_environment.items())),
            version_command=version_command,
            maximum_bytes=maximum_bytes,
            timeout_seconds=timeout_seconds,
        )


class PostgresDumpRunner:
    """Create and validate a PostgreSQL custom-format dump without shell execution."""

    def __init__(self, configuration: PostgresDumpConfiguration) -> None:
        self._configuration = configuration

    def dump(self, destination: Path) -> DatabaseDump:
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if destination.exists():
            raise BackupError("BACKUP_DESTINATION_EXISTS", "Database dump destination exists")
        environment = dict(self._configuration.process_environment)
        try:
            with destination.open("xb") as output:
                returncode = _run_command(
                    [
                        *self._configuration.dump_command,
                        "--format=custom",
                        "--no-owner",
                        "--no-privileges",
                    ],
                    stdout=output,
                    environment=environment,
                    timeout_seconds=self._configuration.timeout_seconds,
                )
        except FileNotFoundError as error:
            destination.unlink(missing_ok=True)
            raise BackupError(
                "PG_DUMP_UNAVAILABLE",
                "The configured PostgreSQL dump command is unavailable",
            ) from error
        except _CommandTimeout as error:
            destination.unlink(missing_ok=True)
            raise BackupError("PG_DUMP_TIMEOUT", "PostgreSQL dump timed out") from error
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise BackupError(
                "PG_DUMP_IO_FAILED", "PostgreSQL dump could not be written"
            ) from error
        if returncode != 0:
            destination.unlink(missing_ok=True)
            raise BackupError("PG_DUMP_FAILED", "PostgreSQL dump command failed")
        try:
            size_bytes = destination.stat().st_size
            with destination.open("rb") as source:
                magic = source.read(5)
        except OSError as error:
            destination.unlink(missing_ok=True)
            raise BackupError("PG_DUMP_IO_FAILED", "PostgreSQL dump cannot be inspected") from error
        if size_bytes == 0 or size_bytes > self._configuration.maximum_bytes:
            destination.unlink(missing_ok=True)
            raise BackupError(
                "PG_DUMP_SIZE_INVALID",
                "PostgreSQL dump is empty or exceeds its fixed limit",
            )
        if magic != b"PGDMP":
            destination.unlink(missing_ok=True)
            raise BackupError("PG_DUMP_FORMAT_INVALID", "PostgreSQL dump is not custom format")
        try:
            self._verify_restore_listing(destination, environment=environment)
            version = self._read_version(environment=environment)
        except BackupError:
            destination.unlink(missing_ok=True)
            raise
        return DatabaseDump(
            path=destination,
            content_hash=_file_sha256(destination),
            size_bytes=size_bytes,
            pg_dump_version=version,
        )

    def _verify_restore_listing(self, path: Path, *, environment: Mapping[str, str]) -> None:
        try:
            with path.open("rb") as source:
                returncode = _run_command(
                    [*self._configuration.restore_command, "--list"],
                    stdin=source,
                    environment=environment,
                    timeout_seconds=self._configuration.timeout_seconds,
                )
        except FileNotFoundError as error:
            raise BackupError(
                "PG_RESTORE_UNAVAILABLE",
                "The configured PostgreSQL restore command is unavailable",
            ) from error
        except _CommandTimeout as error:
            raise BackupError(
                "PG_RESTORE_VERIFY_TIMEOUT",
                "PostgreSQL dump verification timed out",
            ) from error
        except OSError as error:
            raise BackupError(
                "PG_RESTORE_VERIFY_IO_FAILED",
                "PostgreSQL dump verification could not run",
            ) from error
        if returncode != 0:
            raise BackupError(
                "PG_RESTORE_VERIFY_FAILED",
                "PostgreSQL restore listing rejected the dump",
            )

    def _read_version(self, *, environment: Mapping[str, str]) -> str:
        try:
            with SpooledTemporaryFile(max_size=4096, mode="w+b") as output:
                returncode = _run_command(
                    [
                        *(self._configuration.version_command or self._configuration.dump_command),
                        "--version",
                    ],
                    stdout=cast(BinaryIO, output),
                    environment=environment,
                    timeout_seconds=30,
                )
                output.seek(0)
                encoded_version = output.read(4097)
        except (OSError, _CommandTimeout) as error:
            raise BackupError(
                "PG_DUMP_VERSION_FAILED",
                "PostgreSQL dump version cannot be verified",
            ) from error
        try:
            version = encoded_version.decode("ascii").strip()
        except UnicodeError as error:
            raise BackupError(
                "PG_DUMP_VERSION_FAILED",
                "PostgreSQL dump version is invalid",
            ) from error
        if (
            returncode != 0
            or len(encoded_version) > 4096
            or not version.startswith("pg_dump (PostgreSQL) ")
        ):
            raise BackupError(
                "PG_DUMP_VERSION_FAILED",
                "PostgreSQL dump version cannot be verified",
            )
        return version


@dataclass(frozen=True, slots=True)
class BackupRuntimeConfiguration:
    store: ObjectStoreConfiguration
    postgres: PostgresDumpConfiguration
    source_object_maximum_bytes: int

    @classmethod
    def from_environment(
        cls,
        *,
        environment: str,
        primary_store: ObjectStoreConfiguration,
        default_root: Path,
    ) -> BackupRuntimeConfiguration:
        store = ObjectStoreConfiguration.from_environment(
            environment=environment,
            default_root=default_root,
            namespace="RATEREPLAY_BACKUP",
        )
        if store.current_encryption_key_version is None:
            raise RuntimeError("Backup object storage requires client-side encryption")
        if primary_store.backend == "s3" and store.backend == "s3":
            if primary_store.s3_access_key == store.s3_access_key:
                raise RuntimeError("Backup object storage requires separate credentials")
            if (
                primary_store.s3_endpoint,
                primary_store.s3_bucket,
            ) == (store.s3_endpoint, store.s3_bucket):
                raise RuntimeError("Backup object storage requires a separate location")
        if (
            primary_store.backend == "filesystem"
            and store.backend == "filesystem"
            and primary_store.filesystem_root.resolve() == store.filesystem_root.resolve()
        ):
            raise RuntimeError("Backup object storage requires a separate location")
        return cls(
            store=store,
            postgres=PostgresDumpConfiguration.from_environment(environment=environment),
            source_object_maximum_bytes=_positive_environment_integer(
                "RATEREPLAY_BACKUP_SOURCE_OBJECT_MAX_BYTES",
                default=DEFAULT_SOURCE_OBJECT_MAX_BYTES,
            ),
        )


@dataclass(frozen=True, slots=True)
class BackupResult:
    backup_id: str
    created_at: datetime
    expires_at: datetime
    database_content_hash: str
    manifest_content_hash: str
    object_count: int
    total_plaintext_bytes: int


@dataclass(frozen=True, slots=True)
class BackupRetentionOutcome:
    expired_backups: int
    deleted_objects: int


class BackupService:
    def __init__(
        self,
        *,
        source_objects: ObjectStore,
        backup_objects: ObjectStore,
        database_dumper: DatabaseDumper,
        database_maximum_bytes: int = DEFAULT_DATABASE_DUMP_MAX_BYTES,
        source_object_maximum_bytes: int = DEFAULT_SOURCE_OBJECT_MAX_BYTES,
    ) -> None:
        self._source = source_objects
        self._backups = backup_objects
        self._database_dumper = database_dumper
        self._database_maximum_bytes = database_maximum_bytes
        self._source_object_maximum_bytes = source_object_maximum_bytes

    def create(self, *, now: datetime) -> BackupResult:
        created_at = _aware(now)
        backup_id = _backup_id(created_at)
        prefix = f"{BACKUP_PREFIX}/{backup_id}"
        try:
            with TemporaryDirectory(prefix="ratereplay-backup-") as temporary:
                dump = self._database_dumper.dump(Path(temporary) / "database.dump")
                if dump.size_bytes > self._database_maximum_bytes:
                    raise BackupError(
                        "PG_DUMP_SIZE_INVALID",
                        "PostgreSQL dump exceeds the configured backup limit",
                    )
                database_key = f"{prefix}/database.dump"
                with dump.path.open("rb") as source:
                    stored_database = self._backups.put_file(
                        database_key,
                        source,
                        maximum_bytes=self._database_maximum_bytes,
                    )
                if (
                    stored_database.content_hash != dump.content_hash
                    or stored_database.size_bytes != dump.size_bytes
                    or self._backups.content_hash(
                        database_key,
                        maximum_bytes=self._database_maximum_bytes,
                    )
                    != dump.content_hash
                ):
                    raise BackupError(
                        "BACKUP_DATABASE_VERIFY_FAILED",
                        "Encrypted database backup did not verify after transfer",
                    )
                source_keys = self._source.list_prefix("")
                entries = self._copy_source_objects(source_keys, prefix=prefix)
                if self._source.list_prefix("") != source_keys:
                    raise BackupError(
                        "BACKUP_SOURCE_CHANGED",
                        "Source object set changed during backup",
                    )
                manifest = BackupManifest(
                    schema_version=BACKUP_MANIFEST_SCHEMA,
                    backup_id=backup_id,
                    created_at=created_at,
                    expires_at=created_at + BACKUP_RETENTION,
                    retention_days=30,
                    deletion_ledger_included=False,
                    database=DatabaseDumpEntry(
                        format="postgresql-custom",
                        key=database_key,
                        content_hash=dump.content_hash,
                        size_bytes=dump.size_bytes,
                        pg_dump_version=dump.pg_dump_version,
                    ),
                    objects=entries,
                )
                manifest_bytes = _manifest_bytes(manifest)
                manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
                manifest_key = f"{prefix}/manifest-{manifest_hash}.json"
                stored_manifest = self._backups.put_file(
                    manifest_key,
                    BytesIO(manifest_bytes),
                    maximum_bytes=BACKUP_MANIFEST_MAX_BYTES,
                )
                if (
                    stored_manifest.content_hash != manifest_hash
                    or stored_manifest.size_bytes != len(manifest_bytes)
                ):
                    raise BackupError(
                        "BACKUP_MANIFEST_VERIFY_FAILED",
                        "Encrypted backup manifest did not verify after transfer",
                    )
                return self.verify(backup_id)
        except OSError as error:
            self._cleanup_failed_backup(prefix)
            raise BackupError(
                "BACKUP_IO_FAILED",
                "Backup input or temporary storage could not be read",
            ) from error
        except (BackupError, ObjectStoreError):
            self._cleanup_failed_backup(prefix)
            raise

    def _copy_source_objects(
        self,
        source_keys: tuple[str, ...],
        *,
        prefix: str,
    ) -> tuple[BackupObjectEntry, ...]:
        entries: list[BackupObjectEntry] = []
        copied_hashes: set[str] = set()
        for source_key in source_keys:
            with (
                self._source.open_file(
                    source_key,
                    maximum_bytes=self._source_object_maximum_bytes,
                ) as source,
                SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as staged,
            ):
                content_hash, size_bytes = _copy_and_hash(
                    source,
                    cast(BinaryIO, staged),
                    maximum_bytes=self._source_object_maximum_bytes,
                )
                backup_key = f"{prefix}/objects/{content_hash[:2]}/{content_hash}"
                if content_hash not in copied_hashes:
                    staged.seek(0)
                    stored = self._backups.put_file(
                        backup_key,
                        cast(BinaryIO, staged),
                        maximum_bytes=self._source_object_maximum_bytes,
                    )
                    if (
                        stored.content_hash != content_hash
                        or stored.size_bytes != size_bytes
                        or self._backups.content_hash(
                            backup_key,
                            maximum_bytes=self._source_object_maximum_bytes,
                        )
                        != content_hash
                    ):
                        raise BackupError(
                            "BACKUP_OBJECT_VERIFY_FAILED",
                            "Encrypted object backup did not verify after transfer",
                        )
                    copied_hashes.add(content_hash)
                entries.append(
                    BackupObjectEntry(
                        source_key=source_key,
                        backup_key=backup_key,
                        content_hash=content_hash,
                        size_bytes=size_bytes,
                    )
                )
        return tuple(entries)

    def verify(self, backup_id: str) -> BackupResult:
        created_at = _created_at_from_backup_id(backup_id)
        prefix = f"{BACKUP_PREFIX}/{backup_id}"
        keys = self._backups.list_prefix(prefix)
        manifest_keys = tuple(
            key for key in keys if key.startswith(f"{prefix}/manifest-") and key.endswith(".json")
        )
        if len(manifest_keys) != 1:
            raise BackupError(
                "BACKUP_MANIFEST_INVALID",
                "Backup must contain exactly one committed manifest",
            )
        manifest_key = manifest_keys[0]
        with self._backups.open_file(
            manifest_key,
            maximum_bytes=BACKUP_MANIFEST_MAX_BYTES,
        ) as source:
            manifest_bytes = source.read(BACKUP_MANIFEST_MAX_BYTES + 1)
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest_key != f"{prefix}/manifest-{manifest_hash}.json":
            raise BackupError(
                "BACKUP_MANIFEST_HASH_MISMATCH",
                "Backup manifest content address does not match its bytes",
            )
        try:
            manifest = BackupManifest.model_validate_json(manifest_bytes)
        except ValidationError as error:
            raise BackupError(
                "BACKUP_MANIFEST_INVALID",
                "Backup manifest schema validation failed",
            ) from error
        if (
            manifest.schema_version != BACKUP_MANIFEST_SCHEMA
            or manifest.backup_id != backup_id
            or _aware(manifest.created_at) != created_at
            or _aware(manifest.expires_at) != created_at + BACKUP_RETENTION
            or manifest.retention_days != 30
            or manifest.deletion_ledger_included
        ):
            raise BackupError(
                "BACKUP_MANIFEST_INVALID",
                "Backup manifest lifecycle contract is invalid",
            )
        expected_keys = {manifest_key, manifest.database.key}
        expected_keys.update(entry.backup_key for entry in manifest.objects)
        if set(keys) != expected_keys:
            raise BackupError(
                "BACKUP_OBJECT_SET_MISMATCH",
                "Backup object set does not match its committed manifest",
            )
        if (
            self._backups.content_hash(
                manifest.database.key,
                maximum_bytes=self._database_maximum_bytes,
            )
            != manifest.database.content_hash
        ):
            raise BackupError(
                "BACKUP_DATABASE_VERIFY_FAILED",
                "Database backup content hash does not match its manifest",
            )
        for entry in manifest.objects:
            if (
                self._backups.content_hash(
                    entry.backup_key,
                    maximum_bytes=self._source_object_maximum_bytes,
                )
                != entry.content_hash
            ):
                raise BackupError(
                    "BACKUP_OBJECT_VERIFY_FAILED",
                    "Object backup content hash does not match its manifest",
                )
        return BackupResult(
            backup_id=backup_id,
            created_at=created_at,
            expires_at=created_at + BACKUP_RETENTION,
            database_content_hash=manifest.database.content_hash,
            manifest_content_hash=manifest_hash,
            object_count=len(manifest.objects),
            total_plaintext_bytes=manifest.database.size_bytes
            + sum(entry.size_bytes for entry in manifest.objects),
        )

    def _cleanup_failed_backup(self, prefix: str) -> None:
        try:
            for key in self._backups.list_prefix(prefix):
                self._backups.delete(key)
        except ObjectStoreError as error:
            raise BackupError(
                "BACKUP_CLEANUP_FAILED",
                "Incomplete backup could not be cleaned safely",
            ) from error


class BackupRetentionService:
    def __init__(self, backup_objects: ObjectStore) -> None:
        self._backups = backup_objects

    def expire(self, *, now: datetime) -> BackupRetentionOutcome:
        now = _aware(now)
        keys = self._backups.list_prefix(BACKUP_PREFIX)
        backup_ids: set[str] = set()
        for key in keys:
            parts = key.split("/")
            if len(parts) < 3 or parts[0] != BACKUP_PREFIX:
                raise BackupError(
                    "BACKUP_NAMESPACE_INVALID",
                    "Backup namespace contains an invalid object key",
                )
            _created_at_from_backup_id(parts[1])
            backup_ids.add(parts[1])
        expired_backups = 0
        deleted_objects = 0
        for backup_id in sorted(backup_ids):
            created_at = _created_at_from_backup_id(backup_id)
            if created_at + BACKUP_RETENTION > now:
                continue
            prefix = f"{BACKUP_PREFIX}/{backup_id}"
            for key in self._backups.list_prefix(prefix):
                self._backups.delete(key)
                deleted_objects += 1
            if self._backups.list_prefix(prefix):
                raise BackupError(
                    "BACKUP_RETENTION_VERIFY_FAILED",
                    "Expired backup objects remain after deletion",
                )
            expired_backups += 1
        return BackupRetentionOutcome(
            expired_backups=expired_backups,
            deleted_objects=deleted_objects,
        )


def _backup_id(created_at: datetime) -> str:
    timestamp = created_at.strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{secrets.token_hex(8)}"


def _created_at_from_backup_id(backup_id: str) -> datetime:
    match = _BACKUP_ID_PATTERN.fullmatch(backup_id)
    if match is None:
        raise BackupError("BACKUP_ID_INVALID", "Backup identity is invalid")
    try:
        return datetime.strptime(
            match.group("timestamp"),
            "%Y%m%dT%H%M%S%fZ",
        ).replace(tzinfo=UTC)
    except ValueError as error:
        raise BackupError("BACKUP_ID_INVALID", "Backup timestamp is invalid") from error


def _manifest_bytes(manifest: BackupManifest) -> bytes:
    return json.dumps(
        manifest.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _copy_and_hash(
    source: BinaryIO,
    destination: BinaryIO,
    *,
    maximum_bytes: int,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(64 * 1024):
        size += len(chunk)
        if size > maximum_bytes:
            raise BackupError(
                "BACKUP_SOURCE_OBJECT_OVERSIZED",
                "Source object exceeds the configured backup limit",
            )
        digest.update(chunk)
        destination.write(chunk)
    return digest.hexdigest(), size


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise BackupError("PG_DUMP_IO_FAILED", "PostgreSQL dump cannot be hashed") from error
    return digest.hexdigest()


def _command_from_environment(variable: str, *, default: tuple[str, ...]) -> tuple[str, ...]:
    encoded = os.getenv(variable)
    if encoded is None:
        return default
    try:
        decoded = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{variable} must be a JSON string array") from error
    if (
        not isinstance(decoded, list)
        or not decoded
        or any(not isinstance(value, str) or not value for value in decoded)
    ):
        raise RuntimeError(f"{variable} must be a nonempty JSON string array")
    return tuple(decoded)


def _positive_environment_integer(variable: str, *, default: int) -> int:
    encoded = os.getenv(variable)
    if encoded is None:
        return default
    try:
        value = int(encoded)
    except ValueError as error:
        raise RuntimeError(f"{variable} must be a positive integer") from error
    if value <= 0:
        raise RuntimeError(f"{variable} must be a positive integer")
    return value


def _run_command(
    command: list[str],
    *,
    environment: Mapping[str, str],
    timeout_seconds: int,
    stdin: BinaryIO | None = None,
    stdout: BinaryIO | None = None,
) -> int:
    """Run an operator-owned argument vector with no shell and closed output."""

    if not command or any(not argument or "\x00" in argument for argument in command):
        raise OSError("Command argument vector is invalid")
    with open(os.devnull, "r+b") as null:
        input_fd = stdin.fileno() if stdin is not None else null.fileno()
        output_fd = stdout.fileno() if stdout is not None else null.fileno()
        file_actions = (
            (os.POSIX_SPAWN_DUP2, input_fd, 0),
            (os.POSIX_SPAWN_DUP2, output_fd, 1),
            (os.POSIX_SPAWN_DUP2, null.fileno(), 2),
        )
        process_id = os.posix_spawnp(
            command[0],
            command,
            dict(environment),
            file_actions=file_actions,
        )
        deadline = time.monotonic() + timeout_seconds
        try:
            while True:
                waited, status = os.waitpid(process_id, os.WNOHANG)
                if waited == process_id:
                    return os.waitstatus_to_exitcode(status)
                if time.monotonic() >= deadline:
                    os.kill(process_id, signal.SIGKILL)
                    os.waitpid(process_id, 0)
                    raise _CommandTimeout
                time.sleep(0.01)
        except BaseException:
            try:
                os.kill(process_id, signal.SIGKILL)
                os.waitpid(process_id, 0)
            except (ChildProcessError, ProcessLookupError):
                pass
            raise


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
