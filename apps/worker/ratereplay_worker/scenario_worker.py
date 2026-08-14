"""Fenced worker for durable historical flexible-load scenarios."""

from __future__ import annotations

import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from pydantic import ValidationError
from ratereplay_domain.telemetry import Telemetry
from ratereplay_optimizer.lowering import OptimizationLoweringError
from ratereplay_optimizer.models import (
    CanonicalProfileSlot,
    ScenarioInput,
    SolverConfiguration,
)
from ratereplay_optimizer.results import ScenarioResultError, build_scenario_result
from ratereplay_optimizer.scenario import (
    ScenarioValidationError,
    validate_and_decompose_scenario,
)
from ratereplay_optimizer.solver import (
    OptimizationExecutionError,
    optimize_exact,
    optimize_off_peak_heuristic,
)
from ratereplay_optimizer.verification import ScheduleVerificationError
from ratereplay_persistence.artifacts import ArtifactService, ArtifactServiceError
from ratereplay_persistence.jobs import JobLease, JobService
from ratereplay_persistence.models import (
    CalculationManifestRecord,
    ImportReadingRecord,
    JobRecord,
    ProfileVersionRecord,
    ScenarioRecord,
    ScenarioResultRecord,
)
from ratereplay_persistence.scenarios import (
    SCENARIO_CALCULATION_CONTRACT,
    scenario_semantic_identity,
)
from ratereplay_tariffs.admission import AdmittedTariff
from ratereplay_tariffs.billing import ReplayError
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts
from sqlalchemy import BigInteger, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import Session, sessionmaker


class ScenarioWorkerError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ScenarioWorker:
    def __init__(
        self,
        *,
        worker_id: str,
        session_factory: sessionmaker[Session],
        jobs: JobService,
        artifacts: ArtifactService,
        admitted_tariffs: dict[str, AdmittedTariff],
        environment_lock_hash: str,
        telemetry: Telemetry | None = None,
    ) -> None:
        self._worker_id = worker_id
        self._sessions = session_factory
        self._jobs = jobs
        self._artifacts = artifacts
        self._tariffs = admitted_tariffs
        self._environment_lock_hash = environment_lock_hash
        self._telemetry = telemetry

    def run_once(self, *, now: datetime) -> bool:
        started = time.perf_counter()
        now = now.astimezone(UTC)
        lease = self._jobs.lease_next(
            worker_id=self._worker_id,
            now=now,
            kinds=frozenset({"SCENARIO"}),
        )
        if lease is None:
            return False
        if self._telemetry is not None:
            self._telemetry.record_job_lease(
                kind=lease.kind,
                job_id=lease.job_id,
                attempt_number=lease.attempt_number,
            )
        if not self._jobs.start(lease, now=now):
            return True
        self._mark_running(lease)
        try:
            load_count, solver_status, solver_duration = self._publish(lease, now=now)
        except ScenarioWorkerError as error:
            failed = self._jobs.fail(
                lease,
                code=error.code,
                retryable=error.retryable,
                now=now,
            )
            if failed:
                self._sync_terminal_state(lease.job_id)
        except ArtifactServiceError as error:
            failed = self._jobs.fail(
                lease,
                code=error.code,
                retryable=False,
                now=now,
            )
            if failed:
                self._sync_terminal_state(lease.job_id)
        else:
            if self._telemetry is not None:
                self._telemetry.observe_solver(
                    status=solver_status,
                    duration_seconds=solver_duration,
                )
                self._telemetry.observe_scenario(
                    load_count=load_count,
                    duration_seconds=time.perf_counter() - started,
                )
        return True

    def _mark_running(self, lease: JobLease) -> None:
        with self._sessions.begin() as database:
            job = database.get(JobRecord, lease.job_id)
            scenario = database.scalar(
                select(ScenarioRecord).where(ScenarioRecord.job_id == lease.job_id)
            )
            if (
                job is not None
                and scenario is not None
                and job.state == "RUNNING"
                and job.lease_owner == lease.worker_id
                and job.fencing_generation == lease.fencing_generation
                and scenario.state == "QUEUED"
                and scenario.lifecycle_state == "ACTIVE"
            ):
                scenario.state = "RUNNING"

    def _sync_terminal_state(self, job_id: str) -> None:
        with self._sessions.begin() as database:
            job = database.get(JobRecord, job_id)
            scenario = database.scalar(
                select(ScenarioRecord).where(ScenarioRecord.job_id == job_id)
            )
            if (
                job is not None
                and scenario is not None
                and job.state in {"FAILED", "CANCELLED"}
                and scenario.lifecycle_state == "ACTIVE"
                and scenario.state not in {"SUCCEEDED", "FAILED", "CANCELLED"}
            ):
                scenario.state = job.state
                scenario.completed_at = job.completed_at

    def _publish(self, lease: JobLease, *, now: datetime) -> tuple[int, str, float]:
        with self._sessions() as database:
            job = database.get(JobRecord, lease.job_id)
            if (
                job is None
                or job.owner_user_id is None
                or job.profile_version_id is None
                or job.requested_semantic_hash is None
                or job.calculation_contract_version != SCENARIO_CALCULATION_CONTRACT
            ):
                raise ScenarioWorkerError(
                    "SCENARIO_JOB_INVALID",
                    "Scenario job does not contain a complete semantic request",
                )
            payload = _request_payload(job.request_json)
            scenario = database.scalar(
                select(ScenarioRecord).where(ScenarioRecord.job_id == lease.job_id)
            )
            profile = database.get(ProfileVersionRecord, job.profile_version_id)
            if (
                scenario is None
                or scenario.owner_user_id != job.owner_user_id
                or scenario.profile_version_id != job.profile_version_id
                or scenario.lifecycle_state != "ACTIVE"
                or scenario.state != "RUNNING"
                or profile is None
                or profile.owner_user_id != job.owner_user_id
                or profile.id != payload["profile_version_id"]
                or profile.lifecycle_state != "ACTIVE"
            ):
                raise ScenarioWorkerError(
                    "SCENARIO_SCOPE_UNAVAILABLE",
                    "Scenario sources are outside the live fenced owner scope",
                )
            tariff_version_id = cast(str, payload["tariff_version_id"])
            tariff = self._tariffs.get(tariff_version_id)
            if tariff is None:
                raise ScenarioWorkerError(
                    "SCENARIO_TARIFF_UNKNOWN",
                    "Scenario tariff is unavailable",
                )
            if (
                not tariff.lock.scope.optimization_admitted
                or tariff.compilation.reports.solver_lowering_unsupported_reasons
            ):
                raise ScenarioWorkerError(
                    "SCENARIO_TARIFF_OPTIMIZATION_UNAVAILABLE",
                    "Scenario tariff no longer has a complete admitted optimization lowering",
                )
            try:
                account_facts = AccountFacts.model_validate_json(
                    json.dumps(payload["account_facts"])
                )
                dated_payload = payload["dated_eligibility_facts"]
                dated_facts = (
                    DatedEligibilityFacts.model_validate_json(json.dumps(dated_payload))
                    if dated_payload is not None
                    else None
                )
                scenario_input = ScenarioInput.model_validate_json(
                    json.dumps(payload["scenario_input"])
                )
                solver_configuration = SolverConfiguration.model_validate_json(
                    json.dumps(payload["solver_configuration"])
                )
            except ValidationError as error:
                raise ScenarioWorkerError(
                    "SCENARIO_REQUEST_INVALID",
                    "Scenario request failed schema validation",
                ) from error
            attestation_ids = cast(tuple[str, ...], payload["shift_existing_attestation_load_ids"])
            _validate_attestations(scenario_input, attestation_ids)
            _validate_profile(database, profile, scenario_input)
            input_hash = canonical_content_sha256(
                b"RateReplay.ScenarioInput.v1",
                scenario_input.model_dump(mode="json"),
            )
            try:
                stored_input = ScenarioInput.model_validate_json(scenario.input_json)
            except ValidationError as error:
                raise ScenarioWorkerError(
                    "SCENARIO_INPUT_MISMATCH",
                    "Stored scenario input failed schema validation",
                ) from error
            if (
                scenario.tariff_version_id != tariff_version_id
                or scenario_input.tariff_version_id != tariff_version_id
                or scenario.input_hash != input_hash
                or stored_input != scenario_input
            ):
                raise ScenarioWorkerError(
                    "SCENARIO_INPUT_MISMATCH",
                    "Scenario request differs from its immutable scenario record",
                )
            try:
                validated = validate_and_decompose_scenario(scenario_input)
                identity = scenario_semantic_identity(
                    tariff=tariff,
                    account_facts=account_facts,
                    dated_facts=dated_facts,
                    validated=validated,
                    solver_configuration=solver_configuration,
                    environment_lock_hash=self._environment_lock_hash,
                )
                if identity.sha256() != job.requested_semantic_hash:
                    raise ScenarioWorkerError(
                        "SCENARIO_SEMANTIC_IDENTITY_MISMATCH",
                        "Scenario request differs from its submitted semantic identity",
                    )
                solver_started = time.perf_counter()
                exact = optimize_exact(
                    validated,
                    tariff.compilation,
                    account_facts,
                    dated_facts=dated_facts,
                    configuration=solver_configuration,
                )
                solver_duration = time.perf_counter() - solver_started
                heuristic = optimize_off_peak_heuristic(
                    validated,
                    tariff.compilation,
                    account_facts,
                    dated_facts=dated_facts,
                    configuration=solver_configuration,
                )
                result = build_scenario_result(
                    validated,
                    tariff.compilation,
                    account_facts,
                    dated_facts,
                    exact,
                    heuristic,
                )
            except (
                OptimizationExecutionError,
                OptimizationLoweringError,
                ReplayError,
                ScenarioResultError,
                ScenarioValidationError,
                ScheduleVerificationError,
            ) as error:
                raise ScenarioWorkerError(error.code, str(error)) from error
            owner_user_id = job.owner_user_id
            operation_request_hash = job.request_hash
            semantic_hash = job.requested_semantic_hash
            profile_version_id = profile.id
            scenario_id = scenario.id
        result_id = secrets.token_hex(16)
        scenario_result = ScenarioResultRecord(
            id=result_id,
            owner_user_id=owner_user_id,
            scenario_id=scenario_id,
            profile_version_id=profile_version_id,
            job_id=lease.job_id,
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
            scenario_result_id=result_id,
            calculation_hash=result.manifest.calculation_sha256,
            manifest_json=result.manifest.model_dump_json(),
            created_at=now,
        )

        def publish_result(database: Session) -> None:
            current = database.scalar(
                select(ScenarioRecord).where(
                    ScenarioRecord.id == scenario_id,
                    ScenarioRecord.job_id == lease.job_id,
                    ScenarioRecord.owner_user_id == owner_user_id,
                    ScenarioRecord.lifecycle_state == "ACTIVE",
                    ScenarioRecord.state == "RUNNING",
                )
            )
            if current is None:
                raise ArtifactServiceError(
                    "STALE_SCENARIO_ATTEMPT",
                    "Scenario attempt lost its publication fence",
                )
            current.state = "SUCCEEDED"
            current.completed_at = now
            database.add(scenario_result)
            database.flush()
            database.add(manifest)

        self._artifacts.finalize(
            owner_user_id=owner_user_id,
            lease=lease,
            semantic_hash=semantic_hash,
            calculation_contract_version=SCENARIO_CALCULATION_CONTRACT,
            result_type="SCENARIO",
            result_id=result_id,
            artifact_registration_ids=(),
            now=now,
            publish_result=publish_result,
        )
        return len(scenario_input.loads), exact.search_status, solver_duration


def _request_payload(value: str) -> dict[str, object]:
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, TypeError) as error:
        raise ScenarioWorkerError(
            "SCENARIO_REQUEST_INVALID",
            "Scenario job request is not canonical JSON",
        ) from error
    if not isinstance(payload, dict):
        raise ScenarioWorkerError(
            "SCENARIO_REQUEST_INVALID",
            "Scenario job request schema is invalid",
        )
    required = {
        "account_facts",
        "dated_eligibility_facts",
        "profile_version_id",
        "scenario_input",
        "shift_existing_attestation_load_ids",
        "solver_configuration",
        "tariff_version_id",
    }
    attestations = payload.get("shift_existing_attestation_load_ids")
    if (
        set(payload) != required
        or not isinstance(payload["account_facts"], dict)
        or not (
            payload["dated_eligibility_facts"] is None
            or isinstance(payload["dated_eligibility_facts"], dict)
        )
        or not isinstance(payload["profile_version_id"], str)
        or not isinstance(payload["scenario_input"], dict)
        or not isinstance(payload["solver_configuration"], dict)
        or not isinstance(payload["tariff_version_id"], str)
        or not isinstance(attestations, list)
        or not all(isinstance(item, str) for item in attestations)
        or attestations != sorted(attestations)
        or len(attestations) != len(set(attestations))
    ):
        raise ScenarioWorkerError(
            "SCENARIO_REQUEST_INVALID",
            "Scenario job request schema is invalid",
        )
    payload["shift_existing_attestation_load_ids"] = tuple(attestations)
    return cast(dict[str, object], payload)


def _validate_attestations(
    scenario: ScenarioInput,
    attestation_ids: tuple[str, ...],
) -> None:
    try:
        provided = {UUID(item) for item in attestation_ids}
    except ValueError as error:
        raise ScenarioWorkerError(
            "SHIFT_EXISTING_ATTESTATION_MISMATCH",
            "Scenario attestations are invalid",
        ) from error
    expected = {load.load_id for load in scenario.loads if load.mode == "SHIFT_EXISTING"}
    if len(provided) != len(attestation_ids) or provided != expected:
        raise ScenarioWorkerError(
            "SHIFT_EXISTING_ATTESTATION_MISMATCH",
            "Scenario attestations differ from the submitted loads",
        )


def _validate_profile(
    database: Session,
    profile: ProfileVersionRecord,
    scenario: ScenarioInput,
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
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    expected_start = profile.billing_period_start_utc_ns
    stored: list[CanonicalProfileSlot] = []
    for record in records:
        if (
            record.start_utc_ns != expected_start
            or record.flow_direction != "IMPORT"
            or record.start_utc_ns % 1_000_000_000
            or record.duration_seconds > 3_600
        ):
            raise ScenarioWorkerError(
                "SCENARIO_PROFILE_MISMATCH",
                "Scenario intervals differ from the immutable confirmed profile",
            )
        stored.append(
            CanonicalProfileSlot(
                slot_start_utc=epoch + timedelta(seconds=record.start_utc_ns // 1_000_000_000),
                duration_seconds=record.duration_seconds,
                measured_energy_wh=record.energy_wh,
            )
        )
        expected_start += record.duration_seconds * 1_000_000_000
    if (
        scenario.profile_content_sha256 != profile.content_hash
        or scenario.profile_slots != tuple(stored)
        or not stored
        or expected_start != profile.billing_period_end_utc_ns
    ):
        raise ScenarioWorkerError(
            "SCENARIO_PROFILE_MISMATCH",
            "Scenario intervals differ from the immutable confirmed profile",
        )
