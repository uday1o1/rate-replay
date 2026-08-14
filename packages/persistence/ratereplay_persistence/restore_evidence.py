"""Content-addressed binding between one restored instance and its exposure decision."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from ratereplay_persistence.backups import MaterializedBackup
from ratereplay_persistence.restore import (
    RestoreQualification,
    RestoreQualificationError,
    verify_restore_qualification_artifact,
)


@dataclass(frozen=True, slots=True)
class RestoreExposureArtifact:
    schema_version: Literal["restore-exposure-artifact-v1"]
    restore_instance_id: str
    backup_id: str
    backup_manifest_sha256: str
    database_dump_sha256: str
    restored_object_set_sha256: str
    restored_object_count: int
    deletion_ledger_head_sha256: str
    database_revision: str
    qualification_artifact_sha256: str
    exposure_allowed: bool
    bound_at: str
    artifact_sha256: str

    def artifact_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":")) + "\n"


def bind_restore_exposure(
    materialized: MaterializedBackup,
    qualification: RestoreQualification,
    *,
    deletion_ledger_root: Path,
    database_revision: str,
    bound_at: datetime,
) -> RestoreExposureArtifact:
    if bound_at.tzinfo is None:
        raise TypeError("Restore exposure binding timestamp must be timezone-aware")
    if not database_revision or len(database_revision) > 128:
        raise ValueError("Restored database revision is invalid")
    verify_restore_qualification_artifact(json.loads(qualification.artifact_json()))
    head_path = deletion_ledger_root.resolve() / "deletion-ledger-head-v2.json"
    try:
        ledger_head_sha256 = hashlib.sha256(head_path.read_bytes()).hexdigest()
    except OSError as error:
        raise RestoreQualificationError(
            "LEDGER_UNVERIFIED",
            "Restore exposure binding cannot read the verified ledger head",
        ) from error
    object_payload = [
        {
            "source_key_sha256": hashlib.sha256(entry.source_key.encode("utf-8")).hexdigest(),
            "content_hash": entry.content_hash,
            "size_bytes": entry.size_bytes,
        }
        for entry in sorted(materialized.objects, key=lambda item: item.source_key)
    ]
    object_set_sha256 = hashlib.sha256(
        b"RateReplay.RestoredObjectSet.v1\x00" + _canonical(object_payload)
    ).hexdigest()
    payload: dict[str, object] = {
        "schema_version": "restore-exposure-artifact-v1",
        "restore_instance_id": secrets.token_hex(16),
        "backup_id": materialized.backup_id,
        "backup_manifest_sha256": materialized.manifest_content_hash,
        "database_dump_sha256": materialized.database_content_hash,
        "restored_object_set_sha256": object_set_sha256,
        "restored_object_count": len(materialized.objects),
        "deletion_ledger_head_sha256": ledger_head_sha256,
        "database_revision": database_revision,
        "qualification_artifact_sha256": qualification.artifact_sha256,
        "exposure_allowed": qualification.exposure_allowed,
        "bound_at": bound_at.isoformat(),
    }
    return RestoreExposureArtifact(
        schema_version="restore-exposure-artifact-v1",
        restore_instance_id=str(payload["restore_instance_id"]),
        backup_id=materialized.backup_id,
        backup_manifest_sha256=materialized.manifest_content_hash,
        database_dump_sha256=materialized.database_content_hash,
        restored_object_set_sha256=object_set_sha256,
        restored_object_count=len(materialized.objects),
        deletion_ledger_head_sha256=ledger_head_sha256,
        database_revision=database_revision,
        qualification_artifact_sha256=qualification.artifact_sha256,
        exposure_allowed=qualification.exposure_allowed,
        bound_at=bound_at.isoformat(),
        artifact_sha256=hashlib.sha256(
            b"RateReplay.RestoreExposureArtifact.v1\x00" + _canonical(payload)
        ).hexdigest(),
    )


def verify_restore_exposure_artifact(
    payload: Mapping[str, object],
) -> RestoreExposureArtifact:
    required = {
        "schema_version",
        "restore_instance_id",
        "backup_id",
        "backup_manifest_sha256",
        "database_dump_sha256",
        "restored_object_set_sha256",
        "restored_object_count",
        "deletion_ledger_head_sha256",
        "database_revision",
        "qualification_artifact_sha256",
        "exposure_allowed",
        "bound_at",
        "artifact_sha256",
    }
    try:
        if (
            set(payload) != required
            or payload.get("schema_version") != "restore-exposure-artifact-v1"
        ):
            raise TypeError
        strings = {
            key: _required_text(payload, key)
            for key in required - {"restored_object_count", "exposure_allowed"}
        }
        count = payload["restored_object_count"]
        exposure_allowed = payload["exposure_allowed"]
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or not isinstance(exposure_allowed, bool)
            or len(strings["restore_instance_id"]) != 32
            or any(
                len(strings[key]) != 64
                for key in (
                    "backup_manifest_sha256",
                    "database_dump_sha256",
                    "restored_object_set_sha256",
                    "deletion_ledger_head_sha256",
                    "qualification_artifact_sha256",
                    "artifact_sha256",
                )
            )
            or datetime.fromisoformat(strings["bound_at"]).tzinfo is None
        ):
            raise ValueError
        unsigned = dict(payload)
        digest = str(unsigned.pop("artifact_sha256"))
        expected = hashlib.sha256(
            b"RateReplay.RestoreExposureArtifact.v1\x00" + _canonical(unsigned)
        ).hexdigest()
        if not hmac.compare_digest(digest, expected):
            raise ValueError
    except (KeyError, TypeError, ValueError) as error:
        raise RestoreQualificationError(
            "RESTORE_EXPOSURE_ARTIFACT_INVALID",
            "Restore exposure artifact is invalid",
        ) from error
    return RestoreExposureArtifact(
        schema_version="restore-exposure-artifact-v1",
        restore_instance_id=strings["restore_instance_id"],
        backup_id=strings["backup_id"],
        backup_manifest_sha256=strings["backup_manifest_sha256"],
        database_dump_sha256=strings["database_dump_sha256"],
        restored_object_set_sha256=strings["restored_object_set_sha256"],
        restored_object_count=count,
        deletion_ledger_head_sha256=strings["deletion_ledger_head_sha256"],
        database_revision=strings["database_revision"],
        qualification_artifact_sha256=strings["qualification_artifact_sha256"],
        exposure_allowed=exposure_allowed,
        bound_at=strings["bound_at"],
        artifact_sha256=strings["artifact_sha256"],
    )


def write_restore_exposure_artifact(path: Path, artifact: RestoreExposureArtifact) -> None:
    verified = verify_restore_exposure_artifact(json.loads(artifact.artifact_json()))
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = destination.parent / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, verified.artifact_json().encode("ascii"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _required_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise TypeError
    return value


def _canonical(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "ascii"
    )
