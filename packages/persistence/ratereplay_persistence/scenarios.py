"""Owner-scoped immutable scenario publication."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from ratereplay_optimizer.models import ValidatedScenario
from ratereplay_optimizer.results import ScenarioOptimizationResult
from ratereplay_tariffs.hashing import canonical_content_sha256
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.models import (
    CalculationManifestRecord,
    ImportRecord,
    JobAttemptRecord,
    JobRecord,
    OperationRequestRecord,
    ProfileVersionRecord,
    ScenarioLoadRecord,
    ScenarioRecord,
    ScenarioReferenceScheduleRecord,
    ScenarioResultRecord,
    UserRecord,
)

SCENARIO_ROUTE: Final = "POST:/v1/scenarios"
SCENARIO_REQUEST_SCHEMA: Final = "scenario-operation-v1"
IDEMPOTENCY_RETENTION: Final = timedelta(hours=24)


class ScenarioServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredScenario:
    scenario_id: str
    result_id: str
    job_id: str
    repeated: bool


class ScenarioService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def publish(
        self,
        *,
        owner_user_id: str,
        profile_version_id: str,
        idempotency_key: str,
        operation_request_hash: str,
        validated: ValidatedScenario,
        result: ScenarioOptimizationResult,
        now: datetime,
    ) -> StoredScenario:
        if not 8 <= len(idempotency_key) <= 128:
            raise ScenarioServiceError(
                "INVALID_IDEMPOTENCY_KEY", "Idempotency key must contain 8 to 128 characters"
            )
        if result.manifest.scenario_input_sha256 != canonical_content_sha256(
            b"RateReplay.ScenarioInput.v1", validated.scenario.model_dump(mode="json")
        ):
            raise ScenarioServiceError(
                "SCENARIO_RESULT_INPUT_MISMATCH",
                "Scenario result does not identify the validated input.",
            )
        now = now.astimezone(UTC)
        with self._session_factory() as database:
            existing_request = database.scalar(
                select(OperationRequestRecord).where(
                    OperationRequestRecord.owner_user_id == owner_user_id,
                    OperationRequestRecord.route_id == SCENARIO_ROUTE,
                    OperationRequestRecord.idempotency_key == idempotency_key,
                )
            )
            if existing_request is not None:
                return self._repeat_or_conflict(database, existing_request, operation_request_hash)
            user = database.get(UserRecord, owner_user_id)
            profile = database.get(ProfileVersionRecord, profile_version_id)
            if user is None or user.lifecycle_state != "ACTIVE":
                raise ScenarioServiceError("OWNER_NOT_ACTIVE", "Account cannot create scenarios")
            if (
                profile is None
                or profile.owner_user_id != owner_user_id
                or profile.lifecycle_state != "ACTIVE"
            ):
                raise ScenarioServiceError("PROFILE_NOT_FOUND", "Profile is unavailable")
            if profile.content_hash != validated.scenario.profile_content_sha256:
                raise ScenarioServiceError(
                    "PROFILE_INPUT_MISMATCH", "Scenario does not use the selected profile"
                )
            imported = database.get(ImportRecord, profile.import_id)
            if imported is None or imported.lifecycle_state != "ACTIVE":
                raise ScenarioServiceError("PROFILE_NOT_FOUND", "Profile scope is unavailable")
            prior_result = database.scalar(
                select(ScenarioResultRecord).where(
                    ScenarioResultRecord.owner_user_id == owner_user_id,
                    ScenarioResultRecord.semantic_hash == result.manifest.calculation_sha256,
                )
            )
            if prior_result is not None:
                operation = self._operation_record(
                    owner_user_id=owner_user_id,
                    idempotency_key=idempotency_key,
                    operation_request_hash=operation_request_hash,
                    scenario_id=prior_result.scenario_id,
                    now=now,
                )
                database.add(operation)
                try:
                    database.commit()
                except IntegrityError as error:
                    database.rollback()
                    return self._resolve_publication_race(
                        database,
                        owner_user_id,
                        idempotency_key,
                        operation_request_hash,
                        error,
                    )
                return StoredScenario(
                    prior_result.scenario_id,
                    prior_result.id,
                    prior_result.job_id,
                    True,
                )

            scenario_id = secrets.token_hex(16)
            result_id = secrets.token_hex(16)
            job_id = secrets.token_hex(16)
            job = JobRecord(
                id=job_id,
                owner_user_id=owner_user_id,
                kind="SCENARIO",
                request_schema_version=SCENARIO_REQUEST_SCHEMA,
                request_hash=operation_request_hash,
                scope_mode="ACTIVE_SCOPE",
                import_id=profile.import_id,
                profile_version_id=profile.id,
                captured_account_generation=user.lifecycle_generation,
                captured_import_generation=imported.lifecycle_generation,
                captured_profile_generation=profile.lifecycle_generation,
                state="SUCCEEDED",
                attempt_count=1,
                max_attempts=1,
                fencing_generation=1,
                lease_owner="inline-verified-optimizer",
                lease_acquired_at=now,
                lease_expires_at=now,
                heartbeat_at=now,
                not_before=now,
                cancel_requested=False,
                created_at=now,
                completed_at=now,
            )
            attempt = JobAttemptRecord(
                id=secrets.token_hex(16),
                job_id=job_id,
                attempt_number=1,
                fencing_generation=1,
                worker_id="inline-verified-optimizer",
                state="SUCCEEDED",
                leased_at=now,
                lease_expires_at=now,
                completed_at=now,
            )
            scenario = ScenarioRecord(
                id=scenario_id,
                owner_user_id=owner_user_id,
                profile_version_id=profile_version_id,
                job_id=job_id,
                tariff_version_id=validated.scenario.tariff_version_id,
                operation_request_hash=operation_request_hash,
                input_hash=result.manifest.scenario_input_sha256,
                input_json=validated.scenario.model_dump_json(),
                state="SUCCEEDED",
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
                completed_at=now,
            )
            scenario_result = ScenarioResultRecord(
                id=result_id,
                owner_user_id=owner_user_id,
                scenario_id=scenario_id,
                profile_version_id=profile_version_id,
                job_id=job_id,
                operation_request_hash=operation_request_hash,
                semantic_hash=result.manifest.calculation_sha256,
                result_hash=result.result_sha256,
                result_json=result.model_dump_json(),
                lifecycle_state="ACTIVE",
                lifecycle_generation=0,
                created_at=now,
            )
            manifest = CalculationManifestRecord(
                id=secrets.token_hex(16),
                replay_id=None,
                scenario_result_id=result_id,
                calculation_hash=result.manifest.calculation_sha256,
                manifest_json=result.manifest.model_dump_json(),
                created_at=now,
            )
            operation = self._operation_record(
                owner_user_id=owner_user_id,
                idempotency_key=idempotency_key,
                operation_request_hash=operation_request_hash,
                scenario_id=scenario_id,
                now=now,
            )
            loads, references = self._load_records(scenario_id, validated)
            try:
                database.add(job)
                database.flush()
                database.add_all([attempt, scenario])
                database.flush()
                database.add_all(loads)
                database.flush()
                database.add_all(references)
                database.add(scenario_result)
                database.flush()
                database.add_all([manifest, operation])
                database.commit()
            except IntegrityError as error:
                database.rollback()
                return self._resolve_publication_race(
                    database,
                    owner_user_id,
                    idempotency_key,
                    operation_request_hash,
                    error,
                )
            return StoredScenario(scenario_id, result_id, job_id, False)

    def cancel(self, *, owner_user_id: str, scenario_id: str) -> None:
        with self._session_factory() as database:
            scenario = database.scalar(
                select(ScenarioRecord).where(
                    ScenarioRecord.id == scenario_id,
                    ScenarioRecord.owner_user_id == owner_user_id,
                    ScenarioRecord.lifecycle_state == "ACTIVE",
                )
            )
            if scenario is None:
                raise ScenarioServiceError("SCENARIO_NOT_FOUND", "Scenario is unavailable")
            if scenario.state in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                raise ScenarioServiceError(
                    "SCENARIO_ALREADY_TERMINAL", "A terminal scenario cannot be cancelled"
                )
            raise ScenarioServiceError(
                "SCENARIO_CANCELLATION_UNAVAILABLE",
                "Scenario cancellation requires the durable worker path.",
            )

    @staticmethod
    def _load_records(
        scenario_id: str,
        validated: ValidatedScenario,
    ) -> tuple[list[ScenarioLoadRecord], list[ScenarioReferenceScheduleRecord]]:
        loads: list[ScenarioLoadRecord] = []
        references: list[ScenarioReferenceScheduleRecord] = []
        for load in validated.scenario.loads:
            load_record_id = secrets.token_hex(16)
            loads.append(
                ScenarioLoadRecord(
                    id=load_record_id,
                    scenario_id=scenario_id,
                    load_id=str(load.load_id),
                    physical_asset_key=load.physical_asset_key,
                    kind=load.kind,
                    mode=load.mode,
                    execution_spec_json=load.execution_spec.model_dump_json(),
                )
            )
            for occurrence in load.occurrences:
                schedule_payload = [
                    slot.model_dump(mode="json") for slot in occurrence.reference_schedule
                ]
                references.append(
                    ScenarioReferenceScheduleRecord(
                        id=secrets.token_hex(16),
                        scenario_load_id=load_record_id,
                        occurrence_id=str(occurrence.occurrence_id),
                        required_energy_wh=occurrence.required_energy_wh,
                        earliest_start_utc=occurrence.earliest_start_utc,
                        deadline_utc=occurrence.deadline_utc,
                        schedule_hash=canonical_content_sha256(
                            b"RateReplay.OccurrenceReferenceSchedule.v1", schedule_payload
                        ),
                        schedule_json=occurrence.model_dump_json(),
                    )
                )
        return loads, references

    @staticmethod
    def _operation_record(
        *,
        owner_user_id: str,
        idempotency_key: str,
        operation_request_hash: str,
        scenario_id: str,
        now: datetime,
    ) -> OperationRequestRecord:
        return OperationRequestRecord(
            id=secrets.token_hex(16),
            owner_user_id=owner_user_id,
            route_id=SCENARIO_ROUTE,
            idempotency_key=idempotency_key,
            request_schema_version=SCENARIO_REQUEST_SCHEMA,
            canonical_payload_hash=operation_request_hash,
            operation_id=scenario_id,
            created_at=now,
            expires_at=now + IDEMPOTENCY_RETENTION,
        )

    def _resolve_publication_race(
        self,
        database: Session,
        owner_user_id: str,
        idempotency_key: str,
        operation_request_hash: str,
        error: IntegrityError,
    ) -> StoredScenario:
        existing = database.scalar(
            select(OperationRequestRecord).where(
                OperationRequestRecord.owner_user_id == owner_user_id,
                OperationRequestRecord.route_id == SCENARIO_ROUTE,
                OperationRequestRecord.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise ScenarioServiceError(
                "SCENARIO_PUBLICATION_CONFLICT", "Scenario publication conflicted"
            ) from error
        return self._repeat_or_conflict(database, existing, operation_request_hash)

    @staticmethod
    def _repeat_or_conflict(
        database: Session,
        operation: OperationRequestRecord,
        operation_request_hash: str,
    ) -> StoredScenario:
        if operation.canonical_payload_hash != operation_request_hash:
            raise ScenarioServiceError(
                "IDEMPOTENCY_KEY_REUSED", "Idempotency key is bound to another scenario request"
            )
        scenario = database.get(ScenarioRecord, operation.operation_id)
        if scenario is None:
            raise ScenarioServiceError("OPERATION_INCOMPLETE", "Scenario operation is incomplete")
        result = database.scalar(
            select(ScenarioResultRecord).where(ScenarioResultRecord.scenario_id == scenario.id)
        )
        if result is None:
            raise ScenarioServiceError("OPERATION_INCOMPLETE", "Scenario result is incomplete")
        return StoredScenario(scenario.id, result.id, scenario.job_id, True)
