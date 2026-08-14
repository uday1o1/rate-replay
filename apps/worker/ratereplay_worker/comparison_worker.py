"""Fenced worker for durable tariff comparison."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from itertools import pairwise
from typing import cast

from pydantic import ValidationError
from ratereplay_persistence.artifacts import ArtifactService, ArtifactServiceError
from ratereplay_persistence.comparisons import (
    COMPARISON_CALCULATION_CONTRACT,
    comparison_semantic_identity,
)
from ratereplay_persistence.jobs import JobLease, JobService
from ratereplay_persistence.models import (
    ComparisonResultRecord,
    ImportReadingRecord,
    JobRecord,
    ProfileVersionRecord,
    ReplayResultRecord,
)
from ratereplay_tariffs.admission import AdmittedTariff
from ratereplay_tariffs.billing import (
    IntervalReplayRequest,
    ReplayError,
    ReplayInterval,
    ReplayResult,
    evaluate_eligibility,
)
from ratereplay_tariffs.comparison import (
    ComparisonError,
    compare_admitted_tariffs,
)
from ratereplay_tariffs.schema import ChargeComponentKey
from sqlalchemy import BigInteger, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import Session, sessionmaker


class ComparisonWorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ComparisonWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        session_factory: sessionmaker[Session],
        jobs: JobService,
        artifacts: ArtifactService,
        admitted_tariffs: dict[str, AdmittedTariff],
        required_component_keys: tuple[ChargeComponentKey, ...],
        environment_lock_hash: str,
    ) -> None:
        self._worker_id = worker_id
        self._sessions = session_factory
        self._jobs = jobs
        self._artifacts = artifacts
        self._tariffs = admitted_tariffs
        self._required_component_keys = required_component_keys
        self._environment_lock_hash = environment_lock_hash

    def run_once(self, *, now: datetime) -> bool:
        now = now.astimezone(UTC)
        lease = self._jobs.lease_next(
            worker_id=self._worker_id,
            now=now,
            kinds=frozenset({"COMPARISON"}),
        )
        if lease is None:
            return False
        if not self._jobs.start(lease, now=now):
            return True
        try:
            self._publish(lease, now=now)
        except ComparisonWorkerError as error:
            self._jobs.fail(
                lease,
                code=error.code,
                retryable=error.retryable,
                now=now,
            )
        except ArtifactServiceError as error:
            self._jobs.fail(
                lease,
                code=error.code,
                retryable=False,
                now=now,
            )
        return True

    def _publish(self, lease: JobLease, *, now: datetime) -> None:
        with self._sessions() as database:
            job = database.get(JobRecord, lease.job_id)
            if (
                job is None
                or job.owner_user_id is None
                or job.profile_version_id is None
                or job.requested_semantic_hash is None
                or job.calculation_contract_version != COMPARISON_CALCULATION_CONTRACT
            ):
                raise ComparisonWorkerError(
                    "COMPARISON_JOB_INVALID",
                    "Comparison job does not contain a complete semantic request",
                )
            payload = _request_payload(job.request_json)
            profile = database.get(ProfileVersionRecord, job.profile_version_id)
            replay = database.get(
                ReplayResultRecord,
                cast(str, payload["current_replay_id"]),
            )
            replay_job = database.get(JobRecord, replay.job_id) if replay is not None else None
            if (
                profile is None
                or profile.owner_user_id != job.owner_user_id
                or profile.id != payload["profile_version_id"]
                or replay is None
                or replay.owner_user_id != job.owner_user_id
                or replay.profile_version_id != profile.id
                or replay.lifecycle_state != "ACTIVE"
                or replay_job is None
                or replay_job.state != "SUCCEEDED"
            ):
                raise ComparisonWorkerError(
                    "COMPARISON_SCOPE_UNAVAILABLE",
                    "Comparison sources are outside the live fenced owner scope",
                )
            try:
                comparison_request = IntervalReplayRequest.model_validate_json(
                    json.dumps(payload["comparison_request"])
                )
                current_result = ReplayResult.model_validate_json(replay.result_json)
            except ValidationError as error:
                raise ComparisonWorkerError(
                    "COMPARISON_REQUEST_INVALID",
                    "Comparison request failed schema validation",
                ) from error
            _validate_profile_intervals(database, profile, comparison_request)
            tariff_ids = cast(tuple[str, ...], payload["candidate_tariff_version_ids"])
            try:
                tariffs = tuple(self._tariffs[tariff_id] for tariff_id in tariff_ids)
            except KeyError as error:
                raise ComparisonWorkerError(
                    "COMPARISON_TARIFF_UNKNOWN",
                    "A comparison tariff is unavailable",
                ) from error
            current_tariff = self._tariffs.get(replay.tariff_version_id)
            if current_tariff is None:
                raise ComparisonWorkerError(
                    "COMPARISON_CURRENT_TARIFF_UNKNOWN",
                    "The current replay tariff is unavailable",
                )
            provided_current_eligibility = evaluate_eligibility(
                current_tariff.compilation,
                comparison_request.account_facts,
                comparison_request.dated_eligibility_facts,
            )
            if (
                current_result.manifest.tariff_version_id != replay.tariff_version_id
                or provided_current_eligibility.account_facts_sha256
                != current_result.eligibility.account_facts_sha256
            ):
                raise ComparisonWorkerError(
                    "CURRENT_REPLAY_ACCOUNT_MISMATCH",
                    "Comparison account facts differ from the current replay",
                )
            identity = comparison_semantic_identity(
                tariffs=tariffs,
                comparison_request=comparison_request,
                current_replay_result_hash=replay.result_hash,
                required_component_keys=self._required_component_keys,
                environment_lock_hash=self._environment_lock_hash,
            )
            if identity.sha256() != job.requested_semantic_hash:
                raise ComparisonWorkerError(
                    "COMPARISON_SEMANTIC_IDENTITY_MISMATCH",
                    "Comparison request differs from its submitted semantic identity",
                )
            try:
                result = compare_admitted_tariffs(
                    tariffs,
                    comparison_request,
                    current_tariff_version_id=replay.tariff_version_id,
                    required_component_keys=self._required_component_keys,
                )
            except (ComparisonError, ReplayError) as error:
                raise ComparisonWorkerError(error.code, str(error)) from error
            owner_user_id = job.owner_user_id
            operation_request_hash = job.request_hash
            semantic_hash = job.requested_semantic_hash
            profile_version_id = profile.id
            current_replay_id = replay.id
        comparison_id = secrets.token_hex(16)
        comparison = ComparisonResultRecord(
            id=comparison_id,
            owner_user_id=owner_user_id,
            profile_version_id=profile_version_id,
            current_replay_id=current_replay_id,
            job_id=lease.job_id,
            operation_request_hash=operation_request_hash,
            semantic_hash=semantic_hash,
            result_hash=result.comparison_sha256,
            result_json=result.model_dump_json(),
            lifecycle_state="ACTIVE",
            lifecycle_generation=0,
            created_at=now,
        )

        def publish_result(database: Session) -> None:
            database.add(comparison)

        self._artifacts.finalize(
            owner_user_id=owner_user_id,
            lease=lease,
            semantic_hash=semantic_hash,
            calculation_contract_version=COMPARISON_CALCULATION_CONTRACT,
            result_type="COMPARISON",
            result_id=comparison_id,
            artifact_registration_ids=(),
            now=now,
            publish_result=publish_result,
        )


def _request_payload(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise ComparisonWorkerError(
            "COMPARISON_REQUEST_INVALID",
            "Comparison job request is not canonical JSON",
        ) from error
    required = {
        "candidate_tariff_version_ids",
        "comparison_request",
        "current_replay_id",
        "profile_version_id",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != required
        or not isinstance(payload["profile_version_id"], str)
        or not isinstance(payload["current_replay_id"], str)
        or not isinstance(payload["comparison_request"], dict)
        or not isinstance(payload["candidate_tariff_version_ids"], list)
        or not all(isinstance(value, str) for value in payload["candidate_tariff_version_ids"])
        or payload["candidate_tariff_version_ids"]
        != sorted(payload["candidate_tariff_version_ids"])
        or len(payload["candidate_tariff_version_ids"])
        != len(set(payload["candidate_tariff_version_ids"]))
    ):
        raise ComparisonWorkerError(
            "COMPARISON_REQUEST_INVALID",
            "Comparison job request schema is invalid",
        )
    payload["candidate_tariff_version_ids"] = tuple(payload["candidate_tariff_version_ids"])
    return cast(dict[str, object], payload)


def _validate_profile_intervals(
    database: Session,
    profile: ProfileVersionRecord,
    request: IntervalReplayRequest,
) -> None:
    records = tuple(
        database.scalars(
            select(ImportReadingRecord)
            .where(
                ImportReadingRecord.import_id == profile.import_id,
                ImportReadingRecord.start_utc_ns >= profile.billing_period_start_utc_ns,
                ImportReadingRecord.start_utc_ns
                + sql_cast(ImportReadingRecord.duration_seconds, BigInteger) * 1_000_000_000
                <= profile.billing_period_end_utc_ns,
            )
            .order_by(ImportReadingRecord.start_utc_ns)
        )
    )
    stored = tuple(
        ReplayInterval(
            start_utc_ns=record.start_utc_ns,
            duration_seconds=record.duration_seconds,
            energy_wh=record.energy_wh,
        )
        for record in records
        if record.flow_direction == "IMPORT"
    )
    if (
        request.profile_content_sha256 != profile.content_hash
        or len(stored) != len(records)
        or request.intervals != stored
        or request.energy_wh != sum(interval.energy_wh for interval in stored)
        or not stored
        or stored[0].start_utc_ns != profile.billing_period_start_utc_ns
        or stored[-1].start_utc_ns + stored[-1].duration_seconds * 1_000_000_000
        != profile.billing_period_end_utc_ns
        or any(
            current.start_utc_ns + current.duration_seconds * 1_000_000_000
            != following.start_utc_ns
            for current, following in pairwise(stored)
        )
    ):
        raise ComparisonWorkerError(
            "COMPARISON_PROFILE_MISMATCH",
            "Comparison intervals differ from the immutable confirmed profile",
        )
