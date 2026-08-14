"""Versioned deny-by-default aggregate report construction."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Literal

from pydantic import Field
from ratereplay_optimizer.models import CandidateSchedule
from ratereplay_optimizer.results import ScenarioOptimizationResult
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_tariffs.schema import DateRange, FrozenModel

REDACTION_POLICY_VERSION = "redacted-report-policy-v1"
REPORT_TEMPLATE_VERSION = "redacted-report-template-v1"
REPORT_CONTRACT_VERSION = "redacted-report-contract-v1"


class ReportConstructionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RedactedChargeComponent(FrozenModel):
    component_key: str
    amount_cents: int


class RedactedTariffProvenance(FrozenModel):
    tariff_version_id: str
    tariff_ir_version: str
    compiler_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RedactedSolverSummary(FrozenModel):
    search_status: Literal["OPTIMAL", "BEST_FOUND"]
    selected_source: Literal["SOLVER_INCUMBENT", "REFERENCE"]
    verification_status: Literal["VALID"]
    verifier_version: str
    highest_objective_stage_proved_optimal: int = Field(ge=0, le=4)
    first_open_stage: int | None = Field(default=None, ge=1, le=4)


class RedactedReport(FrozenModel):
    schema_version: Literal["redacted-report-v1"] = "redacted-report-v1"
    redaction_policy_version: Literal["redacted-report-policy-v1"] = "redacted-report-policy-v1"
    report_template_version: Literal["redacted-report-template-v1"] = "redacted-report-template-v1"
    calculation_time_mode: Literal["HISTORICAL_REPLAY"] = "HISTORICAL_REPLAY"
    historical_addition_label: Literal["HISTORICAL_COUNTERFACTUAL_NOT_FORECAST"]
    billing_period: DateRange
    aggregate_measured_energy_wh: int = Field(ge=0)
    aggregate_reference_flexible_energy_wh: int = Field(ge=0)
    aggregate_shifted_energy_wh: int = Field(ge=0)
    selected_supported_cost_cents: int
    reference_supported_cost_cents: int
    supported_cost_difference_cents: int
    signed_unexplained_residual_cents: int | None
    supported_charge_components: tuple[RedactedChargeComponent, ...]
    unsupported_component_codes: tuple[str, ...]
    tariff_provenance: RedactedTariffProvenance
    solver: RedactedSolverSummary
    scenario_result_version: str
    scenario_result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    limitations: tuple[str, ...]
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def build_redacted_report(result: ScenarioOptimizationResult) -> RedactedReport:
    """Project a verified scenario result onto the reviewed aggregate allowlist."""

    selected = result.exact.selected
    reference = result.exact.reference
    service_windows = {
        line.contributing_service_window
        for line in (*selected.billing_result.line_items, *reference.billing_result.line_items)
    }
    if len(service_windows) != 1:
        raise ReportConstructionError(
            "REPORT_BILLING_PERIOD_AMBIGUOUS",
            "The verified result does not identify one admitted billing period",
        )
    billing_period = service_windows.pop()
    shifted_energy = _shifted_energy_wh(selected.schedule, reference.schedule)
    charge_totals: defaultdict[str, int] = defaultdict(int)
    for line in selected.billing_result.line_items:
        charge_totals[line.charge_component_key] += line.rounded_cents
    components = tuple(
        RedactedChargeComponent(component_key=key, amount_cents=value)
        for key, value in sorted(charge_totals.items())
    )
    unsupported = {
        placeholder.reason_code
        for placeholder in selected.billing_result.tariff_unsupported_placeholders
    }
    if selected.billing_result.user_unsupported_lines:
        unsupported.add("USER_ENTERED_UNSUPPORTED_COMPONENTS_EXCLUDED")
    reconciliation = selected.billing_result.reconciliation
    limitations = {
        "NO_DAILY_OR_INTERVAL_SERIES",
        "NO_EXACT_LOAD_SCHEDULE",
        "SUPPORTED_CHARGES_ONLY",
        "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST",
    }
    if result.exact.search_status == "BEST_FOUND":
        limitations.add("BEST_FOUND_WITH_OPEN_BOUND")
    payload = {
        "historical_addition_label": result.historical_addition_label,
        "billing_period": billing_period.model_dump(mode="json"),
        "aggregate_measured_energy_wh": sum(
            slot.energy_wh for slot in result.decomposition.reconstructed_measured_profile
        ),
        "aggregate_reference_flexible_energy_wh": sum(
            slot.energy_wh
            for slot in (
                *result.decomposition.shift_existing_reference,
                *result.decomposition.historical_addition_reference,
            )
        ),
        "aggregate_shifted_energy_wh": shifted_energy,
        "selected_supported_cost_cents": selected.billing_result.supported_calculated_cents,
        "reference_supported_cost_cents": reference.billing_result.supported_calculated_cents,
        "supported_cost_difference_cents": (
            reference.billing_result.supported_calculated_cents
            - selected.billing_result.supported_calculated_cents
        ),
        "signed_unexplained_residual_cents": (
            reconciliation.unexplained_residual_cents if reconciliation is not None else None
        ),
        "supported_charge_components": tuple(
            component.model_dump(mode="json") for component in components
        ),
        "unsupported_component_codes": tuple(sorted(unsupported)),
        "tariff_provenance": {
            "tariff_version_id": result.manifest.tariff_version_id,
            "tariff_ir_version": result.manifest.tariff_ir_version,
            "compiler_content_sha256": result.manifest.tariff_compiler_content_sha256,
        },
        "solver": {
            "search_status": result.exact.search_status,
            "selected_source": result.exact.selected_source,
            "verification_status": selected.verification.status,
            "verifier_version": selected.verification.verification_version,
            "highest_objective_stage_proved_optimal": (
                result.exact.highest_objective_stage_proved_optimal
            ),
            "first_open_stage": result.exact.first_open_stage,
        },
        "scenario_result_version": result.result_version,
        "scenario_result_sha256": result.result_sha256,
        "limitations": tuple(sorted(limitations)),
    }
    report_hash = canonical_content_sha256(b"RateReplay.RedactedReport.v1", payload)
    return RedactedReport.model_validate_json(json.dumps({**payload, "report_sha256": report_hash}))


def _shifted_energy_wh(candidate: CandidateSchedule, reference: CandidateSchedule) -> int:
    candidate_values = _schedule_values(candidate)
    reference_values = _schedule_values(reference)
    if set(candidate_values) != set(reference_values):
        raise ReportConstructionError(
            "REPORT_SCHEDULE_IDENTITY_MISMATCH",
            "Selected and reference schedules do not share one canonical slot identity",
        )
    absolute_change = sum(
        abs(candidate_values[key] - reference_values[key]) for key in candidate_values
    )
    if absolute_change % 2:
        raise ReportConstructionError(
            "REPORT_SHIFTED_ENERGY_NONINTEGRAL",
            "Aggregate shifted energy cannot be represented as integer watt-hours",
        )
    return absolute_change // 2


def _schedule_values(schedule: CandidateSchedule) -> dict[tuple[str, str, int], int]:
    values: dict[tuple[str, str, int], int] = {}
    for occurrence in schedule.occurrences:
        for slot in occurrence.slots:
            key = (
                str(occurrence.occurrence_id),
                slot.slot_start_utc.isoformat(),
                slot.duration_seconds,
            )
            if key in values:
                raise ReportConstructionError(
                    "REPORT_SCHEDULE_IDENTITY_DUPLICATE",
                    "A schedule contains a duplicate canonical slot identity",
                )
            values[key] = slot.energy_wh
    return values
