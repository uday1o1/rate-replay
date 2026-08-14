"""Owner-scoped import submission, publication, confirmation, and retention."""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import BinaryIO, Final

from ratereplay_domain.profile_hash import CanonicalFinding, CanonicalReading, FlowDirection
from ratereplay_ingestion.normalize import NormalizedDraft, confirm_draft
from ratereplay_ingestion.simulated import LockedSimulatedProfile
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.models import (
    ImportFindingRecord,
    ImportReadingRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    RawObjectRecord,
    UserRecord,
)
from ratereplay_persistence.object_store import FilesystemObjectStore, StoredObject

IMPORT_ROUTE: Final = "POST:/v1/imports"
IMPORT_REQUEST_SCHEMA: Final = "import-request-v1"
SIMULATED_IMPORT_ROUTE: Final = "POST:/v1/imports/built-in-simulated-profile"
SIMULATED_IMPORT_REQUEST_SCHEMA: Final = "built-in-simulated-import-v1"
RAW_RETENTION: Final = timedelta(hours=24)
IDEMPOTENCY_RETENTION: Final = timedelta(hours=24)


class ImportServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ImportSubmission:
    import_id: str
    job_id: str
    repeated: bool


@dataclass(frozen=True, slots=True)
class SimulatedProfileInstallation:
    profile: ProfileVersionRecord
    repeated: bool


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _payload_hash(adapter: str, raw_hash: str) -> str:
    return hashlib.sha256(
        b"RateReplay.ImportRequest.v1\x00" + adapter.encode() + b"\x00" + raw_hash.encode()
    ).hexdigest()


def _maximum_bytes(adapter: str) -> int:
    limits = {"ESPI_XML": 10 * 1024 * 1024, "PGE_CSV": 20 * 1024 * 1024}
    try:
        return limits[adapter]
    except KeyError as error:
        raise ImportServiceError(
            "UNSUPPORTED_ADAPTER", "Import adapter is not supported"
        ) from error


class ImportService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        object_store: FilesystemObjectStore,
    ) -> None:
        self._session_factory = session_factory
        self._objects = object_store

    def submit(
        self,
        *,
        owner_user_id: str,
        adapter: str,
        idempotency_key: str,
        source: BinaryIO,
        now: datetime,
    ) -> ImportSubmission:
        if not 8 <= len(idempotency_key) <= 128:
            raise ImportServiceError(
                "INVALID_IDEMPOTENCY_KEY", "Idempotency key must contain 8 to 128 characters"
            )
        maximum_bytes = _maximum_bytes(adapter)
        raw_digest = hashlib.sha256()
        size = 0
        while chunk := source.read(64 * 1024):
            size += len(chunk)
            if size > maximum_bytes:
                raise ImportServiceError("OVERSIZED_FILE", "Upload exceeds the adapter size limit")
            raw_digest.update(chunk)
        if size == 0:
            raise ImportServiceError("EMPTY_FILE", "Upload is empty")
        source.seek(0)
        raw_hash = raw_digest.hexdigest()
        payload_hash = _payload_hash(adapter, raw_hash)

        with self._session_factory() as database:
            existing = database.scalar(
                select(OperationRequestRecord).where(
                    OperationRequestRecord.owner_user_id == owner_user_id,
                    OperationRequestRecord.route_id == IMPORT_ROUTE,
                    OperationRequestRecord.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                if existing.canonical_payload_hash != payload_hash:
                    raise ImportServiceError(
                        "IDEMPOTENCY_KEY_REUSED", "Idempotency key is bound to another request"
                    )
                job_id = database.scalar(
                    select(JobRecord.id).where(JobRecord.import_id == existing.operation_id)
                )
                if job_id is None:
                    raise ImportServiceError(
                        "OPERATION_INCOMPLETE", "Import operation is incomplete"
                    )
                return ImportSubmission(existing.operation_id, job_id, True)
            user = database.get(UserRecord, owner_user_id)
            if user is None or user.lifecycle_state != "ACTIVE":
                raise ImportServiceError("OWNER_NOT_ACTIVE", "Account cannot accept imports")
            account_generation = user.lifecycle_generation

        import_id = secrets.token_hex(16)
        job_id = secrets.token_hex(16)
        object_key = f"owners/{owner_user_id}/imports/{import_id}/raw"
        try:
            stored = self._objects.put_file(object_key, source, maximum_bytes=maximum_bytes)
            if stored.content_hash != raw_hash or stored.size_bytes != size:
                raise ImportServiceError(
                    "UPLOAD_HASH_MISMATCH", "Upload changed while being stored"
                )
            submission = self._record_submission(
                owner_user_id=owner_user_id,
                account_generation=account_generation,
                adapter=adapter,
                idempotency_key=idempotency_key,
                payload_hash=payload_hash,
                import_id=import_id,
                job_id=job_id,
                object_key=object_key,
                stored=stored,
                now=now,
            )
            if submission.repeated:
                self._objects.delete(object_key)
            return submission
        except Exception:
            self._objects.delete(object_key)
            raise

    def install_simulated_profile(
        self,
        *,
        owner_user_id: str,
        idempotency_key: str,
        artifact: LockedSimulatedProfile,
        now: datetime,
    ) -> SimulatedProfileInstallation:
        """Install the frozen repository profile as owner-scoped immutable data."""

        if not 8 <= len(idempotency_key) <= 128:
            raise ImportServiceError(
                "INVALID_IDEMPOTENCY_KEY", "Idempotency key must contain 8 to 128 characters"
            )
        payload_hash = hashlib.sha256(
            b"RateReplay.BuiltInSimulatedImport.v1\x00" + artifact.artifact_sha256.encode("ascii")
        ).hexdigest()
        profile_hash = artifact.content.sha256()
        resolved_now = now.astimezone(UTC)
        for _attempt in range(3):
            try:
                with self._session_factory.begin() as database:
                    existing_operation = database.scalar(
                        select(OperationRequestRecord).where(
                            OperationRequestRecord.owner_user_id == owner_user_id,
                            OperationRequestRecord.route_id == SIMULATED_IMPORT_ROUTE,
                            OperationRequestRecord.idempotency_key == idempotency_key,
                        )
                    )
                    if existing_operation is not None:
                        if existing_operation.canonical_payload_hash != payload_hash:
                            raise ImportServiceError(
                                "IDEMPOTENCY_KEY_REUSED",
                                "Idempotency key is bound to another request",
                            )
                        existing_profile = database.scalar(
                            select(ProfileVersionRecord).where(
                                ProfileVersionRecord.id == existing_operation.operation_id,
                                ProfileVersionRecord.owner_user_id == owner_user_id,
                            )
                        )
                        if existing_profile is None:
                            raise ImportServiceError(
                                "OPERATION_INCOMPLETE",
                                "Built-in simulated import is incomplete",
                            )
                        return SimulatedProfileInstallation(existing_profile, True)
                    user = database.get(UserRecord, owner_user_id)
                    if user is None or user.lifecycle_state != "ACTIVE":
                        raise ImportServiceError(
                            "OWNER_NOT_ACTIVE", "Account cannot accept imports"
                        )
                    profile = database.scalar(
                        select(ProfileVersionRecord).where(
                            ProfileVersionRecord.owner_user_id == owner_user_id,
                            ProfileVersionRecord.content_hash == profile_hash,
                        )
                    )
                    repeated = profile is not None
                    if profile is None:
                        import_id = secrets.token_hex(16)
                        profile = ProfileVersionRecord(
                            id=secrets.token_hex(16),
                            owner_user_id=owner_user_id,
                            import_id=import_id,
                            content_hash=profile_hash,
                            canonical_content=artifact.content.to_bytes(),
                            billing_period_start_utc_ns=(
                                artifact.content.billing_period_start_utc_ns
                            ),
                            billing_period_end_utc_ns=(artifact.content.billing_period_end_utc_ns),
                            tariff_timezone=artifact.content.tariff_timezone,
                            interval_resolution_seconds=(
                                artifact.content.interval_resolution_seconds
                            ),
                            lifecycle_state="ACTIVE",
                            lifecycle_generation=0,
                            created_at=resolved_now,
                        )
                        database.add(
                            ImportRecord(
                                id=import_id,
                                owner_user_id=owner_user_id,
                                state="CONFIRMED",
                                lifecycle_state="ACTIVE",
                                lifecycle_generation=0,
                                adapter="SIMULATED_PROFILE_V1",
                                raw_content_hash=artifact.artifact_sha256,
                                created_at=resolved_now,
                                published_at=resolved_now,
                                confirmed_at=resolved_now,
                                profile_version_id=profile.id,
                            )
                        )
                        database.add(profile)
                        database.add_all(
                            [
                                _reading_record(import_id, reading)
                                for reading in artifact.content.readings
                            ]
                        )
                    database.add(
                        OperationRequestRecord(
                            id=secrets.token_hex(16),
                            owner_user_id=owner_user_id,
                            route_id=SIMULATED_IMPORT_ROUTE,
                            idempotency_key=idempotency_key,
                            request_schema_version=SIMULATED_IMPORT_REQUEST_SCHEMA,
                            canonical_payload_hash=payload_hash,
                            operation_id=profile.id,
                            created_at=resolved_now,
                            expires_at=resolved_now + IDEMPOTENCY_RETENTION,
                        )
                    )
                    database.flush()
                    return SimulatedProfileInstallation(profile, repeated)
            except IntegrityError:
                continue
        raise ImportServiceError(
            "OPERATION_CONFLICT",
            "Built-in simulated import could not resolve a concurrent request",
        )

    def _record_submission(
        self,
        *,
        owner_user_id: str,
        account_generation: int,
        adapter: str,
        idempotency_key: str,
        payload_hash: str,
        import_id: str,
        job_id: str,
        object_key: str,
        stored: StoredObject,
        now: datetime,
    ) -> ImportSubmission:
        now = now.astimezone(UTC)
        with self._session_factory() as database:
            database.add_all(
                [
                    ImportRecord(
                        id=import_id,
                        owner_user_id=owner_user_id,
                        state="QUEUED",
                        lifecycle_state="ACTIVE",
                        lifecycle_generation=0,
                        adapter=adapter,
                        raw_content_hash=stored.content_hash,
                        created_at=now,
                    ),
                    RawObjectRecord(
                        id=secrets.token_hex(16),
                        owner_user_id=owner_user_id,
                        import_id=import_id,
                        object_key=object_key,
                        content_hash=stored.content_hash,
                        size_bytes=stored.size_bytes,
                        state="AVAILABLE",
                        created_at=now,
                        expires_at=now + RAW_RETENTION,
                    ),
                    OperationRequestRecord(
                        id=secrets.token_hex(16),
                        owner_user_id=owner_user_id,
                        route_id=IMPORT_ROUTE,
                        idempotency_key=idempotency_key,
                        request_schema_version=IMPORT_REQUEST_SCHEMA,
                        canonical_payload_hash=payload_hash,
                        operation_id=import_id,
                        created_at=now,
                        expires_at=now + IDEMPOTENCY_RETENTION,
                    ),
                    JobRecord(
                        id=job_id,
                        owner_user_id=owner_user_id,
                        kind="IMPORT",
                        request_schema_version=IMPORT_REQUEST_SCHEMA,
                        request_hash=payload_hash,
                        scope_mode="ACTIVE_SCOPE",
                        import_id=import_id,
                        captured_account_generation=account_generation,
                        captured_import_generation=0,
                        state="QUEUED",
                        attempt_count=0,
                        max_attempts=3,
                        fencing_generation=0,
                        not_before=now,
                        cancel_requested=False,
                        created_at=now,
                    ),
                ]
            )
            try:
                database.commit()
                return ImportSubmission(import_id, job_id, False)
            except IntegrityError as error:
                database.rollback()
                existing = database.scalar(
                    select(OperationRequestRecord).where(
                        OperationRequestRecord.owner_user_id == owner_user_id,
                        OperationRequestRecord.route_id == IMPORT_ROUTE,
                        OperationRequestRecord.idempotency_key == idempotency_key,
                    )
                )
                if existing is None or existing.canonical_payload_hash != payload_hash:
                    raise ImportServiceError(
                        "IDEMPOTENCY_KEY_REUSED", "Idempotency key is bound to another request"
                    ) from error
                existing_job = database.scalar(
                    select(JobRecord.id).where(JobRecord.import_id == existing.operation_id)
                )
                if existing_job is None:
                    raise ImportServiceError(
                        "OPERATION_INCOMPLETE", "Import operation is incomplete"
                    ) from error
                return ImportSubmission(existing.operation_id, existing_job, True)

    @contextmanager
    def open_raw(self, import_id: str) -> Iterator[tuple[str, BinaryIO]]:
        with self._session_factory() as database:
            record = database.get(ImportRecord, import_id)
            raw = database.scalar(
                select(RawObjectRecord).where(RawObjectRecord.import_id == import_id)
            )
            if record is None or raw is None or raw.state != "AVAILABLE":
                raise ImportServiceError("RAW_OBJECT_MISSING", "Raw import object is unavailable")
            adapter = record.adapter
            object_key = raw.object_key
            expected_hash = raw.content_hash
        maximum_bytes = _maximum_bytes(adapter)
        if self._objects.content_hash(object_key, maximum_bytes=maximum_bytes) != expected_hash:
            raise ImportServiceError("RAW_OBJECT_HASH_MISMATCH", "Raw object integrity failed")
        with self._objects.open_file(object_key, maximum_bytes=maximum_bytes) as payload:
            yield adapter, payload

    def publish_draft(
        self,
        *,
        import_id: str,
        draft: NormalizedDraft,
        worker_id: str,
        fencing_generation: int,
        now: datetime,
    ) -> bool:
        with self._session_factory.begin() as database:
            job = database.scalar(select(JobRecord).where(JobRecord.import_id == import_id))
            record = database.get(ImportRecord, import_id)
            if job is None or record is None:
                return False
            if not (
                job.state == "RUNNING"
                and job.lease_owner == worker_id
                and job.fencing_generation == fencing_generation
                and record.state == "PROCESSING"
                and record.lifecycle_state == "ACTIVE"
                and record.lifecycle_generation == job.captured_import_generation
            ):
                return False
            existing_count = database.scalar(
                select(ImportReadingRecord.id)
                .where(ImportReadingRecord.import_id == import_id)
                .limit(1)
            )
            if existing_count is not None:
                return False
            for reading in draft.readings:
                database.add(_reading_record(import_id, reading))
            warning_ids = iter(draft.warning_ids)
            for finding in draft.findings:
                database.add(
                    ImportFindingRecord(
                        id=secrets.token_hex(16),
                        import_id=import_id,
                        code=finding.code,
                        severity=finding.severity,
                        field_path=finding.field_path,
                        safe_value=finding.safe_value,
                        warning_id=next(warning_ids) if finding.severity == "WARNING" else None,
                    )
                )
            record.state = "READY"
            record.published_at = now.astimezone(UTC)
            job.state = "SUCCEEDED"
            job.completed_at = now.astimezone(UTC)
            attempt = database.scalar(
                select(JobAttemptRecord).where(
                    JobAttemptRecord.job_id == job.id,
                    JobAttemptRecord.fencing_generation == fencing_generation,
                )
            )
            if attempt is not None:
                attempt.state = "SUCCEEDED"
                attempt.completed_at = now.astimezone(UTC)
            return True

    def draft(self, *, owner_user_id: str, import_id: str) -> NormalizedDraft:
        with self._session_factory() as database:
            record = database.scalar(
                select(ImportRecord).where(
                    ImportRecord.id == import_id,
                    ImportRecord.owner_user_id == owner_user_id,
                )
            )
            if record is None:
                raise ImportServiceError("IMPORT_NOT_FOUND", "Import is unavailable")
            readings = database.scalars(
                select(ImportReadingRecord)
                .where(ImportReadingRecord.import_id == import_id)
                .order_by(ImportReadingRecord.start_utc_ns)
            ).all()
            findings = database.scalars(
                select(ImportFindingRecord)
                .where(ImportFindingRecord.import_id == import_id)
                .order_by(ImportFindingRecord.code, ImportFindingRecord.field_path)
            ).all()
            if record.state not in {"READY", "CONFIRMED"} or not readings:
                raise ImportServiceError("IMPORT_NOT_READY", "Import draft is not ready")
            return NormalizedDraft(
                source_hash=record.raw_content_hash,
                adapter_fingerprint=(
                    "espi-4.0-download-my-data-v1"
                    if record.adapter == "ESPI_XML"
                    else "pge-green-button-csv-v1"
                ),
                tariff_timezone="America/Los_Angeles",
                interval_resolution_seconds=readings[0].duration_seconds,
                readings=tuple(_canonical_reading(reading) for reading in readings),
                findings=tuple(
                    CanonicalFinding(
                        finding.code,
                        finding.severity,
                        finding.field_path,
                        finding.safe_value,
                    )
                    for finding in findings
                ),
            )

    def confirm(
        self,
        *,
        owner_user_id: str,
        import_id: str,
        billing_period_start_utc_ns: int,
        billing_period_end_utc_ns: int,
        acknowledged_warning_ids: tuple[str, ...],
        pge_service_attested: bool,
        now: datetime,
    ) -> ProfileVersionRecord:
        draft = self.draft(owner_user_id=owner_user_id, import_id=import_id)
        content = confirm_draft(
            draft,
            billing_period_start_utc_ns=billing_period_start_utc_ns,
            billing_period_end_utc_ns=billing_period_end_utc_ns,
            acknowledged_warning_ids=acknowledged_warning_ids,
            pge_service_attested=pge_service_attested,
        )
        profile_hash = content.sha256()
        raw_key: str | None = None
        with self._session_factory.begin() as database:
            record = database.scalar(
                select(ImportRecord)
                .where(
                    ImportRecord.id == import_id,
                    ImportRecord.owner_user_id == owner_user_id,
                )
                .with_for_update()
            )
            if record is None or record.state not in {"READY", "CONFIRMED"}:
                raise ImportServiceError("IMPORT_NOT_READY", "Import cannot be confirmed")
            existing = database.scalar(
                select(ProfileVersionRecord).where(
                    ProfileVersionRecord.owner_user_id == owner_user_id,
                    ProfileVersionRecord.content_hash == profile_hash,
                )
            )
            if existing is None:
                existing = ProfileVersionRecord(
                    id=secrets.token_hex(16),
                    owner_user_id=owner_user_id,
                    import_id=import_id,
                    content_hash=profile_hash,
                    canonical_content=content.to_bytes(),
                    billing_period_start_utc_ns=billing_period_start_utc_ns,
                    billing_period_end_utc_ns=billing_period_end_utc_ns,
                    tariff_timezone=content.tariff_timezone,
                    interval_resolution_seconds=content.interval_resolution_seconds,
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                    created_at=now.astimezone(UTC),
                )
                database.add(existing)
                database.flush()
            record.state = "CONFIRMED"
            record.confirmed_at = now.astimezone(UTC)
            record.profile_version_id = existing.id
            raw = database.scalar(
                select(RawObjectRecord).where(RawObjectRecord.import_id == import_id)
            )
            if raw is not None and raw.state in {"AVAILABLE", "DELETE_PENDING"}:
                raw_key = raw.object_key
                raw.state = "DELETE_PENDING"
        if raw_key is not None:
            self._objects.delete(raw_key)
            with self._session_factory.begin() as database:
                raw = database.scalar(
                    select(RawObjectRecord).where(
                        RawObjectRecord.import_id == import_id,
                        RawObjectRecord.state == "DELETE_PENDING",
                    )
                )
                if raw is not None:
                    raw.state = "DELETED"
                    raw.deleted_at = now.astimezone(UTC)
        return existing

    def expire_raw_objects(self, *, now: datetime) -> int:
        expired_keys: list[str] = []
        with self._session_factory.begin() as database:
            rows = database.scalars(
                select(RawObjectRecord).where(
                    or_(
                        RawObjectRecord.state == "DELETE_PENDING",
                        (
                            (RawObjectRecord.state == "AVAILABLE")
                            & (RawObjectRecord.expires_at <= now.astimezone(UTC))
                        ),
                    )
                )
            ).all()
            for row in rows:
                expired_keys.append(row.object_key)
                row.state = "DELETE_PENDING"
        for key in expired_keys:
            self._objects.delete(key)
        if expired_keys:
            with self._session_factory.begin() as database:
                rows = database.scalars(
                    select(RawObjectRecord).where(
                        RawObjectRecord.object_key.in_(expired_keys),
                        RawObjectRecord.state == "DELETE_PENDING",
                    )
                ).all()
                for row in rows:
                    row.state = "DELETED"
                    row.deleted_at = now.astimezone(UTC)
        return len(expired_keys)


def _reading_record(import_id: str, reading: CanonicalReading) -> ImportReadingRecord:
    return ImportReadingRecord(
        id=secrets.token_hex(16),
        import_id=import_id,
        start_utc_ns=reading.start_utc_ns,
        duration_seconds=reading.duration_seconds,
        energy_wh=reading.energy_wh,
        flow_direction=reading.flow_direction.value,
        source_unit=reading.source_unit,
        source_multiplier=reading.source_multiplier,
        source_reading_type=reading.source_reading_type,
        source_service_category=reading.source_service_category,
        source_commodity=reading.source_commodity,
        source_accumulation_behavior=reading.source_accumulation_behavior,
        source_data_qualifier=reading.source_data_qualifier,
        source_time_attribute=reading.source_time_attribute,
        source_local_time_parameters_hash=reading.source_local_time_parameters_hash,
        source_timezone_offset_seconds=reading.source_timezone_offset_seconds,
        source_dst_offset_seconds=reading.source_dst_offset_seconds,
        quality_flags_json=json.dumps(sorted(reading.quality_flags), separators=(",", ":")),
    )


def _canonical_reading(record: ImportReadingRecord) -> CanonicalReading:
    return CanonicalReading(
        start_utc_ns=record.start_utc_ns,
        duration_seconds=record.duration_seconds,
        energy_wh=record.energy_wh,
        flow_direction=FlowDirection(record.flow_direction),
        source_unit=record.source_unit,
        source_multiplier=record.source_multiplier,
        source_reading_type=record.source_reading_type,
        source_service_category=record.source_service_category,
        source_commodity=record.source_commodity,
        source_accumulation_behavior=record.source_accumulation_behavior,
        source_data_qualifier=record.source_data_qualifier,
        source_time_attribute=record.source_time_attribute,
        source_local_time_parameters_hash=record.source_local_time_parameters_hash,
        source_timezone_offset_seconds=record.source_timezone_offset_seconds,
        source_dst_offset_seconds=record.source_dst_offset_seconds,
        quality_flags=frozenset(json.loads(record.quality_flags_json)),
    )
