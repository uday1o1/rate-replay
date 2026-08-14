"""Owner-scoped durable scenario submission and semantic identity."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from ratereplay_domain.semantic_identity import SemanticCalculationIdentity
from ratereplay_optimizer.lowering import compile_scenario_model
from ratereplay_optimizer.models import SolverConfiguration, ValidatedScenario
from ratereplay_optimizer.verification import (
    candidate_from_reference,
    verify_candidate_schedule,
)
from ratereplay_tariffs.admission import AdmittedTariff
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.audit import append_audit_event
from ratereplay_persistence.calculations import (
    CalculationSubmission,
    CalculationSubmissionError,
    CalculationSubmissionService,
)
from ratereplay_persistence.models import (
    JobRecord,
    ScenarioLoadRecord,
    ScenarioRecord,
    ScenarioReferenceScheduleRecord,
)

SCENARIO_REQUEST_SCHEMA: Final = "scenario-operation-v1"
SCENARIO_CALCULATION_CONTRACT: Final = "verified-scenario-calculation-v1"
SCENARIO_BILLING_EVALUATOR_VERSION: Final = "interval-replay-evaluator-v1"
SCENARIO_HEURISTIC_CONTRACT: Final = "off-peak-heuristic-v1"
SCENARIO_SOLVER_LOWERING_VERSION: Final = "cp-sat-charge-lowering-v1"
SCENARIO_VERIFIER_VERSION: Final = "independent-schedule-verifier-v1"


class ScenarioServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ScenarioSubmission:
    scenario_id: str
    calculation: CalculationSubmission

    @property
    def job_id(self) -> str:
        return self.calculation.job_id


class ScenarioService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._submissions = CalculationSubmissionService(session_factory)

    def submit(
        self,
        *,
        owner_user_id: str,
        profile_version_id: str,
        idempotency_key: str,
        tariff: AdmittedTariff,
        account_facts: AccountFacts,
        dated_facts: DatedEligibilityFacts | None,
        validated: ValidatedScenario,
        attestation_load_ids: tuple[str, ...],
        solver_configuration: SolverConfiguration,
        environment_lock_hash: str,
        now: datetime,
    ) -> ScenarioSubmission:
        identity = scenario_semantic_identity(
            tariff=tariff,
            account_facts=account_facts,
            dated_facts=dated_facts,
            validated=validated,
            solver_configuration=solver_configuration,
            environment_lock_hash=environment_lock_hash,
        )

        def initialize_new_job(
            database: Session,
            calculation: CalculationSubmission,
        ) -> None:
            self._initialize_scenario(
                database,
                owner_user_id=owner_user_id,
                profile_version_id=profile_version_id,
                tariff_version_id=tariff.lock.tariff_version_id,
                validated=validated,
                calculation=calculation,
                now=now,
            )

        try:
            calculation = self._submissions.submit(
                owner_user_id=owner_user_id,
                profile_version_id=profile_version_id,
                job_kind="SCENARIO",
                request_schema_version=SCENARIO_REQUEST_SCHEMA,
                idempotency_key=idempotency_key,
                operation_payload={
                    "account_facts": account_facts.model_dump(mode="json"),
                    "dated_eligibility_facts": (
                        dated_facts.model_dump(mode="json") if dated_facts is not None else None
                    ),
                    "profile_version_id": profile_version_id,
                    "scenario_input": validated.scenario.model_dump(mode="json"),
                    "shift_existing_attestation_load_ids": sorted(attestation_load_ids),
                    "solver_configuration": solver_configuration.model_dump(mode="json"),
                    "tariff_version_id": tariff.lock.tariff_version_id,
                },
                semantic_identity=identity,
                now=now,
                initialize_new_job=initialize_new_job,
            )
        except CalculationSubmissionError as error:
            raise ScenarioServiceError(error.code, str(error)) from error
        scenario = self._ensure_scenario(
            owner_user_id=owner_user_id,
            profile_version_id=profile_version_id,
            tariff_version_id=tariff.lock.tariff_version_id,
            validated=validated,
            calculation=calculation,
            now=now,
        )
        return ScenarioSubmission(scenario_id=scenario.id, calculation=calculation)

    def cancel(
        self,
        *,
        owner_user_id: str,
        scenario_id: str,
        now: datetime,
    ) -> None:
        now = now.astimezone(UTC)
        with self._session_factory.begin() as database:
            scenario = database.scalar(
                select(ScenarioRecord)
                .where(
                    ScenarioRecord.id == scenario_id,
                    ScenarioRecord.owner_user_id == owner_user_id,
                    ScenarioRecord.lifecycle_state == "ACTIVE",
                )
                .with_for_update()
            )
            if scenario is None:
                raise ScenarioServiceError("SCENARIO_NOT_FOUND", "Scenario is unavailable")
            job = database.scalar(
                select(JobRecord).where(JobRecord.id == scenario.job_id).with_for_update()
            )
            if scenario.state in {"SUCCEEDED", "FAILED", "CANCELLED"} or job is None:
                raise ScenarioServiceError(
                    "SCENARIO_ALREADY_TERMINAL",
                    "A terminal scenario cannot be cancelled",
                )
            job.cancel_requested = True
            job.state = "CANCELLED"
            job.failure_code = "CANCELLED_BY_OWNER"
            job.completed_at = now
            scenario.state = "CANCELLED"
            scenario.completed_at = now
            append_audit_event(
                database,
                owner_user_id=owner_user_id,
                event_type="JOB_CANCELLED",
                subject_type="JOB",
                subject_id=job.id,
                sequence=job.fencing_generation,
                outcome="CANCELLED",
                now=now,
            )

    def _ensure_scenario(
        self,
        *,
        owner_user_id: str,
        profile_version_id: str,
        tariff_version_id: str,
        validated: ValidatedScenario,
        calculation: CalculationSubmission,
        now: datetime,
    ) -> ScenarioRecord:
        input_hash = canonical_content_sha256(
            b"RateReplay.ScenarioInput.v1",
            validated.scenario.model_dump(mode="json"),
        )
        try:
            with self._session_factory.begin() as database:
                return self._initialize_scenario(
                    database,
                    owner_user_id=owner_user_id,
                    profile_version_id=profile_version_id,
                    tariff_version_id=tariff_version_id,
                    validated=validated,
                    calculation=calculation,
                    now=now,
                )
        except IntegrityError as error:
            with self._session_factory() as database:
                existing = database.scalar(
                    select(ScenarioRecord).where(ScenarioRecord.job_id == calculation.job_id)
                )
                if existing is None:
                    raise ScenarioServiceError(
                        "SCENARIO_SUBMISSION_CONFLICT",
                        "Scenario submission could not resolve a concurrent request",
                    ) from error
                return _validated_existing_scenario(
                    existing,
                    owner_user_id=owner_user_id,
                    profile_version_id=profile_version_id,
                    tariff_version_id=tariff_version_id,
                    input_hash=input_hash,
                )

    def _initialize_scenario(
        self,
        database: Session,
        *,
        owner_user_id: str,
        profile_version_id: str,
        tariff_version_id: str,
        validated: ValidatedScenario,
        calculation: CalculationSubmission,
        now: datetime,
    ) -> ScenarioRecord:
        input_hash = canonical_content_sha256(
            b"RateReplay.ScenarioInput.v1",
            validated.scenario.model_dump(mode="json"),
        )
        existing = database.scalar(
            select(ScenarioRecord).where(ScenarioRecord.job_id == calculation.job_id)
        )
        if existing is not None:
            return _validated_existing_scenario(
                existing,
                owner_user_id=owner_user_id,
                profile_version_id=profile_version_id,
                tariff_version_id=tariff_version_id,
                input_hash=input_hash,
            )
        job = database.get(JobRecord, calculation.job_id)
        if (
            job is None
            or job.owner_user_id != owner_user_id
            or job.profile_version_id != profile_version_id
            or job.kind != "SCENARIO"
        ):
            raise ScenarioServiceError(
                "SCENARIO_JOB_INVALID",
                "Scenario calculation job is outside the owner scope",
            )
        scenario = ScenarioRecord(
            id=secrets.token_hex(16),
            owner_user_id=owner_user_id,
            profile_version_id=profile_version_id,
            job_id=job.id,
            tariff_version_id=tariff_version_id,
            operation_request_hash=calculation.operation_request_hash,
            input_hash=input_hash,
            input_json=validated.scenario.model_dump_json(),
            state=job.state,
            lifecycle_state="ACTIVE",
            lifecycle_generation=0,
            created_at=now.astimezone(UTC),
            completed_at=job.completed_at,
        )
        database.add(scenario)
        database.flush()
        loads, references = _load_records(scenario.id, validated)
        database.add_all(loads)
        database.flush()
        database.add_all(references)
        return scenario


def scenario_semantic_identity(
    *,
    tariff: AdmittedTariff,
    account_facts: AccountFacts,
    dated_facts: DatedEligibilityFacts | None,
    validated: ValidatedScenario,
    solver_configuration: SolverConfiguration,
    environment_lock_hash: str,
) -> SemanticCalculationIdentity:
    reference = verify_candidate_schedule(
        validated.scenario,
        candidate_from_reference(validated.scenario),
        tariff.compilation,
        account_facts,
        dated_facts=dated_facts,
    )
    lowered = compile_scenario_model(
        validated,
        tariff.compilation,
        account_facts,
        reference.billing_result,
    )
    scenario_hash = canonical_content_sha256(
        b"RateReplay.ScenarioInput.v1",
        validated.scenario.model_dump(mode="json"),
    )
    reference_hashes = tuple(
        canonical_content_sha256(
            b"RateReplay.LoadReferenceSchedules.v1",
            {
                "load_id": str(load.load_id),
                "occurrences": [
                    {
                        "occurrence_id": str(occurrence.occurrence_id),
                        "reference_schedule": [
                            slot.model_dump(mode="json") for slot in occurrence.reference_schedule
                        ],
                    }
                    for occurrence in load.occurrences
                ],
            },
        )
        for load in validated.scenario.loads
    )
    component_vector_hash = canonical_content_sha256(
        b"RateReplay.ComparisonComponentVector.v1",
        tariff.compilation.reports.component_vector.model_dump(mode="json"),
    )
    account_hash = canonical_content_sha256(
        b"RateReplay.ScenarioEligibilityFacts.v1",
        {
            "account_facts": account_facts.model_dump(mode="json"),
            "dated_eligibility_facts": (
                dated_facts.model_dump(mode="json") if dated_facts is not None else None
            ),
        },
    )
    configuration_hash = canonical_content_sha256(
        b"RateReplay.SolverConfiguration.v1",
        solver_configuration.model_dump(mode="json"),
    )
    heuristic_configuration_hash = canonical_content_sha256(
        b"RateReplay.HeuristicSolverConfiguration.v1",
        solver_configuration.model_dump(mode="json"),
    )
    capability_hash = canonical_content_sha256(
        b"RateReplay.SolverLoweringCapability.v1",
        {
            "supported_operators": (tariff.compilation.reports.solver_lowering_supported_operators),
            "unsupported_reasons": (tariff.compilation.reports.solver_lowering_unsupported_reasons),
        },
    )
    provenance_hash = canonical_content_sha256(
        b"RateReplay.TariffProvenanceVector.v1",
        tuple(
            sorted(source.source_sha256 for source in tariff.compilation.reports.source_coverage)
        ),
    )
    return SemanticCalculationIdentity(
        job_kind="SCENARIO",
        request_schema_version=SCENARIO_REQUEST_SCHEMA,
        calculation_contract_version=SCENARIO_CALCULATION_CONTRACT,
        environment_lock_hash=environment_lock_hash,
        tariff_compiler_version=tariff.compilation.bundle_version,
        billing_evaluator_version=SCENARIO_BILLING_EVALUATOR_VERSION,
        profile_version_hash=validated.scenario.profile_content_sha256,
        tariff_ast_hashes=(tariff.compilation.reports.normalized_ast_sha256,),
        component_vector_hashes=(component_vector_hash,),
        account_facts_hash=account_hash,
        billing_period_identity_hash=canonical_content_sha256(
            b"RateReplay.BillingPeriodIdentity.v1",
            account_facts.service_window.model_dump(mode="json"),
        ),
        scenario_and_reference_hashes=(
            scenario_hash,
            *reference_hashes,
            lowered.record.lowering_sha256,
            capability_hash,
            provenance_hash,
        ),
        heuristic_contract_version=SCENARIO_HEURISTIC_CONTRACT,
        heuristic_rank_calendar_hash=lowered.record.rank_calendar_sha256,
        heuristic_solver_configuration_hash=heuristic_configuration_hash,
        solver_lowering_version=SCENARIO_SOLVER_LOWERING_VERSION,
        solver_name_and_version=(
            f"{solver_configuration.solver_name} {solver_configuration.solver_version}"
        ),
        solver_configuration_hash=configuration_hash,
        verifier_version=SCENARIO_VERIFIER_VERSION,
    )


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
                        b"RateReplay.OccurrenceReferenceSchedule.v1",
                        schedule_payload,
                    ),
                    schedule_json=occurrence.model_dump_json(),
                )
            )
    return loads, references


def _validated_existing_scenario(
    scenario: ScenarioRecord,
    *,
    owner_user_id: str,
    profile_version_id: str,
    tariff_version_id: str,
    input_hash: str,
) -> ScenarioRecord:
    if (
        scenario.owner_user_id != owner_user_id
        or scenario.profile_version_id != profile_version_id
        or scenario.tariff_version_id != tariff_version_id
        or scenario.input_hash != input_hash
    ):
        raise ScenarioServiceError(
            "SCENARIO_OPERATION_MISMATCH",
            "Scenario operation is bound to different semantic inputs",
        )
    return scenario
