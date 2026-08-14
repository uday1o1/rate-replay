"""Authenticated historical flexible-load scenario routes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from ratereplay_optimizer.lowering import OptimizationLoweringError
from ratereplay_optimizer.models import (
    CanonicalProfileSlot,
    FlexibleLoad,
    ScenarioElectricalConstraints,
    ScenarioInput,
)
from ratereplay_optimizer.results import (
    ScenarioOptimizationResult,
    ScenarioResultError,
    build_scenario_result,
)
from ratereplay_optimizer.scenario import ScenarioValidationError, validate_and_decompose_scenario
from ratereplay_optimizer.solver import (
    OptimizationExecutionError,
    default_solver_configuration,
    optimize_exact,
    optimize_off_peak_heuristic,
)
from ratereplay_persistence.models import (
    ImportReadingRecord,
    ProfileVersionRecord,
    ScenarioRecord,
    ScenarioResultRecord,
)
from ratereplay_persistence.scenarios import ScenarioService, ScenarioServiceError
from ratereplay_tariffs.admission import AdmittedTariff
from ratereplay_tariffs.billing import ReplayError
from ratereplay_tariffs.hashing import canonical_content_sha256, canonical_json_bytes
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts, FrozenModel
from sqlalchemy import BigInteger, select
from sqlalchemy import cast as sql_cast
from sqlalchemy.orm import Session

from ratereplay_api.auth import AuthenticatedSession
from ratereplay_api.auth_routes import (
    get_authenticated_session,
    get_database,
    require_csrf_session,
)
from ratereplay_api.problems import ApiProblem, problem_openapi_responses
from ratereplay_api.replay_routes import profile_window


def _default_electrical_constraints() -> dict[str, object]:
    return {"energy_basis": "METER_SIDE"}


class CreateScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_schema_version: Literal["scenario-operation-v1"]
    profile_version_id: str = Field(min_length=1)
    tariff_version_id: str = Field(min_length=1)
    account_facts: dict[str, object]
    dated_eligibility_facts: dict[str, object] | None = None
    electrical_constraints: dict[str, object] = Field(
        default_factory=_default_electrical_constraints
    )
    loads: tuple[dict[str, object], ...] = Field(min_length=1, max_length=5)
    shift_existing_attestation_load_ids: tuple[str, ...] = ()


class ProfileScenarioSlotsResponse(FrozenModel):
    schema_version: Literal["profile-scenario-slots-v1"] = "profile-scenario-slots-v1"
    profile_version_id: str
    profile_content_sha256: str
    calculation_time_mode: Literal["HISTORICAL_REPLAY"] = "HISTORICAL_REPLAY"
    energy_basis: Literal["METER_SIDE"] = "METER_SIDE"
    slots: tuple[CanonicalProfileSlot, ...]


class ScenarioResourceResponse(FrozenModel):
    schema_version: Literal["scenario-resource-v1"] = "scenario-resource-v1"
    scenario_id: str
    result_id: str
    job_id: str
    owner_user_id: str
    profile_version_id: str
    tariff_version_id: str
    state: str
    lifecycle_state: str
    created_at: str
    repeated: bool
    result: ScenarioOptimizationResult


class ScenarioCancelResponse(FrozenModel):
    schema_version: Literal["scenario-cancel-v1"] = "scenario-cancel-v1"
    scenario_id: str
    state: Literal["CANCEL_REQUESTED"] = "CANCEL_REQUESTED"


def _tariffs(request: Request) -> dict[str, AdmittedTariff]:
    return cast(dict[str, AdmittedTariff], request.app.state.admitted_tariffs)


def _scenarios(request: Request) -> ScenarioService:
    return cast(ScenarioService, request.app.state.scenario_service)


def _iso(value: datetime) -> str:
    return (value.replace(tzinfo=UTC) if value.tzinfo is None else value).isoformat()


def _profile_slots(
    database: Session,
    profile: ProfileVersionRecord,
) -> tuple[CanonicalProfileSlot, ...]:
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
    slots: list[CanonicalProfileSlot] = []
    for record in records:
        if (
            record.start_utc_ns != expected_start
            or record.flow_direction != "IMPORT"
            or record.start_utc_ns % 1_000_000_000
            or record.duration_seconds > 3_600
        ):
            raise ReplayError(
                "PROFILE_INTERVAL_COVERAGE_MISMATCH",
                "Confirmed profile intervals are not complete supported import coverage.",
            )
        slots.append(
            CanonicalProfileSlot(
                slot_start_utc=epoch + timedelta(seconds=record.start_utc_ns // 1_000_000_000),
                duration_seconds=record.duration_seconds,
                measured_energy_wh=record.energy_wh,
            )
        )
        expected_start += record.duration_seconds * 1_000_000_000
    if not slots or expected_start != profile.billing_period_end_utc_ns:
        raise ReplayError(
            "PROFILE_INTERVAL_COVERAGE_MISMATCH",
            "Confirmed profile intervals do not cover the complete billing period.",
        )
    return tuple(slots)


def _owned_profile(
    database: Session,
    owner_user_id: str,
    profile_version_id: str,
) -> ProfileVersionRecord:
    profile = database.scalar(
        select(ProfileVersionRecord).where(
            ProfileVersionRecord.id == profile_version_id,
            ProfileVersionRecord.owner_user_id == owner_user_id,
            ProfileVersionRecord.lifecycle_state == "ACTIVE",
        )
    )
    if profile is None:
        raise ApiProblem(
            status_code=404, code="PROFILE_NOT_FOUND", message="Profile is unavailable"
        )
    return profile


def _problem(
    error: ScenarioServiceError
    | ScenarioValidationError
    | OptimizationLoweringError
    | OptimizationExecutionError
    | ScenarioResultError
    | ReplayError,
) -> ApiProblem:
    statuses = {
        "EXACT_SOLVER_MODEL_CONTRACT_VIOLATION": 500,
        "EXACT_SOLVER_MODEL_INVALID": 500,
        "EXACT_SOLVER_UNKNOWN": 503,
        "EXACT_SOLVER_UNVERIFIED_INCUMBENT": 500,
        "HEURISTIC_UNVERIFIED_INCUMBENT": 500,
        "IDEMPOTENCY_KEY_REUSED": 409,
        "INVALID_IDEMPOTENCY_KEY": 422,
        "OPERATION_INCOMPLETE": 409,
        "OWNER_NOT_ACTIVE": 409,
        "PROFILE_INPUT_MISMATCH": 409,
        "PROFILE_INTERVAL_COVERAGE_MISMATCH": 422,
        "PROFILE_NOT_FOUND": 404,
        "SCENARIO_ALREADY_TERMINAL": 409,
        "SCENARIO_CANCELLATION_UNAVAILABLE": 409,
        "SCENARIO_NOT_FOUND": 404,
        "SCENARIO_PUBLICATION_CONFLICT": 409,
        "SCENARIO_RESULT_INPUT_MISMATCH": 500,
        "TARIFF_INELIGIBLE": 422,
        "TARIFF_OPTIMIZATION_UNAVAILABLE": 422,
        "TARIFF_UNKNOWN": 422,
    }
    witness = getattr(error, "witness", {})
    return ApiProblem(
        status_code=statuses.get(error.code, 500 if "MODEL" in error.code else 422),
        code=error.code,
        message=str(error),
        witness=cast(dict[str, object], witness),
    )


def _resource(
    scenario: ScenarioRecord,
    scenario_result: ScenarioResultRecord,
    *,
    repeated: bool,
) -> ScenarioResourceResponse:
    return ScenarioResourceResponse(
        scenario_id=scenario.id,
        result_id=scenario_result.id,
        job_id=scenario.job_id,
        owner_user_id=scenario.owner_user_id,
        profile_version_id=scenario.profile_version_id,
        tariff_version_id=scenario.tariff_version_id,
        state=scenario.state,
        lifecycle_state=scenario.lifecycle_state,
        created_at=_iso(scenario.created_at),
        repeated=repeated,
        result=ScenarioOptimizationResult.model_validate_json(scenario_result.result_json),
    )


def _stored_resource(
    database: Session,
    owner_user_id: str,
    scenario_id: str,
    *,
    repeated: bool,
) -> ScenarioResourceResponse:
    scenario = database.scalar(
        select(ScenarioRecord).where(
            ScenarioRecord.id == scenario_id,
            ScenarioRecord.owner_user_id == owner_user_id,
            ScenarioRecord.lifecycle_state == "ACTIVE",
        )
    )
    if scenario is None:
        raise ApiProblem(
            status_code=404, code="SCENARIO_NOT_FOUND", message="Scenario is unavailable"
        )
    scenario_result = database.scalar(
        select(ScenarioResultRecord).where(
            ScenarioResultRecord.scenario_id == scenario.id,
            ScenarioResultRecord.owner_user_id == owner_user_id,
            ScenarioResultRecord.lifecycle_state == "ACTIVE",
        )
    )
    if scenario_result is None:
        raise ApiProblem(
            status_code=409,
            code="SCENARIO_RESULT_INCOMPLETE",
            message="Scenario result is incomplete",
        )
    return _resource(scenario, scenario_result, repeated=repeated)


router = APIRouter(tags=["scenarios"])


@router.get(
    "/v1/profiles/{profile_id}/scenario-slots",
    response_model=ProfileScenarioSlotsResponse,
    responses=problem_openapi_responses(401, 404, 422),
)
def get_profile_scenario_slots(
    profile_id: str,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    database: Annotated[Session, Depends(get_database)],
) -> ProfileScenarioSlotsResponse:
    profile = _owned_profile(database, authenticated.user_id, profile_id)
    try:
        slots = _profile_slots(database, profile)
    except ReplayError as error:
        raise _problem(error) from error
    return ProfileScenarioSlotsResponse(
        profile_version_id=profile.id,
        profile_content_sha256=profile.content_hash,
        slots=slots,
    )


@router.post(
    "/v1/scenarios",
    response_model=ScenarioResourceResponse,
    status_code=status.HTTP_201_CREATED,
    responses=problem_openapi_responses(401, 403, 404, 409, 422, 500, 503),
)
def create_scenario(
    payload: CreateScenarioRequest,
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(require_csrf_session)],
    database: Annotated[Session, Depends(get_database)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
) -> ScenarioResourceResponse:
    admitted = _tariffs(request).get(payload.tariff_version_id)
    if admitted is None:
        raise ApiProblem(status_code=404, code="TARIFF_NOT_FOUND", message="Tariff is unavailable")
    if (
        not admitted.lock.scope.optimization_admitted
        or admitted.compilation.reports.solver_lowering_unsupported_reasons
    ):
        raise ApiProblem(
            status_code=422,
            code="TARIFF_OPTIMIZATION_UNAVAILABLE",
            message="Tariff does not have a complete exact optimization lowering.",
            witness={"reasons": admitted.compilation.reports.solver_lowering_unsupported_reasons},
        )
    profile = _owned_profile(database, authenticated.user_id, payload.profile_version_id)
    try:
        try:
            account_facts = AccountFacts.model_validate_json(
                canonical_json_bytes(payload.account_facts)
            )
            dated_facts = (
                DatedEligibilityFacts.model_validate_json(
                    canonical_json_bytes(payload.dated_eligibility_facts)
                )
                if payload.dated_eligibility_facts is not None
                else None
            )
            loads = tuple(
                FlexibleLoad.model_validate_json(canonical_json_bytes(item))
                for item in payload.loads
            )
            electrical_constraints = ScenarioElectricalConstraints.model_validate_json(
                canonical_json_bytes(payload.electrical_constraints)
            )
            attestation_ids = tuple(
                UUID(value) for value in payload.shift_existing_attestation_load_ids
            )
        except (ValidationError, ValueError) as error:
            raise ReplayError(
                "SCENARIO_REQUEST_INVALID",
                "Scenario account or dated eligibility facts are invalid.",
            ) from error
        if profile_window(profile) != account_facts.service_window:
            raise ReplayError(
                "PROFILE_ACCOUNT_WINDOW_MISMATCH",
                "Account facts do not describe the confirmed profile billing period.",
            )
        expected_attestations = {load.load_id for load in loads if load.mode == "SHIFT_EXISTING"}
        provided_attestations = set(attestation_ids)
        if len(provided_attestations) != len(attestation_ids) or (
            provided_attestations != expected_attestations
        ):
            raise ScenarioValidationError(
                "SHIFT_EXISTING_ATTESTATION_MISMATCH",
                "Every existing load requires one exact user-supplied reference attestation.",
                missing=tuple(
                    sorted(str(value) for value in expected_attestations - provided_attestations)
                ),
                unexpected=tuple(
                    sorted(str(value) for value in provided_attestations - expected_attestations)
                ),
            )
        scenario_input = ScenarioInput(
            scenario_version="historical-flex-scenario-v1",
            profile_content_sha256=profile.content_hash,
            tariff_version_id=payload.tariff_version_id,
            profile_slots=_profile_slots(database, profile),
            loads=loads,
            electrical_constraints=electrical_constraints,
        )
        validated = validate_and_decompose_scenario(scenario_input)
        operation_hash = canonical_content_sha256(
            b"RateReplay.ScenarioOperationRequest.v1",
            {
                "request_schema_version": payload.request_schema_version,
                "profile_version_id": profile.id,
                "account_facts": account_facts.model_dump(mode="json"),
                "dated_eligibility_facts": (
                    dated_facts.model_dump(mode="json") if dated_facts is not None else None
                ),
                "scenario": scenario_input.model_dump(mode="json"),
                "shift_existing_attestation_load_ids": tuple(
                    sorted(str(value) for value in provided_attestations)
                ),
            },
        )
        configuration = default_solver_configuration(max_deterministic_time_per_stage=5.0)
        exact = optimize_exact(
            validated,
            admitted.compilation,
            account_facts,
            dated_facts=dated_facts,
            configuration=configuration,
        )
        heuristic = optimize_off_peak_heuristic(
            validated,
            admitted.compilation,
            account_facts,
            dated_facts=dated_facts,
            configuration=configuration,
        )
        result = build_scenario_result(
            validated,
            admitted.compilation,
            account_facts,
            dated_facts,
            exact,
            heuristic,
        )
        stored = _scenarios(request).publish(
            owner_user_id=authenticated.user_id,
            profile_version_id=profile.id,
            idempotency_key=idempotency_key,
            operation_request_hash=operation_hash,
            validated=validated,
            result=result,
            now=datetime.now(UTC),
        )
    except (
        OptimizationExecutionError,
        OptimizationLoweringError,
        ReplayError,
        ScenarioResultError,
        ScenarioServiceError,
        ScenarioValidationError,
    ) as error:
        raise _problem(error) from error
    return _stored_resource(
        database,
        authenticated.user_id,
        stored.scenario_id,
        repeated=stored.repeated,
    )


@router.get(
    "/v1/scenarios/{scenario_id}",
    response_model=ScenarioResourceResponse,
    responses=problem_openapi_responses(401, 404, 409),
)
def get_scenario(
    scenario_id: str,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    database: Annotated[Session, Depends(get_database)],
) -> ScenarioResourceResponse:
    return _stored_resource(database, authenticated.user_id, scenario_id, repeated=False)


@router.post(
    "/v1/scenarios/{scenario_id}/cancel",
    response_model=ScenarioCancelResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses=problem_openapi_responses(401, 403, 404, 409),
)
def cancel_scenario(
    scenario_id: str,
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(require_csrf_session)],
) -> ScenarioCancelResponse:
    try:
        _scenarios(request).cancel(
            owner_user_id=authenticated.user_id,
            scenario_id=scenario_id,
        )
    except ScenarioServiceError as error:
        raise _problem(error) from error
    return ScenarioCancelResponse(scenario_id=scenario_id)
