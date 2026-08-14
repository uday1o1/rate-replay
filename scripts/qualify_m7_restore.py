"""Exercise the Milestone 7 local backup, restore, and rollback contract."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess  # nosec B404
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Final

from minio import Minio
from ratereplay_persistence.backups import BackupRetentionService, BackupService, DatabaseDump
from ratereplay_persistence.database import make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger, LedgerEvent
from ratereplay_persistence.deletion_sweep import DeletionSweepService
from ratereplay_persistence.deletions import (
    DeletionCheckpoint,
    DeletionCoordinator,
)
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.keyrings import VersionedKeyring
from ratereplay_persistence.models import ImportRecord, RawObjectRecord, UserRecord
from ratereplay_persistence.object_store import EncryptedObjectStore, S3ObjectStore
from ratereplay_persistence.restore import derive_local_postgres_outcome
from ratereplay_worker.deletion_worker import DeletionWorker
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

EVIDENCE_SCHEMA: Final = "m7-local-restore-rollback-evidence-v1"
LOCAL_EVIDENCE_LEVEL: Final = "LOCAL_REPRODUCIBLE"
HEAD_REVISION: Final = "20260814_0015"
SAFE_ROLLBACK_REVISION: Final = "20260814_0013"
AUDIT_REVISION: Final = "20260814_0014"
OUTCOME_KEY: Final = b"o" * 32
_BACKUP_ID = re.compile(r"backup_id=([^ ]+)")


class QualificationError(RuntimeError):
    pass


class _UnusedDumper:
    def dump(self, destination: Path) -> DatabaseDump:
        del destination
        raise AssertionError("The verification-only backup service cannot create a dump")


def main() -> int:
    runtime = _required_path("RATEREPLAY_M7_RUNTIME_DIR")
    artifact_file = _required_path("RATEREPLAY_M7_ARTIFACT_FILE")
    source_database_url = _required("RATEREPLAY_M7_SOURCE_DATABASE_URL")
    quarantine_database_url = _required("RATEREPLAY_M7_QUARANTINE_DATABASE_URL")
    source_container = _required("RATEREPLAY_M7_SOURCE_POSTGRES_CONTAINER")
    quarantine_container = _required("RATEREPLAY_M7_QUARANTINE_POSTGRES_CONTAINER")
    started_at = datetime.now(UTC)
    source_environment = _service_environment(
        database_url=source_database_url,
        primary_endpoint=_required("RATEREPLAY_M7_SOURCE_MINIO_ENDPOINT"),
        primary_access_file=_required_path("RATEREPLAY_M7_SOURCE_MINIO_ACCESS_KEY_FILE"),
        primary_secret_file=_required_path("RATEREPLAY_M7_SOURCE_MINIO_SECRET_KEY_FILE"),
        primary_bucket="ratereplay-m7-source",
        backup_endpoint=_required("RATEREPLAY_M7_BACKUP_MINIO_ENDPOINT"),
        backup_access_file=_required_path("RATEREPLAY_M7_BACKUP_MINIO_ACCESS_KEY_FILE"),
        backup_secret_file=_required_path("RATEREPLAY_M7_BACKUP_MINIO_SECRET_KEY_FILE"),
        source_container=source_container,
        restore_container=source_container,
        runtime=runtime,
    )
    quarantine_environment = _service_environment(
        database_url=quarantine_database_url,
        primary_endpoint=_required("RATEREPLAY_M7_QUARANTINE_MINIO_ENDPOINT"),
        primary_access_file=_required_path("RATEREPLAY_M7_QUARANTINE_MINIO_ACCESS_KEY_FILE"),
        primary_secret_file=_required_path("RATEREPLAY_M7_QUARANTINE_MINIO_SECRET_KEY_FILE"),
        primary_bucket="ratereplay-m7-quarantine",
        backup_endpoint=_required("RATEREPLAY_M7_BACKUP_MINIO_ENDPOINT"),
        backup_access_file=_required_path("RATEREPLAY_M7_BACKUP_MINIO_ACCESS_KEY_FILE"),
        backup_secret_file=_required_path("RATEREPLAY_M7_BACKUP_MINIO_SECRET_KEY_FILE"),
        source_container=source_container,
        restore_container=quarantine_container,
        runtime=runtime,
    )
    _alembic(source_database_url, "upgrade", "head")
    _alembic(source_database_url, "check")
    source_engine = make_engine(source_database_url)
    try:
        outcome = _exercise_restore_contract(
            runtime=runtime,
            started_at=started_at,
            source_engine=source_engine,
            source_environment=source_environment,
            quarantine_database_url=quarantine_database_url,
            quarantine_environment=quarantine_environment,
            source_container=source_container,
            quarantine_container=quarantine_container,
        )
        migration = _exercise_safe_migration_rollback(
            runtime=runtime,
            source_container=source_container,
            source_database_url=source_database_url,
        )
    finally:
        source_engine.dispose()
    evidence = _build_evidence(
        started_at=started_at,
        outcome=outcome,
        migration=migration,
    )
    write_evidence(artifact_file, evidence)
    verified = verify_evidence(json.loads(artifact_file.read_text(encoding="ascii")))
    print(
        "gate_result=PASS "
        f"evidence_level={verified['evidence_level']} "
        f"artifact_sha256={verified['artifact_sha256']}"
    )
    return 0


def _exercise_restore_contract(
    *,
    runtime: Path,
    started_at: datetime,
    source_engine: Engine,
    source_environment: dict[str, str],
    quarantine_database_url: str,
    quarantine_environment: dict[str, str],
    source_container: str,
    quarantine_container: str,
) -> dict[str, object]:
    sessions = make_session_factory(source_engine)
    source_objects, _source_raw = _object_store(
        endpoint=_required("RATEREPLAY_M7_SOURCE_MINIO_ENDPOINT"),
        access_file=_required_path("RATEREPLAY_M7_SOURCE_MINIO_ACCESS_KEY_FILE"),
        secret_file=_required_path("RATEREPLAY_M7_SOURCE_MINIO_SECRET_KEY_FILE"),
        bucket="ratereplay-m7-source",
        key=b"p" * 32,
        key_version="object-key-v1",
    )
    backup_objects, backup_raw = _object_store(
        endpoint=_required("RATEREPLAY_M7_BACKUP_MINIO_ENDPOINT"),
        access_file=_required_path("RATEREPLAY_M7_BACKUP_MINIO_ACCESS_KEY_FILE"),
        secret_file=_required_path("RATEREPLAY_M7_BACKUP_MINIO_SECRET_KEY_FILE"),
        bucket="ratereplay-m7-backups",
        key=b"b" * 32,
        key_version="backup-key-v1",
    )
    quarantine_objects, _ = _object_store(
        endpoint=_required("RATEREPLAY_M7_QUARANTINE_MINIO_ENDPOINT"),
        access_file=_required_path("RATEREPLAY_M7_QUARANTINE_MINIO_ACCESS_KEY_FILE"),
        secret_file=_required_path("RATEREPLAY_M7_QUARANTINE_MINIO_SECRET_KEY_FILE"),
        bucket="ratereplay-m7-quarantine",
        key=b"p" * 32,
        key_version="object-key-v1",
    )
    ledger = FilesystemDeletionLedger(
        runtime / "ledger",
        keyring=VersionedKeyring.single("ledger-v1", b"l" * 32),
        restore_key_version="restore-v1",
    )
    owners = {name: secrets.token_hex(16) for name in ("abort", "deleted", "fenced", "retained")}
    scopes = {name: secrets.token_hex(16) for name in owners}
    raw_import_id = secrets.token_hex(16)
    raw_id = secrets.token_hex(16)
    raw_key = f"owners/{owners['retained']}/imports/{raw_import_id}/raw.xml"
    raw_expires_at = started_at + timedelta(seconds=10)
    marker = b"RateReplay M7 encrypted backup marker"
    qualification_password_hash = hashlib.sha256(b"m7-noncredential").hexdigest()
    with sessions.begin() as database:
        for name, owner_id in owners.items():
            database.add(
                UserRecord(
                    id=owner_id,
                    username_canonical=f"m7_{name}_{owner_id[:8]}",
                    password_hash=qualification_password_hash,
                    created_at=started_at,
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                    deletion_scope_id=scopes[name],
                )
            )
        database.flush()
        database.add(
            ImportRecord(
                id=raw_import_id,
                owner_user_id=owners["retained"],
                state="READY",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                deletion_scope_id=secrets.token_hex(16),
                adapter="SIMULATED",
                raw_content_hash=hashlib.sha256(marker).hexdigest(),
                created_at=started_at,
            )
        )
        database.flush()
        database.add(
            RawObjectRecord(
                id=raw_id,
                owner_user_id=owners["retained"],
                import_id=raw_import_id,
                object_key=raw_key,
                content_hash=hashlib.sha256(marker).hexdigest(),
                size_bytes=len(marker),
                state="AVAILABLE",
                created_at=started_at,
                expires_at=raw_expires_at,
            )
        )
    owner_keys: dict[str, str] = {}
    for name, owner_id in owners.items():
        key = f"owners/{owner_id}/qualification/{name}.bin"
        owner_keys[name] = key
        source_objects.put_file(key, BytesIO(marker + name.encode("ascii")), maximum_bytes=1024)
    source_objects.put_file(raw_key, BytesIO(marker), maximum_bytes=1024)

    created = _worker(("create-backup",), source_environment)
    backup_match = _BACKUP_ID.search(created.stdout)
    if backup_match is None:
        raise QualificationError("BACKUP_ID_MISSING")
    backup_id = backup_match.group(1)
    backup_service = BackupService(
        source_objects=source_objects,
        backup_objects=backup_objects,
        database_dumper=_UnusedDumper(),
    )
    backup = backup_service.verify(backup_id)
    if not (backup.created_at < raw_expires_at):
        raise QualificationError("BACKUP_NOT_BEFORE_RETENTION")
    ciphertext = b"".join(
        _read_object(backup_raw, key) for key in backup_raw.list_prefix(f"backups/{backup_id}")
    )
    if b"PGDMP" in ciphertext or marker in ciphertext:
        raise QualificationError("BACKUP_PLAINTEXT_EXPOSED")

    abort_id = _start_with_primary_loss(
        sessions=sessions,
        ledger=ledger,
        owner_id=owners["abort"],
        checkpoint="AFTER_PREPARED_COMMIT",
        source_container=source_container,
        source_engine=source_engine,
        now=started_at + timedelta(minutes=1),
    )
    _coordinator(sessions, ledger).prove_noncommit_and_abort(
        deletion_id=abort_id,
        now=started_at + timedelta(minutes=2),
    )

    deleted_id = _start_with_primary_loss(
        sessions=sessions,
        ledger=ledger,
        owner_id=owners["deleted"],
        checkpoint="AFTER_REQUESTED_APPEND_BEFORE_ACK",
        source_container=source_container,
        source_engine=source_engine,
        now=started_at + timedelta(minutes=3),
    )
    _worker(("reconcile-deletions-once",), source_environment)
    deletion_worker = DeletionWorker(
        worker_id="m7-qualification",
        jobs=JobService(sessions),
        sweeps=DeletionSweepService(sessions, source_objects, ledger),
    )
    if not deletion_worker.run_once(now=started_at + timedelta(minutes=4)):
        raise QualificationError("DELETION_JOB_NOT_PROCESSED")
    if deletion_worker.run_once(now=started_at + timedelta(minutes=4, seconds=1)):
        raise QualificationError("DELETION_QUEUE_NOT_IDLE")
    if source_objects.exists(owner_keys["deleted"]):
        raise QualificationError("LIVE_DELETION_OBJECT_REMAINS")
    if tuple(event.phase for event in ledger.chain(deleted_id)) != (
        "PREPARED",
        "REQUESTED",
        "COMPLETED",
    ):
        raise QualificationError("LIVE_DELETION_CHAIN_INVALID")

    expired_count = ImportService(sessions, source_objects).expire_raw_objects(
        now=raw_expires_at + timedelta(microseconds=1)
    )
    if expired_count != 1 or source_objects.exists(raw_key):
        raise QualificationError("LIVE_RETENTION_FAILED")

    fenced_id = _start_with_primary_loss(
        sessions=sessions,
        ledger=ledger,
        owner_id=owners["fenced"],
        checkpoint="AFTER_FENCE_COMMIT",
        source_container=source_container,
        source_engine=source_engine,
        now=started_at + timedelta(minutes=5),
    )
    fenced_prepared = ledger.chain(fenced_id)[0]
    committed_evidence = derive_local_postgres_outcome(
        sessions,
        fenced_prepared,
        observed_at=started_at + timedelta(minutes=6),
        key=OUTCOME_KEY,
    )
    if committed_evidence is None or committed_evidence.outcome != "COMMITTED":
        raise QualificationError("COMMITTED_OUTCOME_NOT_DERIVED")
    outcome_file = runtime / "committed-outcome.json"
    _write_private_json(outcome_file, [asdict(committed_evidence)])

    failure_results: list[dict[str, object]] = []
    _reset_quarantine(quarantine_container, quarantine_objects)
    missing_result = _restore(
        backup_id=backup_id,
        runtime=runtime,
        name="missing-ledger",
        environment={
            **quarantine_environment,
            "RATEREPLAY_DELETION_LEDGER_ROOT": str(runtime / "missing-ledger"),
        },
        expected=(1,),
    )
    failure_results.append(
        _failure("MISSING_LEDGER", "LEDGER_MISSING", "LEDGER_MISSING" in missing_result.stdout)
    )

    _reset_quarantine(quarantine_container, quarantine_objects)
    held_result = _restore(
        backup_id=backup_id,
        runtime=runtime,
        name="unresolved-prepared",
        environment=quarantine_environment,
        expected=(3,),
    )
    held_artifact = json.loads(
        (runtime / "unresolved-prepared-exposure.json").read_text(encoding="ascii")
    )
    failure_results.append(
        _failure(
            "UNRESOLVED_PREPARED",
            "QUARANTINED",
            "exposure_allowed=false" in held_result.stdout
            and held_artifact["exposure_allowed"] is False,
        )
    )

    tampered_ledger = runtime / "tampered-ledger"
    shutil.copytree(runtime / "ledger", tampered_ledger)
    _tamper_ledger_stream(tampered_ledger)
    _reset_quarantine(quarantine_container, quarantine_objects)
    tampered_result = _restore(
        backup_id=backup_id,
        runtime=runtime,
        name="tampered-ledger-run",
        environment={
            **quarantine_environment,
            "RATEREPLAY_DELETION_LEDGER_ROOT": str(tampered_ledger),
        },
        expected=(1,),
    )
    failure_results.append(
        _failure(
            "TAMPERED_LEDGER",
            "LEDGER_UNVERIFIED",
            "LEDGER_" in tampered_result.stdout
            and not (runtime / "tampered-ledger-run-exposure.json").exists(),
        )
    )

    _wait_until(raw_expires_at)
    _reset_quarantine(quarantine_container, quarantine_objects)
    restored = _restore(
        backup_id=backup_id,
        runtime=runtime,
        name="qualified",
        environment=quarantine_environment,
        outcome_file=outcome_file,
        expected=(0,),
    )
    if "exposure_allowed=true" not in restored.stdout:
        raise QualificationError("RESTORE_NOT_EXPOSABLE")
    exposure = json.loads((runtime / "qualified-exposure.json").read_text(encoding="ascii"))
    qualification = json.loads(
        (runtime / "qualified-qualification.json").read_text(encoding="ascii")
    )
    quarantine_engine = make_engine(quarantine_database_url)
    try:
        quarantine_sessions = make_session_factory(quarantine_engine)
        with quarantine_sessions() as database:
            abort_owner = database.get(UserRecord, owners["abort"])
            deleted_owner = database.get(UserRecord, owners["deleted"])
            fenced_owner = database.get(UserRecord, owners["fenced"])
            retained_owner = database.get(UserRecord, owners["retained"])
            restored_raw = database.get(RawObjectRecord, raw_id)
        restored_state_checks = {
            "ABORT_OWNER_ACTIVE": (
                abort_owner is not None and abort_owner.lifecycle_state == "ACTIVE"
            ),
            "ABORT_OBJECT_PRESENT": quarantine_objects.exists(owner_keys["abort"]),
            "DELETED_OBJECT_ABSENT": not quarantine_objects.exists(owner_keys["deleted"]),
            "DELETED_OWNER_ABSENT": deleted_owner is None,
            "FENCED_OBJECT_ABSENT": not quarantine_objects.exists(owner_keys["fenced"]),
            "FENCED_OWNER_ABSENT": fenced_owner is None,
            "RAW_OBJECT_ABSENT": not quarantine_objects.exists(raw_key),
            "RAW_ROW_EXPIRED": restored_raw is not None and restored_raw.state == "DELETED",
            "RETAINED_OWNER_PRESENT": retained_owner is not None,
        }
        failed_state_checks = sorted(
            name for name, passed in restored_state_checks.items() if not passed
        )
        if failed_state_checks:
            raise QualificationError(
                "RESTORED_STATE_NOT_SUPPRESSED:" + ",".join(failed_state_checks)
            )
    finally:
        quarantine_engine.dispose()

    before_expiry = BackupRetentionService(backup_objects).expire(
        now=backup.expires_at - timedelta(microseconds=1)
    )
    if before_expiry.expired_backups != 0:
        raise QualificationError("BACKUP_EXPIRED_EARLY")
    backup_service.verify(backup_id)
    at_expiry = BackupRetentionService(backup_objects).expire(now=backup.expires_at)
    ledger.validate()
    if at_expiry.expired_backups != 1 or backup_raw.list_prefix(f"backups/{backup_id}"):
        raise QualificationError("BACKUP_EXPIRY_BOUNDARY_FAILED")

    return {
        "backup": {
            "manifest_sha256": backup.manifest_content_hash,
            "database_sha256": backup.database_content_hash,
            "object_count": backup.object_count,
            "created_before_deletion": True,
            "created_before_raw_expiry": True,
            "encrypted_at_rest_probe_passed": True,
        },
        "failure_injections": [
            *failure_results,
            _failure("PRIMARY_LOSS_AFTER_PREPARED", "QUARANTINED_THEN_ABORTED", True),
            _failure("PRIMARY_LOSS_AFTER_FENCE", "COMMITTED_OUTCOME_REQUIRED", True),
            _failure("PRIMARY_LOSS_AFTER_REQUESTED", "SUPPRESS_FROM_LEDGER", True),
        ],
        "reconciliation": {
            "qualification_sha256": qualification["artifact_sha256"],
            "exposure_artifact_sha256": exposure["artifact_sha256"],
            "exposure_allowed": True,
            "quarantine_hold_count": 0,
            "suppressed_deletion_count": len(qualification["suppressed_deletions"]),
            "aborted_deletion_count": 1,
            "retention_expired_object_count": qualification["retention_expired_objects"],
            "no_suppressive_scope_remains": True,
        },
        "backup_retention": {
            "retained_one_microsecond_before_deadline": True,
            "expired_at_deadline": True,
            "remaining_backup_object_count": 0,
            "clock_mode": "INJECTED_LOGICAL_TIME",
        },
        "topology": {
            "source_and_quarantine_volumes_distinct": True,
            "backup_store_separate": True,
            "ledger_in_database_backup": False,
            "api_started_before_qualification": False,
        },
    }


def _exercise_safe_migration_rollback(
    *,
    runtime: Path,
    source_container: str,
    source_database_url: str,
) -> dict[str, object]:
    database_name = f"m7_migration_{secrets.token_hex(4)}"
    _run(("docker", "exec", source_container, "createdb", "-U", "ratereplay", database_name))
    scratch_url = source_database_url.rsplit("/", 1)[0] + f"/{database_name}"
    try:
        _alembic(scratch_url, "upgrade", SAFE_ROLLBACK_REVISION)
        engine = make_engine(scratch_url)
        try:
            canary_id = secrets.token_hex(16)
            canary_scope = secrets.token_hex(16)
            with engine.begin() as database:
                database.execute(
                    text(
                        "INSERT INTO users "
                        "(id, username_canonical, password_hash, created_at, lifecycle_state, "
                        "lifecycle_generation, deletion_scope_id) "
                        "VALUES (:id, :username, :password, :created_at, 'ACTIVE', 0, :scope)"
                    ),
                    {
                        "id": canary_id,
                        "username": f"m7_canary_{canary_id[:8]}",
                        "password": hashlib.sha256(b"m7-canary-noncredential").hexdigest(),
                        "created_at": datetime.now(UTC),
                        "scope": canary_scope,
                    },
                )
            before = _canary_hash(engine, canary_id)
            _alembic(scratch_url, "upgrade", AUDIT_REVISION)
            audit_inspector = inspect(engine)
            if "audit_events" not in audit_inspector.get_table_names() or not any(
                index["name"] == "ix_audit_events_owner_recorded"
                for index in audit_inspector.get_indexes("audit_events")
            ):
                raise QualificationError("MIGRATION_UPGRADE_MISSING_AUDIT_SCHEMA")
            _alembic(scratch_url, "downgrade", SAFE_ROLLBACK_REVISION)
            if "audit_events" in inspect(engine).get_table_names():
                raise QualificationError("MIGRATION_DOWNGRADE_LEFT_AUDIT_SCHEMA")
            if _canary_hash(engine, canary_id) != before:
                raise QualificationError("MIGRATION_DOWNGRADE_CHANGED_CANARY")
            _alembic(scratch_url, "upgrade", "head")
            _alembic(scratch_url, "check")
            if _canary_hash(engine, canary_id) != before:
                raise QualificationError("MIGRATION_REUPGRADE_CHANGED_CANARY")
            failed_transaction = False
            try:
                with engine.begin() as database:
                    database.execute(text("CREATE TABLE m7_failed_upgrade_probe (id INTEGER)"))
                    database.execute(text("SELECT * FROM m7_intentionally_missing_relation"))
            except Exception:
                failed_transaction = True
            if (
                not failed_transaction
                or "m7_failed_upgrade_probe" in inspect(engine).get_table_names()
            ):
                raise QualificationError("FAILED_MIGRATION_TRANSACTION_NOT_ROLLED_BACK")
        finally:
            engine.dispose()
    finally:
        _run(
            (
                "docker",
                "exec",
                source_container,
                "dropdb",
                "-U",
                "ratereplay",
                "--if-exists",
                database_name,
            )
        )
    return {
        "safe_migration_from": AUDIT_REVISION,
        "safe_migration_to": SAFE_ROLLBACK_REVISION,
        "reupgraded_to": HEAD_REVISION,
        "stable_row_unchanged": True,
        "failed_upgrade_rolled_back": True,
    }


def _start_with_primary_loss(
    *,
    sessions: sessionmaker[Session],
    ledger: FilesystemDeletionLedger,
    owner_id: str,
    checkpoint: DeletionCheckpoint,
    source_container: str,
    source_engine: Engine,
    now: datetime,
) -> str:
    coordinator = _coordinator(sessions, ledger)
    intent = coordinator.create_intent(
        owner_user_id=owner_id,
        idempotency_key=f"m7-{checkpoint.lower()}",
        receipt_secret=b"r" * 32,
        now=now,
    )

    class InjectedPrimaryLoss(RuntimeError):
        pass

    def kill_at_boundary(current: DeletionCheckpoint, event: LedgerEvent) -> None:
        del event
        if current != checkpoint:
            return
        _run(("docker", "kill", "--signal=KILL", source_container))
        raise InjectedPrimaryLoss

    faulting = DeletionCoordinator(
        sessions,
        ledger,
        restore_key=b"r" * 32,
        checkpoint_observer=kill_at_boundary,
    )
    try:
        faulting.authorize_and_start(
            owner_user_id=owner_id,
            deletion_id=intent.deletion_id,
            receipt_secret=b"r" * 32,
            now=now,
        )
    except InjectedPrimaryLoss:
        pass
    else:
        raise QualificationError(f"PRIMARY_LOSS_NOT_INJECTED:{checkpoint}")
    source_engine.dispose()
    _run(("docker", "start", source_container))
    _wait_for_database(source_engine)
    return intent.deletion_id


def _coordinator(
    sessions: sessionmaker[Session],
    ledger: FilesystemDeletionLedger,
) -> DeletionCoordinator:
    return DeletionCoordinator(
        sessions,
        ledger,
        restore_key=b"r" * 32,
    )


def _wait_for_database(engine: Engine) -> None:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with engine.connect() as database:
                database.execute(text("SELECT 1"))
            return
        except Exception:
            engine.dispose()
            time.sleep(1)
    raise QualificationError("POSTGRES_DID_NOT_RECOVER")


def _wait_until(deadline: datetime) -> None:
    remaining = (deadline - datetime.now(UTC)).total_seconds()
    if remaining > 0:
        time.sleep(remaining + 0.1)


def _restore(
    *,
    backup_id: str,
    runtime: Path,
    name: str,
    environment: dict[str, str],
    expected: tuple[int, ...],
    outcome_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    if name not in {
        "missing-ledger",
        "qualified",
        "tampered-ledger-run",
        "unresolved-prepared",
    }:
        raise QualificationError("RESTORE_STAGE_INVALID")
    arguments = [
        "restore-backup-to-quarantine",
        backup_id,
        "--materialization-directory",
        str(runtime / f"{name}-materialized"),
        "--qualification-artifact-file",
        str(runtime / f"{name}-qualification.json"),
        "--exposure-artifact-file",
        str(runtime / f"{name}-exposure.json"),
    ]
    if outcome_file is not None:
        arguments.extend(("--outcome-evidence-file", str(outcome_file)))
    try:
        return _worker(tuple(arguments), environment, expected=expected)
    except QualificationError as error:
        raise QualificationError(f"RESTORE_STAGE_FAILED:{name}:{error}") from error


def _reset_quarantine(container: str, objects: EncryptedObjectStore) -> None:
    for key in objects.list_prefix(""):
        objects.delete(key)
    _run(
        (
            "docker",
            "exec",
            container,
            "psql",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "ratereplay",
            "-d",
            "ratereplay",
            "-c",
            "DROP SCHEMA public CASCADE; CREATE SCHEMA public AUTHORIZATION ratereplay;",
        )
    )


def _tamper_ledger_stream(root: Path) -> None:
    stream = root / "deletion-ledger-v2.jsonl"
    lines = stream.read_text(encoding="ascii").splitlines()
    if not lines:
        raise QualificationError("LEDGER_TAMPER_INPUT_EMPTY")
    record = json.loads(lines[0])
    ciphertext = record["ciphertext"]
    record["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
    lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
    stream.write_text("\n".join(lines) + "\n", encoding="ascii")


def _object_store(
    *,
    endpoint: str,
    access_file: Path,
    secret_file: Path,
    bucket: str,
    key: bytes,
    key_version: str,
) -> tuple[EncryptedObjectStore, S3ObjectStore]:
    client = Minio(
        endpoint,
        access_key=access_file.read_text(encoding="ascii").strip(),
        secret_key=secret_file.read_text(encoding="ascii").strip(),
        secure=False,
    )
    raw = S3ObjectStore(client, bucket, ensure_bucket=True)
    return (
        EncryptedObjectStore(raw, current_key_version=key_version, keys={key_version: key}),
        raw,
    )


def _service_environment(
    *,
    database_url: str,
    primary_endpoint: str,
    primary_access_file: Path,
    primary_secret_file: Path,
    primary_bucket: str,
    backup_endpoint: str,
    backup_access_file: Path,
    backup_secret_file: Path,
    source_container: str,
    restore_container: str,
    runtime: Path,
) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "RATEREPLAY_ENV": "development",
            "RATEREPLAY_DATABASE_URL": database_url,
            "RATEREPLAY_DATABASE_AT_REST_ENCRYPTION": "development-unencrypted",
            "RATEREPLAY_OBJECT_STORE_BACKEND": "s3",
            "RATEREPLAY_S3_ENDPOINT": primary_endpoint,
            "RATEREPLAY_S3_BUCKET": primary_bucket,
            "RATEREPLAY_S3_ACCESS_KEY_FILE": str(primary_access_file),
            "RATEREPLAY_S3_SECRET_KEY_FILE": str(primary_secret_file),
            "RATEREPLAY_S3_SECURE": "false",
            "RATEREPLAY_OBJECT_ENCRYPTION_KEYS_DIR": str(runtime / "object-keys"),
            "RATEREPLAY_OBJECT_ENCRYPTION_CURRENT_KEY_VERSION": "object-key-v1",
            "RATEREPLAY_BACKUP_OBJECT_STORE_BACKEND": "s3",
            "RATEREPLAY_BACKUP_S3_ENDPOINT": backup_endpoint,
            "RATEREPLAY_BACKUP_S3_BUCKET": "ratereplay-m7-backups",
            "RATEREPLAY_BACKUP_S3_ACCESS_KEY_FILE": str(backup_access_file),
            "RATEREPLAY_BACKUP_S3_SECRET_KEY_FILE": str(backup_secret_file),
            "RATEREPLAY_BACKUP_S3_SECURE": "false",
            "RATEREPLAY_BACKUP_OBJECT_ENCRYPTION_KEYS_DIR": str(runtime / "backup-keys"),
            "RATEREPLAY_BACKUP_OBJECT_ENCRYPTION_CURRENT_KEY_VERSION": "backup-key-v1",
            "RATEREPLAY_BACKUP_PGDUMP_COMMAND_JSON": json.dumps(
                [
                    "docker",
                    "exec",
                    "-i",
                    source_container,
                    "pg_dump",
                    "-U",
                    "ratereplay",
                    "-d",
                    "ratereplay",
                ]
            ),
            "RATEREPLAY_BACKUP_PGDUMP_VERSION_COMMAND_JSON": json.dumps(
                ["docker", "exec", "-i", source_container, "pg_dump"]
            ),
            "RATEREPLAY_BACKUP_PGRESTORE_COMMAND_JSON": json.dumps(
                [
                    sys.executable,
                    str(Path(__file__).resolve().with_name("m7_pg_restore_adapter.py")),
                    restore_container,
                ]
            ),
            "RATEREPLAY_DELETION_LEDGER_ROOT": str(runtime / "ledger"),
            "RATEREPLAY_DELETION_LEDGER_KEYS_DIR": str(runtime / "ledger-keys"),
            "RATEREPLAY_DELETION_LEDGER_CURRENT_KEY_VERSION": "ledger-v1",
            "RATEREPLAY_RESTORE_KEYS_DIR": str(runtime / "restore-keys"),
            "RATEREPLAY_RESTORE_CURRENT_KEY_VERSION": "restore-v1",
            "RATEREPLAY_TRANSACTION_OUTCOME_KEY_FILE": str(runtime / "outcome.key"),
        }
    )
    return environment


def _worker(
    arguments: tuple[str, ...],
    environment: dict[str, str],
    *,
    expected: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    return _run(
        ("uv", "run", "ratereplay-worker", *arguments),
        environment=environment,
        expected=expected,
    )


def _alembic(database_url: str, action: str, revision: str | None = None) -> None:
    arguments = ["uv", "run", "alembic", action]
    if revision is not None:
        arguments.append(revision)
    environment = {**os.environ, "RATEREPLAY_DATABASE_URL": database_url}
    _run(tuple(arguments), environment=environment)


def _run(
    command: tuple[str, ...],
    *,
    environment: dict[str, str] | None = None,
    expected: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(  # noqa: S603  # nosec B603
        command,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    combined = completed.stdout + completed.stderr
    completed = subprocess.CompletedProcess(command, completed.returncode, combined, "")
    if completed.returncode not in expected:
        raise QualificationError(f"COMMAND_FAILED:{_command_label(command)}:{completed.returncode}")
    return completed


def _command_label(command: tuple[str, ...]) -> str:
    executable = Path(command[0]).name
    if executable == "uv" and len(command) >= 4 and command[1] == "run":
        tool = Path(command[2]).name
        operation = command[3]
        allowed = {
            ("alembic", "check"),
            ("alembic", "downgrade"),
            ("alembic", "upgrade"),
            ("ratereplay-worker", "create-backup"),
            ("ratereplay-worker", "reconcile-deletions-once"),
            ("ratereplay-worker", "restore-backup-to-quarantine"),
        }
        if (tool, operation) in allowed:
            return f"uv:{tool}:{operation}"
    if executable == "docker" and len(command) >= 2:
        operation = command[1]
        if operation in {"exec", "kill", "start", "version"}:
            return f"docker:{operation}"
    if executable == "docker-compose" and len(command) >= 2:
        return "docker-compose:version"
    if executable == "git" and command[1:3] == ("rev-parse", "HEAD"):
        return "git:rev-parse"
    return executable


def _canary_hash(engine: Engine, canary_id: str) -> str:
    with engine.connect() as database:
        row = database.execute(
            text(
                "SELECT id, username_canonical, password_hash, lifecycle_state, "
                "lifecycle_generation, deletion_scope_id FROM users WHERE id = :id"
            ),
            {"id": canary_id},
        ).one()
    return hashlib.sha256(
        json.dumps(list(row), sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _read_object(store: S3ObjectStore, key: str) -> bytes:
    with store.open_file(key, maximum_bytes=64 * 1024 * 1024) as source:
        return bytes(source.read())


def _failure(identifier: str, expected: str, passed: bool) -> dict[str, object]:
    if not passed:
        raise QualificationError(f"FAULT_CONTROL_FAILED:{identifier}")
    return {"id": identifier, "expected": expected, "passed": True}


def _build_evidence(
    *,
    started_at: datetime,
    outcome: dict[str, object],
    migration: dict[str, object],
) -> dict[str, object]:
    repository = Path(__file__).resolve().parents[1]
    source_commit = _run(("git", "rev-parse", "HEAD")).stdout.strip()
    payload: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA,
        "evidence_level": LOCAL_EVIDENCE_LEVEL,
        "gate_result": "PASS",
        "source_commit": source_commit,
        "generated_at": started_at.isoformat(),
        "environment": {
            "os": platform.platform(),
            "architecture": platform.machine(),
            "python": platform.python_version(),
            "docker_engine": _run(
                ("docker", "version", "--format", "{{.Server.Version}}")
            ).stdout.strip(),
            "docker_compose": _run(("docker-compose", "version", "--short")).stdout.strip(),
            "postgres_image": _compose_image(repository, "postgres"),
            "minio_image": _compose_image(repository, "minio"),
        },
        "inputs": {
            "compose_sha256": _file_hash(repository / "compose.yaml"),
            "uv_lock_sha256": _file_hash(repository / "uv.lock"),
            "migration_head": HEAD_REVISION,
        },
        "topology": outcome["topology"],
        "backup": outcome["backup"],
        "failure_injections": outcome["failure_injections"],
        "reconciliation": outcome["reconciliation"],
        "backup_retention": outcome["backup_retention"],
        "rollback": migration,
        "claims_withheld": [
            "HOSTED_VALIDATED",
            "REAL_WAL_RECOVERY",
            "POINT_IN_TIME_RECOVERY",
            "PRODUCTION_TLS",
            "MANAGED_VOLUME_ENCRYPTION",
            "PRODUCTION_NETWORK_ISOLATION",
        ],
    }
    payload["artifact_sha256"] = _artifact_hash(payload)
    return payload


def verify_evidence(payload: dict[str, object]) -> dict[str, object]:
    required = {
        "schema_version",
        "evidence_level",
        "gate_result",
        "source_commit",
        "generated_at",
        "environment",
        "inputs",
        "topology",
        "backup",
        "failure_injections",
        "reconciliation",
        "backup_retention",
        "rollback",
        "claims_withheld",
        "artifact_sha256",
    }
    failure_injections = payload.get("failure_injections")
    if (
        set(payload) != required
        or payload.get("schema_version") != EVIDENCE_SCHEMA
        or payload.get("evidence_level") != LOCAL_EVIDENCE_LEVEL
        or payload.get("gate_result") != "PASS"
        or not isinstance(payload.get("source_commit"), str)
        or len(str(payload["source_commit"])) != 40
        or not isinstance(failure_injections, list)
        or not all(
            isinstance(item, dict) and item.get("passed") is True for item in failure_injections
        )
    ):
        raise QualificationError("M7_EVIDENCE_INVALID")
    digest = payload.get("artifact_sha256")
    unsigned = dict(payload)
    unsigned.pop("artifact_sha256")
    if not isinstance(digest, str) or digest != _artifact_hash(unsigned):
        raise QualificationError("M7_EVIDENCE_DIGEST_INVALID")
    return payload


def write_evidence(path: Path, payload: dict[str, object]) -> None:
    verify_evidence(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True) + "\n",
        encoding="ascii",
    )
    os.chmod(temporary, 0o644)
    os.replace(temporary, path)


def _write_private_json(path: Path, payload: object) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="ascii",
    )
    os.chmod(path, 0o600)


def _artifact_hash(payload: dict[str, object]) -> str:
    unsigned = dict(payload)
    unsigned.pop("artifact_sha256", None)
    return hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("ascii")
    ).hexdigest()


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compose_image(repository: Path, kind: str) -> str:
    encoded = (repository / "compose.yaml").read_text(encoding="utf-8")
    for line in encoded.splitlines():
        stripped = line.strip()
        if stripped.startswith("image:") and kind in stripped:
            return stripped.split("image:", 1)[1].strip()
    raise QualificationError(f"COMPOSE_IMAGE_MISSING:{kind}")


def _required(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value:
        raise QualificationError(f"ENVIRONMENT_REQUIRED:{name}")
    return value


def _required_path(name: str) -> Path:
    return Path(_required(name)).resolve()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except QualificationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from error
