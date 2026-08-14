"""Exact CP-SAT lowering for admitted historical tariff scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import NoReturn, cast
from uuid import UUID

from ortools.sat.python import cp_model
from ratereplay_tariffs.billing import (
    ReplayError,
    ReplayResult,
    classify_interval_period,
    resolve_time_schedule,
    rule_applies,
)
from ratereplay_tariffs.compiled import (
    CompilationBundle,
    IRBaselineAllowance,
    IRExplicitUnsupportedCharge,
    IRFixedDailyCharge,
    IRFixedMonthlyCharge,
    IRTieredEnergyCharge,
    IRTimeOfUseEnergyCharge,
    IRTimeOfUseSchedule,
)
from ratereplay_tariffs.hashing import canonical_content_sha256
from ratereplay_tariffs.schema import AccountFacts

from ratereplay_optimizer.models import (
    FlexibleLoad,
    InterruptibleModulatingSpec,
    LoadOccurrence,
    LoweringRecord,
    ObjectiveBounds,
    OmittedChargeProof,
    ScenarioInput,
    ValidatedScenario,
)

SIGNED_INT64_MAX = 2**63 - 1
WATT_SECONDS_PER_WATT_HOUR = 3_600
WATT_HOUR_RATE_TO_CENTS_DENOMINATOR = 10_000_000
HALF_CENT_NUMERATOR = WATT_HOUR_RATE_TO_CENTS_DENOMINATOR // 2


class OptimizationLoweringError(ValueError):
    def __init__(self, code: str, message: str, **witness: object) -> None:
        super().__init__(message)
        self.code = code
        self.witness = witness


def _fail(code: str, message: str, **witness: object) -> NoReturn:
    raise OptimizationLoweringError(code, message, **witness)


@dataclass(frozen=True, slots=True)
class LoweredObjectives:
    supported_cost: cp_model.LinearExpr
    changed_occurrence_slots: cp_model.LinearExpr
    completion_slot_index_sum: cp_model.LinearExpr
    stable_slot_order_score: cp_model.LinearExpr
    proxy_rank_score: cp_model.LinearExpr


@dataclass(frozen=True, slots=True)
class LoweredScenarioModel:
    model: cp_model.CpModel
    energy_by_occurrence: dict[UUID, tuple[cp_model.IntVar, ...]]
    objectives: LoweredObjectives
    record: LoweringRecord


def _canonical_occurrences(
    scenario: ScenarioInput,
) -> tuple[tuple[FlexibleLoad, LoadOccurrence], ...]:
    values = tuple((load, occurrence) for load in scenario.loads for occurrence in load.occurrences)
    return tuple(
        sorted(
            values,
            key=lambda item: (
                item[1].deadline_utc,
                item[1].earliest_start_utc,
                item[0].load_id.bytes,
                item[1].occurrence_id.bytes,
            ),
        )
    )


def _check_int64(**bounds: int) -> None:
    for name, value in bounds.items():
        if abs(value) > SIGNED_INT64_MAX:
            _fail(
                "INT64_OBJECTIVE_BOUND",
                "A solver variable or expression exceeds signed 64-bit safety",
                bound_name=name,
                bound_value=value,
            )


def _ceil_div(numerator: int, denominator: int) -> int:
    return (numerator + denominator - 1) // denominator


def _window_indices(
    scenario: ScenarioInput,
    occurrence: LoadOccurrence,
) -> tuple[int, int]:
    boundaries = {slot.slot_start_utc: index for index, slot in enumerate(scenario.profile_slots)}
    final = scenario.profile_slots[-1]
    boundaries[final.slot_start_utc + timedelta(seconds=final.duration_seconds)] = len(
        scenario.profile_slots
    )
    try:
        return boundaries[occurrence.earliest_start_utc], boundaries[occurrence.deadline_utc]
    except KeyError:
        _fail(
            "MODEL_CONTRACT_VIOLATION",
            "A validated occurrence boundary is absent from the canonical profile",
            occurrence_id=str(occurrence.occurrence_id),
        )


def _reference_energy(occurrence: LoadOccurrence) -> tuple[int, ...]:
    return tuple(slot.energy_wh for slot in occurrence.reference_schedule)


def _preflight_signed_int64_safety(
    scenario: ScenarioInput,
    bundle: CompilationBundle,
    account_facts: AccountFacts,
    background: tuple[int, ...],
    occurrences: tuple[tuple[FlexibleLoad, LoadOccurrence], ...],
) -> None:
    service_end = account_facts.service_window.end
    service_start = account_facts.service_window.start
    if service_end is None:
        _fail("UNBOUNDED_BILLING_PERIOD", "Optimization requires a bounded billing period")
    billing_days = (service_end - service_start).days
    stable_maximum = 0
    flexible_slot_maxima = [0] * len(scenario.profile_slots)
    vector_position = 0
    for load, occurrence in occurrences:
        start, end = _window_indices(scenario, occurrence)
        spec = load.execution_spec
        for index, slot in enumerate(scenario.profile_slots):
            if isinstance(spec, InterruptibleModulatingSpec):
                maximum = 0
                if start <= index < end:
                    maximum_power_product = spec.maximum_power_w * slot.duration_seconds
                    minimum_power_product = spec.minimum_power_when_active_w * slot.duration_seconds
                    _check_int64(
                        maximum_power_product=maximum_power_product,
                        minimum_power_product=minimum_power_product,
                    )
                    maximum = maximum_power_product // WATT_SECONDS_PER_WATT_HOUR
            else:
                maximum = max(spec.fixed_slot_shape_wh, default=0)
            vector_position += 1
            stable_maximum += maximum * vector_position
            flexible_slot_maxima[index] += maximum
        _check_int64(required_energy_wh=occurrence.required_energy_wh)
        for reference in occurrence.reference_schedule:
            _check_int64(reference_energy_wh=reference.energy_wh)
    invariant_energy = sum(background) + sum(
        occurrence.required_energy_wh for _, occurrence in occurrences
    )
    profile_maxima = tuple(
        background[index] + flexible_slot_maxima[index] for index in range(len(background))
    )
    constraints = scenario.electrical_constraints
    for index, slot in enumerate(scenario.profile_slots):
        values = {
            "profile_energy_product": profile_maxima[index] * WATT_SECONDS_PER_WATT_HOUR,
            "flexible_energy_product": flexible_slot_maxima[index] * WATT_SECONDS_PER_WATT_HOUR,
        }
        if constraints.site_import_cap_w is not None:
            values["site_import_cap_product"] = (
                constraints.site_import_cap_w * slot.duration_seconds
            )
        if constraints.flexible_load_aggregate_cap_w is not None:
            values["flexible_load_cap_product"] = (
                constraints.flexible_load_aggregate_cap_w * slot.duration_seconds
            )
        _check_int64(**values)
    monetary_numerator_bound = 0
    for operator in bundle.ir.operators:
        if isinstance(operator, IRTieredEnergyCharge):
            monetary_numerator_bound += invariant_energy * max(
                abs(tier.rate_microdollars_per_kwh) for tier in operator.tiers
            )
        elif isinstance(operator, IRTimeOfUseEnergyCharge):
            monetary_numerator_bound += invariant_energy * sum(
                abs(rate.rate_microdollars_per_kwh) for rate in operator.period_rates
            )
            if operator.baseline_credit_microdollars_per_kwh is not None:
                monetary_numerator_bound += invariant_energy * abs(
                    operator.baseline_credit_microdollars_per_kwh
                )
        elif isinstance(operator, IRFixedDailyCharge):
            monetary_numerator_bound += billing_days * abs(operator.rate_microdollars_per_day)
        elif isinstance(operator, IRFixedMonthlyCharge):
            monetary_numerator_bound += abs(operator.amount_microdollars)
    _check_int64(
        invariant_total_profile_energy_wh=invariant_energy,
        monetary_numerator_bound=monetary_numerator_bound,
        maximum_changed_entries=len(occurrences) * len(scenario.profile_slots),
        maximum_completion_sum=len(occurrences) * len(scenario.profile_slots),
        maximum_stable_slot_order_score=stable_maximum,
        conservative_proxy_rank_score=sum(flexible_slot_maxima)
        * max(0, len(scenario.profile_slots) - 1),
    )


def _create_occurrence_variables(
    model: cp_model.CpModel,
    scenario: ScenarioInput,
    load: FlexibleLoad,
    occurrence: LoadOccurrence,
) -> tuple[tuple[cp_model.IntVar, ...], tuple[int, ...]]:
    slots = scenario.profile_slots
    start, end = _window_indices(scenario, occurrence)
    spec = load.execution_spec
    variables: list[cp_model.IntVar] = []
    maxima: list[int] = []
    if isinstance(spec, InterruptibleModulatingSpec):
        for index, slot in enumerate(slots):
            maximum = 0
            if start <= index < end:
                maximum = (
                    spec.maximum_power_w * slot.duration_seconds
                ) // WATT_SECONDS_PER_WATT_HOUR
            variable = model.new_int_var(
                0,
                maximum,
                f"energy_{occurrence.occurrence_id.hex}_{index}",
            )
            model.add(
                variable * WATT_SECONDS_PER_WATT_HOUR
                <= spec.maximum_power_w * slot.duration_seconds
            )
            active = model.new_bool_var(f"active_{occurrence.occurrence_id.hex}_{index}")
            model.add(variable >= 1).only_enforce_if(active)
            model.add(variable == 0).only_enforce_if(active.Not())
            if spec.minimum_power_when_active_w:
                model.add(
                    variable * WATT_SECONDS_PER_WATT_HOUR
                    >= spec.minimum_power_when_active_w * slot.duration_seconds
                ).only_enforce_if(active)
            variables.append(variable)
            maxima.append(maximum)
    else:
        allowed_starts = tuple(range(start, end - len(spec.fixed_slot_shape_wh) + 1))
        if not allowed_starts:
            _fail(
                "MODEL_CONTRACT_VIOLATION",
                "A validated fixed shape has no allowed start",
                occurrence_id=str(occurrence.occurrence_id),
            )
        starts = {
            candidate: model.new_bool_var(f"start_{occurrence.occurrence_id.hex}_{candidate}")
            for candidate in allowed_starts
        }
        model.add_exactly_one(starts.values())
        for index in range(len(slots)):
            contributions = tuple(
                (starts[candidate], spec.fixed_slot_shape_wh[index - candidate])
                for candidate in allowed_starts
                if candidate <= index < candidate + len(spec.fixed_slot_shape_wh)
            )
            maximum = max((value for _, value in contributions), default=0)
            variable = model.new_int_var(
                0,
                maximum,
                f"energy_{occurrence.occurrence_id.hex}_{index}",
            )
            model.add(
                variable == sum(start_variable * value for start_variable, value in contributions)
            )
            variables.append(variable)
            maxima.append(maximum)
    model.add(sum(variables) == occurrence.required_energy_wh)
    for variable, reference in zip(variables, _reference_energy(occurrence), strict=True):
        model.add_hint(variable, reference)
    return tuple(variables), tuple(maxima)


def _proof(
    operator: (
        IRBaselineAllowance
        | IRTieredEnergyCharge
        | IRTimeOfUseSchedule
        | IRTimeOfUseEnergyCharge
        | IRFixedDailyCharge
        | IRFixedMonthlyCharge
        | IRExplicitUnsupportedCharge
    ),
    subterm: str,
    reason: str,
    invariant_energy: int,
) -> OmittedChargeProof:
    return OmittedChargeProof.model_validate(
        {
            "rule_id": operator.rule_id,
            "operator": operator.operator,
            "subterm": subterm,
            "algebraic_reason": reason,
            "invariant_total_energy_wh": invariant_energy,
            "billing_period_confined": True,
        }
    )


def _active_operators_and_proofs(
    scenario: ScenarioInput,
    bundle: CompilationBundle,
    account_facts: AccountFacts,
    invariant_energy: int,
) -> tuple[tuple[IRTimeOfUseEnergyCharge, ...], tuple[OmittedChargeProof, ...], tuple[str, ...]]:
    service_end = account_facts.service_window.end
    if service_end is None:
        _fail("UNBOUNDED_BILLING_PERIOD", "Optimization requires a bounded billing period")
    bill_cycle_month = service_end.month
    time_charges: list[IRTimeOfUseEnergyCharge] = []
    proofs: list[OmittedChargeProof] = []
    supported: set[str] = set()
    operators = cast(tuple[object, ...], bundle.ir.operators)
    for operator in operators:
        operator_name = getattr(operator, "operator", type(operator).__name__)
        if not isinstance(operator_name, str):
            operator_name = type(operator).__name__
        supported.add(operator_name)
        known = isinstance(
            operator,
            (
                IRBaselineAllowance,
                IRTieredEnergyCharge,
                IRTimeOfUseSchedule,
                IRTimeOfUseEnergyCharge,
                IRFixedDailyCharge,
                IRFixedMonthlyCharge,
                IRExplicitUnsupportedCharge,
            ),
        )
        if not known:
            _fail(
                "UNSUPPORTED_IR_OPERATOR",
                "The compiled tariff contains an operator without an exact lowering",
                operator=operator_name,
            )
        operator = cast(
            IRBaselineAllowance
            | IRTieredEnergyCharge
            | IRTimeOfUseSchedule
            | IRTimeOfUseEnergyCharge
            | IRFixedDailyCharge
            | IRFixedMonthlyCharge
            | IRExplicitUnsupportedCharge,
            operator,
        )
        if not rule_applies(operator.applicability, account_facts, bill_cycle_month):
            proofs.append(
                _proof(
                    operator,
                    "complete_rule",
                    "ACCOUNT_APPLICABILITY_FALSE",
                    invariant_energy,
                )
            )
            continue
        if isinstance(operator, (IRBaselineAllowance, IRTimeOfUseSchedule)):
            proofs.append(
                _proof(
                    operator,
                    "complete_rule",
                    "NON_MONETARY_CLASSIFIER",
                    invariant_energy,
                )
            )
        elif isinstance(operator, IRTieredEnergyCharge):
            proofs.append(
                _proof(
                    operator,
                    "complete_rule",
                    "TOTAL_PROFILE_ENERGY_INVARIANT",
                    invariant_energy,
                )
            )
        elif isinstance(operator, IRTimeOfUseEnergyCharge):
            if operator.rounding != "HALF_UP_CENT_AT_LINE_ITEM":
                _fail(
                    "UNSUPPORTED_IR_ROUNDING",
                    "Variable time-of-use energy requires exact half-up line rounding",
                    rule_id=operator.rule_id,
                    rounding=operator.rounding,
                )
            time_charges.append(operator)
            if operator.baseline_credit_microdollars_per_kwh is not None:
                proofs.append(
                    _proof(
                        operator,
                        "baseline_credit",
                        "TOTAL_PROFILE_ENERGY_AND_BASELINE_INVARIANT",
                        invariant_energy,
                    )
                )
        elif isinstance(operator, IRFixedDailyCharge):
            proofs.append(
                _proof(
                    operator,
                    "complete_rule",
                    "BILLING_DAYS_INVARIANT",
                    invariant_energy,
                )
            )
        elif isinstance(operator, IRFixedMonthlyCharge):
            proofs.append(
                _proof(
                    operator,
                    "complete_rule",
                    "BILLING_PERIOD_INVARIANT",
                    invariant_energy,
                )
            )
        else:
            proofs.append(
                _proof(
                    operator,
                    "complete_rule",
                    "SUPPORTED_COST_ALWAYS_ZERO",
                    invariant_energy,
                )
            )
    return tuple(time_charges), tuple(proofs), tuple(sorted(supported))


def _slot_periods_and_rates(
    scenario: ScenarioInput,
    bundle: CompilationBundle,
    account_facts: AccountFacts,
    time_charges: tuple[IRTimeOfUseEnergyCharge, ...],
) -> tuple[dict[str, tuple[str, ...]], tuple[int, ...], tuple[int, ...]]:
    periods_by_rule: dict[str, tuple[str, ...]] = {}
    marginal_rates = [0] * len(scenario.profile_slots)
    for operator in time_charges:
        schedule = resolve_time_schedule(bundle, operator.schedule_rule_id)
        rate_by_period = {
            period_rate.period: period_rate.rate_microdollars_per_kwh
            for period_rate in operator.period_rates
        }
        periods: list[str] = []
        for index, slot in enumerate(scenario.profile_slots):
            try:
                period = classify_interval_period(
                    schedule,
                    slot.slot_start_utc,
                    slot.duration_seconds,
                    account_facts.service_window,
                )
            except ReplayError as error:
                _fail(error.code, str(error), slot_index=index, rule_id=operator.rule_id)
            if period not in rate_by_period:
                _fail(
                    "UNSUPPORTED_IR_PERIOD",
                    "A classified period has no exact time-of-use rate",
                    rule_id=operator.rule_id,
                    period=period,
                )
            periods.append(period)
            marginal_rates[index] += rate_by_period[period]
        periods_by_rule[operator.rule_id] = tuple(periods)
    distinct = {rate for rate in marginal_rates}
    rank_by_rate = {rate: rank for rank, rate in enumerate(sorted(distinct))}
    ranks = tuple(rank_by_rate[rate] for rate in marginal_rates)
    return periods_by_rule, tuple(marginal_rates), ranks


def _rounded_cents_variable(
    model: cp_model.CpModel,
    quantity: cp_model.IntVar,
    quantity_maximum: int,
    rate_microdollars_per_kwh: int,
    name: str,
) -> tuple[cp_model.IntVar, int, int]:
    magnitude = abs(rate_microdollars_per_kwh)
    numerator_maximum = quantity_maximum * magnitude + HALF_CENT_NUMERATOR
    rounded_maximum = numerator_maximum // WATT_HOUR_RATE_TO_CENTS_DENOMINATOR
    _check_int64(
        rounded_numerator_maximum=numerator_maximum,
        rounded_cents_maximum=rounded_maximum,
    )
    rounded = model.new_int_var(0, rounded_maximum, name)
    model.add_division_equality(
        rounded,
        quantity * magnitude + HALF_CENT_NUMERATOR,
        WATT_HOUR_RATE_TO_CENTS_DENOMINATOR,
    )
    sign = -1 if rate_microdollars_per_kwh < 0 else 1
    return rounded, sign, rounded_maximum


def compile_scenario_model(
    validated: ValidatedScenario,
    bundle: CompilationBundle,
    account_facts: AccountFacts,
    reference_billing: ReplayResult,
) -> LoweredScenarioModel:
    """Lower one prevalidated scenario and prove every objective bound."""

    scenario = validated.scenario
    if scenario.tariff_version_id != bundle.ir.tariff_version_id:
        _fail(
            "TARIFF_VERSION_MISMATCH",
            "Scenario and compiled tariff versions differ",
        )
    if bundle.reports.solver_lowering_unsupported_reasons:
        _fail(
            "TARIFF_OPTIMIZATION_UNAVAILABLE",
            "The tariff compiler did not admit optimizer lowering",
            reasons=bundle.reports.solver_lowering_unsupported_reasons,
        )
    if not bundle.reports.solver_lowering_supported_operators:
        _fail(
            "TARIFF_OPTIMIZATION_UNAVAILABLE",
            "The tariff compiler emitted no admitted optimizer operators",
        )
    occurrences = _canonical_occurrences(scenario)
    background = tuple(slot.energy_wh for slot in validated.decomposition.fixed_background)
    invariant_energy = sum(background) + sum(
        occurrence.required_energy_wh for _, occurrence in occurrences
    )
    time_charges, proofs, supported_operators = _active_operators_and_proofs(
        scenario,
        bundle,
        account_facts,
        invariant_energy,
    )
    periods_by_rule, marginal_rates, off_peak_ranks = _slot_periods_and_rates(
        scenario,
        bundle,
        account_facts,
        time_charges,
    )
    _preflight_signed_int64_safety(
        scenario,
        bundle,
        account_facts,
        background,
        occurrences,
    )
    model = cp_model.CpModel()
    energy_by_occurrence: dict[UUID, tuple[cp_model.IntVar, ...]] = {}
    maximum_by_occurrence: dict[UUID, tuple[int, ...]] = {}
    changed_variables: list[cp_model.IntVar] = []
    completion_variables: list[cp_model.IntVar] = []
    stable_terms: list[cp_model.LinearExpr] = []
    vector_position = 0
    for load, occurrence in occurrences:
        variables, maxima = _create_occurrence_variables(
            model,
            scenario,
            load,
            occurrence,
        )
        energy_by_occurrence[occurrence.occurrence_id] = variables
        maximum_by_occurrence[occurrence.occurrence_id] = maxima
        active_variables: list[cp_model.IntVar] = []
        reference = _reference_energy(occurrence)
        for index, variable in enumerate(variables):
            changed = model.new_bool_var(f"changed_{occurrence.occurrence_id.hex}_{index}")
            model.add(variable == reference[index]).only_enforce_if(changed.Not())
            model.add(variable != reference[index]).only_enforce_if(changed)
            changed_variables.append(changed)
            active = model.new_bool_var(f"completion_active_{occurrence.occurrence_id.hex}_{index}")
            model.add(variable >= 1).only_enforce_if(active)
            model.add(variable == 0).only_enforce_if(active.Not())
            active_variables.append(active)
            vector_position += 1
            stable_terms.append(variable * vector_position)
        completion = model.new_int_var(
            1,
            len(scenario.profile_slots),
            f"completion_{occurrence.occurrence_id.hex}",
        )
        model.add_max_equality(
            completion,
            tuple((index + 1) * active for index, active in enumerate(active_variables)),
        )
        completion_variables.append(completion)

    flexible_expressions: list[cp_model.LinearExpr] = []
    profile_expressions: list[cp_model.LinearExpr] = []
    profile_maxima: list[int] = []
    for index, slot in enumerate(scenario.profile_slots):
        slot_variables = [values[index] for values in energy_by_occurrence.values()]
        flexible = cp_model.LinearExpr.sum(slot_variables)
        profile = flexible + background[index]
        flexible_maximum = sum(values[index] for values in maximum_by_occurrence.values())
        profile_maximum = flexible_maximum + background[index]
        _check_int64(
            flexible_slot_maximum=flexible_maximum,
            profile_slot_maximum=profile_maximum,
            site_cap_product=profile_maximum * WATT_SECONDS_PER_WATT_HOUR,
        )
        constraints = scenario.electrical_constraints
        if constraints.site_import_cap_w is not None:
            model.add(
                profile * WATT_SECONDS_PER_WATT_HOUR
                <= constraints.site_import_cap_w * slot.duration_seconds
            )
        if constraints.flexible_load_aggregate_cap_w is not None:
            model.add(
                flexible * WATT_SECONDS_PER_WATT_HOUR
                <= constraints.flexible_load_aggregate_cap_w * slot.duration_seconds
            )
        flexible_expressions.append(flexible)
        profile_expressions.append(profile)
        profile_maxima.append(profile_maximum)

    variable_reference_keys = {
        (operator.rule_id, f"{operator.line_item_key}.{period_rate.period.lower()}")
        for operator in time_charges
        for period_rate in operator.period_rates
    }
    reference_variable_cents = sum(
        line.rounded_cents
        for line in reference_billing.line_items
        if (line.rule_id, line.line_item_key) in variable_reference_keys
    )
    constant_cost = reference_billing.supported_calculated_cents - reference_variable_cents
    cost_terms: list[cp_model.LinearExpr | int] = [constant_cost]
    minimum_cost = constant_cost
    maximum_cost = constant_cost
    for operator in time_charges:
        periods = periods_by_rule[operator.rule_id]
        for period_rate in operator.period_rates:
            indices = tuple(
                index for index, period in enumerate(periods) if period == period_rate.period
            )
            quantity_maximum = sum(profile_maxima[index] for index in indices)
            quantity = model.new_int_var(
                0,
                quantity_maximum,
                f"quantity_{operator.rule_id}_{period_rate.period}",
            )
            model.add(quantity == sum(profile_expressions[index] for index in indices))
            rounded, sign, rounded_maximum = _rounded_cents_variable(
                model,
                quantity,
                quantity_maximum,
                period_rate.rate_microdollars_per_kwh,
                f"cents_{operator.rule_id}_{period_rate.period}",
            )
            cost_terms.append(sign * rounded)
            if sign > 0:
                maximum_cost += rounded_maximum
            else:
                minimum_cost -= rounded_maximum
    cost_objective = cp_model.LinearExpr.sum(cost_terms)
    changed_objective = cp_model.LinearExpr.sum(changed_variables)
    completion_objective = cp_model.LinearExpr.sum(completion_variables)
    stable_objective = cp_model.LinearExpr.sum(stable_terms)
    proxy_terms = [
        off_peak_ranks[index] * variable
        for variables in energy_by_occurrence.values()
        for index, variable in enumerate(variables)
    ]
    proxy_objective = cp_model.LinearExpr.sum(proxy_terms)
    maximum_changed = len(occurrences) * len(scenario.profile_slots)
    maximum_completion = len(occurrences) * len(scenario.profile_slots)
    maximum_stable = sum(
        maximum_by_occurrence[occurrence.occurrence_id][index] * position
        for position, (_, occurrence, index) in enumerate(
            (
                (load, occurrence, index)
                for load, occurrence in occurrences
                for index in range(len(scenario.profile_slots))
            ),
            start=1,
        )
    )
    maximum_proxy = sum(
        maximum_by_occurrence[occurrence.occurrence_id][index] * off_peak_ranks[index]
        for _, occurrence in occurrences
        for index in range(len(scenario.profile_slots))
    )
    _check_int64(
        invariant_energy=invariant_energy,
        minimum_supported_cost=minimum_cost,
        maximum_supported_cost=maximum_cost,
        maximum_changed=maximum_changed,
        maximum_completion=maximum_completion,
        maximum_stable=maximum_stable,
        maximum_proxy=maximum_proxy,
    )
    rank_payload = {
        "contract_version": "off-peak-rank-v1",
        "tariff_version_id": scenario.tariff_version_id,
        "slots": [
            {
                "slot_start_utc": slot.slot_start_utc.isoformat(),
                "duration_seconds": slot.duration_seconds,
                "marginal_rate_microdollars_per_kwh": marginal_rates[index],
                "off_peak_rank": off_peak_ranks[index],
            }
            for index, slot in enumerate(scenario.profile_slots)
        ],
    }
    rank_hash = canonical_content_sha256(b"RateReplay.OffPeakRankCalendar.v1", rank_payload)
    objective_bounds = ObjectiveBounds(
        minimum_supported_cost_cents=minimum_cost,
        maximum_supported_cost_cents=maximum_cost,
        maximum_changed_occurrence_slot_count=maximum_changed,
        maximum_completion_slot_index_sum=maximum_completion,
        maximum_stable_slot_order_score=maximum_stable,
        maximum_proxy_rank_score=maximum_proxy,
    )
    lowering_payload = {
        "tariff_version_id": scenario.tariff_version_id,
        "supported_operators": supported_operators,
        "constant_supported_cost_cents": constant_cost,
        "invariant_total_profile_energy_wh": invariant_energy,
        "omitted_charge_proofs": [proof.model_dump(mode="json") for proof in proofs],
        "off_peak_ranks": off_peak_ranks,
        "rank_calendar_sha256": rank_hash,
        "objective_bounds": objective_bounds.model_dump(mode="json"),
    }
    record = LoweringRecord(
        tariff_version_id=scenario.tariff_version_id,
        supported_operators=supported_operators,
        constant_supported_cost_cents=constant_cost,
        invariant_total_profile_energy_wh=invariant_energy,
        omitted_charge_proofs=proofs,
        off_peak_ranks=off_peak_ranks,
        rank_calendar_sha256=rank_hash,
        objective_bounds=objective_bounds,
        lowering_sha256=canonical_content_sha256(
            b"RateReplay.CpSatChargeLowering.v1", lowering_payload
        ),
    )
    return LoweredScenarioModel(
        model=model,
        energy_by_occurrence=energy_by_occurrence,
        objectives=LoweredObjectives(
            supported_cost=cost_objective,
            changed_occurrence_slots=changed_objective,
            completion_slot_index_sum=completion_objective,
            stable_slot_order_score=stable_objective,
            proxy_rank_score=proxy_objective,
        ),
        record=record,
    )
