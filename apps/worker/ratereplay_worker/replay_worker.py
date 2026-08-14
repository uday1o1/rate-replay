"""Fenced worker for durable historical bill replay."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime
from typing import cast

from pydantic import ValidationError
from ratereplay_persistence.artifacts import ArtifactService, ArtifactServiceError
from ratereplay_persistence.jobs import JobLease, JobService
from ratereplay_persistence.models import (
    CalculationManifestRecord,
    JobRecord,
    ProfileVersionRecord,
    ReplayResultRecord,
)
from ratereplay_persistence.replays import (
    REPLAY_CALCULATION_CONTRACT,
    replay_semantic_identity,
)
from ratereplay_tariffs.admission import AdmittedTariff
from ratereplay_tariffs.billing import ReplayError, ReplayRequest, replay_compiled_tariff
from sqlalchemy.orm import Session, sessionmaker


class ReplayWorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ReplayWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        session_factory: sessionmaker[Session],
        jobs: JobService,
        artifacts: ArtifactService,
        admitted_tariffs: dict[str, AdmittedTariff],
        environment_lock_hash: str,
    ) -> None:
        self._worker_id = worker_id
        self._sessions = session_factory
        self._jobs = jobs
        self._artifacts = artifacts
        self._tariffs = admitted_tariffs
        self._environment_lock_hash = environment_lock_hash

    def run_once(self, *, now: datetime) -> bool:
        now = now.astimezone(UTC)
        lease = self._jobs.lease_next(
            worker_id=self._worker_id,
            now=now,
            kinds=frozenset({"REPLAY"}),
        )
        if lease is None:
            return False
        if not self._jobs.start(lease, now=now):
            return True
        try:
            self._publish(lease, now=now)
        except ReplayWorkerError as error:
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
                or job.calculation_contract_version != REPLAY_CALCULATION_CONTRACT
            ):
                raise ReplayWorkerError(
                    "REPLAY_JOB_INVALID",
                    "Replay job does not contain a complete semantic request",
                )
            profile = database.get(ProfileVersionRecord, job.profile_version_id)
            payload = _request_payload(job.request_json)
            profile_version_id = cast(str, payload["profile_version_id"])
            tariff_version_id = cast(str, payload["tariff_version_id"])
            if (
                profile is None
                or profile.owner_user_id != job.owner_user_id
                or profile_version_id != profile.id
            ):
                raise ReplayWorkerError(
                    "REPLAY_SCOPE_UNAVAILABLE",
                    "Replay source is outside the live fenced owner scope",
                )
            tariff = self._tariffs.get(tariff_version_id)
            if tariff is None:
                raise ReplayWorkerError("REPLAY_TARIFF_UNKNOWN", "Replay tariff is unavailable")
            try:
                replay_request = ReplayRequest.model_validate_json(
                    json.dumps(payload["replay_request"])
                )
            except ValidationError as error:
                raise ReplayWorkerError(
                    "REPLAY_REQUEST_INVALID",
                    "Replay request failed schema validation",
                ) from error
            identity = replay_semantic_identity(
                tariff=tariff,
                replay_request=replay_request,
                environment_lock_hash=self._environment_lock_hash,
            )
            if (
                replay_request.profile_content_sha256 != profile.content_hash
                or identity.sha256() != job.requested_semantic_hash
            ):
                raise ReplayWorkerError(
                    "REPLAY_SEMANTIC_IDENTITY_MISMATCH",
                    "Replay request differs from its submitted semantic identity",
                )
            try:
                result = replay_compiled_tariff(tariff.compilation, replay_request)
            except ReplayError as error:
                raise ReplayWorkerError(error.code, str(error)) from error
            owner_user_id = job.owner_user_id
            operation_request_hash = job.request_hash
            semantic_hash = job.requested_semantic_hash
            profile_version_id = profile.id
        replay_id = secrets.token_hex(16)
        replay = ReplayResultRecord(
            id=replay_id,
            owner_user_id=owner_user_id,
            profile_version_id=profile_version_id,
            job_id=lease.job_id,
            tariff_version_id=result.manifest.tariff_version_id,
            operation_request_hash=operation_request_hash,
            semantic_hash=semantic_hash,
            result_hash=result.result_sha256,
            result_json=result.model_dump_json(),
            lifecycle_state="ACTIVE",
            lifecycle_generation=0,
            created_at=now,
        )
        manifest = CalculationManifestRecord(
            id=secrets.token_hex(16),
            replay_id=replay_id,
            calculation_hash=result.manifest.calculation_sha256,
            manifest_json=result.manifest.model_dump_json(),
            created_at=now,
        )

        def publish_result(database: Session) -> None:
            database.add(replay)
            database.flush()
            database.add(manifest)

        self._artifacts.finalize(
            owner_user_id=owner_user_id,
            lease=lease,
            semantic_hash=semantic_hash,
            calculation_contract_version=REPLAY_CALCULATION_CONTRACT,
            result_type="REPLAY",
            result_id=replay_id,
            artifact_registration_ids=(),
            now=now,
            publish_result=publish_result,
        )


def _request_payload(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise ReplayWorkerError(
            "REPLAY_REQUEST_INVALID",
            "Replay job request is not canonical JSON",
        ) from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"profile_version_id", "replay_request", "tariff_version_id"}
        or not isinstance(payload["profile_version_id"], str)
        or not isinstance(payload["tariff_version_id"], str)
        or not isinstance(payload["replay_request"], dict)
    ):
        raise ReplayWorkerError(
            "REPLAY_REQUEST_INVALID",
            "Replay job request schema is invalid",
        )
    return cast(dict[str, object], payload)
