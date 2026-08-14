"""Owner-scoped durable comparison submission and semantic identity."""

from __future__ import annotations

from datetime import datetime
from typing import Final

from ratereplay_domain.semantic_identity import SemanticCalculationIdentity
from ratereplay_tariffs.admission import AdmittedTariff
from ratereplay_tariffs.billing import IntervalReplayRequest
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_tariffs.schema import ChargeComponentKey
from sqlalchemy.orm import Session, sessionmaker

from ratereplay_persistence.calculations import (
    CalculationSubmission,
    CalculationSubmissionError,
    CalculationSubmissionService,
)
from ratereplay_persistence.models import (
    JobRecord,
    ReplayResultRecord,
)

COMPARISON_REQUEST_SCHEMA: Final = "comparison-operation-v1"
COMPARISON_CALCULATION_CONTRACT: Final = "tariff-comparison-calculation-v1"
COMPARISON_COVERAGE_VERSION: Final = "candidate-admission-matrix-v1"
COMPARISON_EVALUATOR_VERSION: Final = "alternative-plan-replay-evaluator-v1"


class ComparisonServiceError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ComparisonService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory
        self._submissions = CalculationSubmissionService(session_factory)

    def submit(
        self,
        *,
        owner_user_id: str,
        profile_version_id: str,
        current_replay_id: str,
        idempotency_key: str,
        tariffs: tuple[AdmittedTariff, ...],
        comparison_request: IntervalReplayRequest,
        required_component_keys: tuple[ChargeComponentKey, ...],
        environment_lock_hash: str,
        now: datetime,
    ) -> CalculationSubmission:
        with self._session_factory() as database:
            replay = database.get(ReplayResultRecord, current_replay_id)
            replay_job = database.get(JobRecord, replay.job_id) if replay is not None else None
            if (
                replay is None
                or replay.owner_user_id != owner_user_id
                or replay.profile_version_id != profile_version_id
                or replay.lifecycle_state != "ACTIVE"
                or replay_job is None
                or replay_job.state != "SUCCEEDED"
            ):
                raise ComparisonServiceError(
                    "REPLAY_NOT_FOUND",
                    "Current replay is unavailable",
                )
            current_replay_result_hash = replay.result_hash
        identity = comparison_semantic_identity(
            tariffs=tariffs,
            comparison_request=comparison_request,
            current_replay_result_hash=current_replay_result_hash,
            required_component_keys=required_component_keys,
            environment_lock_hash=environment_lock_hash,
        )
        try:
            return self._submissions.submit(
                owner_user_id=owner_user_id,
                profile_version_id=profile_version_id,
                job_kind="COMPARISON",
                request_schema_version=COMPARISON_REQUEST_SCHEMA,
                idempotency_key=idempotency_key,
                operation_payload={
                    "candidate_tariff_version_ids": sorted(
                        tariff.lock.tariff_version_id for tariff in tariffs
                    ),
                    "comparison_request": comparison_request.model_dump(mode="json"),
                    "current_replay_id": current_replay_id,
                    "profile_version_id": profile_version_id,
                },
                semantic_identity=identity,
                now=now,
            )
        except CalculationSubmissionError as error:
            raise ComparisonServiceError(error.code, str(error)) from error


def comparison_semantic_identity(
    *,
    tariffs: tuple[AdmittedTariff, ...],
    comparison_request: IntervalReplayRequest,
    current_replay_result_hash: str,
    required_component_keys: tuple[ChargeComponentKey, ...],
    environment_lock_hash: str,
) -> SemanticCalculationIdentity:
    ordered = tuple(sorted(tariffs, key=lambda item: item.lock.tariff_version_id))
    component_vectors = tuple(
        canonical_content_sha256(
            b"RateReplay.ComparisonComponentVector.v1",
            tariff.compilation.reports.component_vector.model_dump(mode="json"),
        )
        for tariff in ordered
    )
    dated_facts_hash = canonical_content_sha256(
        b"RateReplay.DatedEligibilityFacts.v1",
        (
            comparison_request.dated_eligibility_facts.model_dump(mode="json")
            if comparison_request.dated_eligibility_facts is not None
            else None
        ),
    )
    coverage_hash = canonical_content_sha256(
        b"RateReplay.ComparisonCoverage.v1",
        {
            "coverage_version": COMPARISON_COVERAGE_VERSION,
            "required_component_keys": required_component_keys,
        },
    )
    return SemanticCalculationIdentity(
        job_kind="COMPARISON",
        request_schema_version=COMPARISON_REQUEST_SCHEMA,
        calculation_contract_version=COMPARISON_CALCULATION_CONTRACT,
        environment_lock_hash=environment_lock_hash,
        tariff_compiler_version="|".join(
            sorted({tariff.compilation.bundle_version for tariff in ordered})
        ),
        billing_evaluator_version=COMPARISON_EVALUATOR_VERSION,
        profile_version_hash=comparison_request.profile_content_sha256,
        tariff_ast_hashes=tuple(
            tariff.compilation.reports.normalized_ast_sha256 for tariff in ordered
        ),
        component_vector_hashes=component_vectors,
        account_facts_hash=canonical_content_sha256(
            b"RateReplay.AccountFacts.v1",
            comparison_request.account_facts.model_dump(mode="json"),
        ),
        billing_period_identity_hash=canonical_content_sha256(
            b"RateReplay.BillingPeriodIdentity.v1",
            comparison_request.account_facts.service_window.model_dump(mode="json"),
        ),
        comparison_coverage_version=COMPARISON_COVERAGE_VERSION,
        scenario_and_reference_hashes=(
            current_replay_result_hash,
            dated_facts_hash,
            coverage_hash,
        ),
    )
