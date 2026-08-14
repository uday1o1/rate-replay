"""Relational records shared by API and worker processes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ratereplay_persistence.database import Base


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username_canonical: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    lifecycle_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    sessions: Mapped[list[SessionRecord]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SessionRecord(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_active", "user_id", "revoked_at"),
        Index("ix_sessions_idle_expiry", "idle_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[UserRecord] = relationship(back_populates="sessions")


class ImportRecord(Base):
    __tablename__ = "imports"
    __table_args__ = (
        CheckConstraint(
            "state IN ('QUEUED', 'PROCESSING', 'READY', 'CONFIRMED', 'FAILED', 'DELETED')",
            name="ck_import_state",
        ),
        CheckConstraint(
            "lifecycle_state IN ('ACTIVE', 'DELETION_PENDING_LEDGER', 'DELETING', 'DELETED')",
            name="ck_import_lifecycle",
        ),
        Index("ix_imports_owner_created", "owner_user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    lifecycle_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    adapter: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    profile_version_id: Mapped[str | None] = mapped_column(String(32))


class RawObjectRecord(Base):
    __tablename__ = "raw_objects"
    __table_args__ = (UniqueConstraint("object_key", name="uq_raw_object_key"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    import_id: Mapped[str] = mapped_column(
        ForeignKey("imports.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    object_key: Mapped[str] = mapped_column(String(255), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OperationRequestRecord(Base):
    __tablename__ = "operation_requests"
    __table_args__ = (
        UniqueConstraint(
            "owner_user_id", "route_id", "idempotency_key", name="uq_operation_identity"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    route_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ImportReadingRecord(Base):
    __tablename__ = "interval_readings"
    __table_args__ = (
        UniqueConstraint("import_id", "start_utc_ns", name="uq_import_reading_start"),
        Index("ix_interval_readings_import_start", "import_id", "start_utc_ns"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    import_id: Mapped[str] = mapped_column(
        ForeignKey("imports.id", ondelete="CASCADE"), nullable=False
    )
    start_utc_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    energy_wh: Mapped[int] = mapped_column(BigInteger, nullable=False)
    flow_direction: Mapped[str] = mapped_column(String(16), nullable=False)
    source_unit: Mapped[str] = mapped_column(String(32), nullable=False)
    source_multiplier: Mapped[int] = mapped_column(Integer, nullable=False)
    source_reading_type: Mapped[str] = mapped_column(String(64), nullable=False)
    source_service_category: Mapped[str] = mapped_column(String(64), nullable=False)
    source_commodity: Mapped[str] = mapped_column(String(64), nullable=False)
    source_accumulation_behavior: Mapped[str] = mapped_column(String(64), nullable=False)
    source_data_qualifier: Mapped[str] = mapped_column(String(64), nullable=False)
    source_time_attribute: Mapped[str] = mapped_column(String(64), nullable=False)
    source_local_time_parameters_hash: Mapped[str | None] = mapped_column(String(64))
    source_timezone_offset_seconds: Mapped[int | None] = mapped_column(Integer)
    source_dst_offset_seconds: Mapped[int | None] = mapped_column(Integer)
    quality_flags_json: Mapped[str] = mapped_column(Text, nullable=False)


class ImportFindingRecord(Base):
    __tablename__ = "import_quality_findings"
    __table_args__ = (
        UniqueConstraint("import_id", "code", "field_path", name="uq_import_finding_identity"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    import_id: Mapped[str] = mapped_column(
        ForeignKey("imports.id", ondelete="CASCADE"), nullable=False
    )
    code: Mapped[str] = mapped_column(String(64), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    field_path: Mapped[str] = mapped_column(String(255), nullable=False)
    safe_value: Mapped[str] = mapped_column(String(255), nullable=False)
    warning_id: Mapped[str | None] = mapped_column(String(64))


class ProfileVersionRecord(Base):
    __tablename__ = "profile_versions"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "content_hash", name="uq_owner_profile_content"),
        Index("ix_profiles_owner_created", "owner_user_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    import_id: Mapped[str] = mapped_column(ForeignKey("imports.id"), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    billing_period_start_utc_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    billing_period_end_utc_ns: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tariff_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    interval_resolution_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    lifecycle_state: Mapped[str] = mapped_column(String(32), nullable=False, default="ACTIVE")
    lifecycle_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class JobRecord(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(
            "state IN ('QUEUED', 'LEASED', 'RUNNING', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_job_state",
        ),
        Index("ix_jobs_lease_queue", "state", "not_before", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    request_schema_version: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    import_id: Mapped[str] = mapped_column(ForeignKey("imports.id"), nullable=False)
    captured_account_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    captured_import_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    fencing_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_acquired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    not_before: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class JobAttemptRecord(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "attempt_number", name="uq_job_attempt_number"),
        UniqueConstraint("job_id", "fencing_generation", name="uq_job_attempt_fence"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), nullable=False)
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    fencing_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    leased_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))


def _prevent_immutable_update(_mapper: object, _connection: object, target: object) -> None:
    raise RuntimeError(f"{type(target).__name__} is immutable")


for _immutable_model in (ImportReadingRecord, ImportFindingRecord, ProfileVersionRecord):
    event.listen(_immutable_model, "before_update", _prevent_immutable_update)
