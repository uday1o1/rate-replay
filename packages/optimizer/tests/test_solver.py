import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import ratereplay_optimizer.solver as solver_module
from ortools.sat.python import cp_model
from ratereplay_optimizer.lowering import (
    LoweredScenarioModel,
    OptimizationLoweringError,
    compile_scenario_model,
)
from ratereplay_optimizer.models import (
    CandidateSchedule,
    CanonicalProfileSlot,
    ContiguousFixedShapeSpec,
    FlexibleLoad,
    InterruptibleModulatingSpec,
    LoadOccurrence,
    ReferenceSlot,
    ScenarioInput,
    SolverConfiguration,
    ValidatedScenario,
)
from ratereplay_optimizer.results import ScenarioResultError, build_scenario_result
from ratereplay_optimizer.scenario import validate_and_decompose_scenario
from ratereplay_optimizer.solver import (
    OptimizationExecutionError,
    default_solver_configuration,
    optimize_exact,
    optimize_off_peak_heuristic,
)
from ratereplay_optimizer.verification import (
    candidate_from_reference,
    verify_candidate_schedule,
)
from ratereplay_tariffs.billing import (
    IntervalReplayRequest,
    ReplayInterval,
    ReplayResult,
    replay_compiled_tariff,
)
from ratereplay_tariffs.compiled import CompilationBundle
from ratereplay_tariffs.compiler import compile_tariff
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts

ROOT = Path(__file__).resolve().parents[3]
START = datetime(2026, 7, 6, 22, tzinfo=UTC)
LOAD_ID = UUID("00000000-0000-0000-0000-000000000001")
OCCURRENCE_ID = UUID("10000000-0000-0000-0000-000000000001")
DEFINITIONS = tuple(sorted((ROOT / "tariffs/definitions").glob("*.json")))


def _facts() -> tuple[AccountFacts, DatedEligibilityFacts]:
    payload = json.loads(
        (ROOT / "tariffs/examples/m3-comparison-account.json").read_text(encoding="utf-8")
    )
    return (
        AccountFacts.model_validate_json(json.dumps(payload["account_facts"])),
        DatedEligibilityFacts.model_validate_json(json.dumps(payload["dated_eligibility_facts"])),
    )


def _bundle(definition: Path) -> CompilationBundle:
    return compile_tariff(ROOT, definition)


def _scenario(
    bundle: CompilationBundle,
    *,
    reference: tuple[int, ...] = (0, 0, 70),
    background: tuple[int, ...] = (100, 100, 100),
) -> ScenarioInput:
    slots = tuple(
        CanonicalProfileSlot(
            slot_start_utc=START + timedelta(hours=index),
            duration_seconds=3_600,
            measured_energy_wh=background[index] + reference[index],
        )
        for index in range(len(reference))
    )
    reference_slots = tuple(
        ReferenceSlot(
            slot_start_utc=slot.slot_start_utc,
            duration_seconds=slot.duration_seconds,
            energy_wh=reference[index],
        )
        for index, slot in enumerate(slots)
    )
    occurrence = LoadOccurrence(
        occurrence_id=OCCURRENCE_ID,
        required_energy_wh=sum(reference),
        earliest_start_utc=slots[0].slot_start_utc,
        deadline_utc=slots[-1].slot_start_utc + timedelta(hours=1),
        reference_schedule=reference_slots,
    )
    load = FlexibleLoad(
        load_id=LOAD_ID,
        physical_asset_key="ev-1",
        kind="EV",
        mode="SHIFT_EXISTING",
        execution_spec=InterruptibleModulatingSpec(
            execution_type="INTERRUPTIBLE_MODULATING",
            maximum_power_w=70,
            minimum_power_when_active_w=0,
        ),
        occurrences=(occurrence,),
    )
    return ScenarioInput(
        scenario_version="historical-flex-scenario-v1",
        profile_content_sha256="a" * 64,
        tariff_version_id=bundle.ir.tariff_version_id,
        profile_slots=slots,
        loads=(load,),
    )


def _compositions(total: int, slots: int) -> Iterator[tuple[int, ...]]:
    if slots == 1:
        yield (total,)
        return
    for first in range(total + 1):
        for suffix in _compositions(total - first, slots - 1):
            yield (first, *suffix)


def _public_cost(
    scenario: ScenarioInput,
    amounts: tuple[int, ...],
    bundle: CompilationBundle,
    account: AccountFacts,
    dated: DatedEligibilityFacts,
) -> int:
    reference = tuple(
        slot.energy_wh for slot in scenario.loads[0].occurrences[0].reference_schedule
    )
    background = tuple(
        slot.measured_energy_wh - reference[index]
        for index, slot in enumerate(scenario.profile_slots)
    )
    profile = tuple(background[index] + amounts[index] for index in range(len(amounts)))
    intervals = tuple(
        ReplayInterval(
            start_utc_ns=int(slot.slot_start_utc.timestamp()) * 1_000_000_000,
            duration_seconds=slot.duration_seconds,
            energy_wh=profile[index],
        )
        for index, slot in enumerate(scenario.profile_slots)
    )
    result = replay_compiled_tariff(
        bundle,
        IntervalReplayRequest(
            request_version="interval-replay-request-v1",
            profile_content_sha256=scenario.profile_content_sha256,
            account_facts=account,
            energy_wh=sum(profile),
            intervals=intervals,
            dated_eligibility_facts=dated,
        ),
    )
    return result.supported_calculated_cents


def _independent_objective(
    scenario: ScenarioInput,
    amounts: tuple[int, ...],
    bundle: CompilationBundle,
    account: AccountFacts,
    dated: DatedEligibilityFacts,
) -> tuple[int, int, int, int]:
    reference = tuple(
        slot.energy_wh for slot in scenario.loads[0].occurrences[0].reference_schedule
    )
    positive = tuple(index + 1 for index, amount in enumerate(amounts) if amount > 0)
    return (
        _public_cost(scenario, amounts, bundle, account, dated),
        sum(left != right for left, right in zip(amounts, reference, strict=True)),
        positive[-1],
        sum(index * amount for index, amount in enumerate(amounts, start=1)),
    )


def test_exact_staged_solver_matches_complete_independent_optimum_set() -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-etoud-2026-07.json")
    scenario = _scenario(bundle)
    validated = validate_and_decompose_scenario(scenario)
    account, dated = _facts()
    scored = []
    for amounts in _compositions(70, 3):
        scored.append(
            (
                _independent_objective(scenario, amounts, bundle, account, dated),
                amounts,
            )
        )
    optimum = min(score for score, _ in scored)
    optimum_set = {amounts for score, amounts in scored if score == optimum}

    result = optimize_exact(
        validated,
        bundle,
        account,
        dated_facts=dated,
        configuration=default_solver_configuration(max_deterministic_time_per_stage=2.0),
    )

    selected_amounts = tuple(
        slot.energy_wh for slot in result.selected.selected.schedule.occurrences[0].slots
    )
    assert result.search_status == "OPTIMAL"
    assert result.highest_objective_stage_proved_optimal == 4
    assert result.first_open_stage is None
    assert tuple(record.status for record in result.stage_records) == ("OPTIMAL",) * 4
    assert result.selected.selected.record.objective.ordered_values() == optimum
    assert selected_amounts in optimum_set
    assert optimum_set == {(70, 0, 0)}
    assert result.selected.selected_source == "SOLVER_INCUMBENT"


@pytest.mark.parametrize("definition", DEFINITIONS, ids=lambda path: path.stem)
def test_every_public_tariff_lowering_matches_fresh_reference_billing(
    definition: Path,
) -> None:
    bundle = _bundle(definition)
    scenario = _scenario(bundle)
    validated = validate_and_decompose_scenario(scenario)
    account, dated = _facts()
    reference = verify_candidate_schedule(
        scenario,
        candidate_from_reference(scenario),
        bundle,
        account,
        dated_facts=dated,
    )
    random_state = 20_260_813
    samples = {(0, 70, 0), (70, 0, 0), (20, 30, 20), (15, 40, 15)}
    while len(samples) < 10:
        random_state = (1_103_515_245 * random_state + 12_345) % (2**31)
        first = random_state % 71
        random_state = (1_103_515_245 * random_state + 12_345) % (2**31)
        second = random_state % (71 - first)
        samples.add((first, second, 70 - first - second))

    for sample_index, amounts in enumerate(sorted(samples)):
        lowered = compile_scenario_model(
            validated,
            bundle,
            account,
            reference.billing_result,
        )
        for variable, amount in zip(
            lowered.energy_by_occurrence[OCCURRENCE_ID],
            amounts,
            strict=True,
        ):
            lowered.model.add(variable == amount)
        lowered.model.minimize(lowered.objectives.supported_cost)
        solver = cp_model.CpSolver()
        solver.parameters.num_workers = 1
        status = solver.solve(lowered.model)
        assert status == cp_model.OPTIMAL
        assert solver.value(lowered.objectives.supported_cost) == (
            _public_cost(scenario, amounts, bundle, account, dated)
        ), (definition.name, sample_index, amounts)


def test_e1_invariance_proof_keeps_reference_when_full_tuple_is_equal_or_better() -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-e1-2026-07.json")
    scenario = _scenario(bundle)
    validated = validate_and_decompose_scenario(scenario)
    account, dated = _facts()

    result = optimize_exact(validated, bundle, account, dated_facts=dated)

    assert result.search_status == "OPTIMAL"
    assert result.selected.selected_source == "REFERENCE"
    assert result.selected.reason == "REFERENCE_EQUAL_OR_BETTER"
    assert result.selected.incumbent is not None
    assert result.selected.incumbent.record.objective == result.selected.reference.record.objective
    tier_proof = next(
        proof
        for proof in result.lowering_record.omitted_charge_proofs
        if proof.operator == "TIER_ALLOCATE_AND_MULTIPLY_RATIONAL"
    )
    assert tier_proof.algebraic_reason == "TOTAL_PROFILE_ENERGY_INVARIANT"
    assert tier_proof.invariant_total_energy_wh == 370


def test_contiguous_fixed_shape_is_lowered_as_one_exact_allowed_start() -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-etoud-2026-07.json")
    scenario = _scenario(bundle, reference=(0, 30, 40))
    fixed_load = scenario.loads[0].model_copy(
        update={
            "kind": "DISHWASHER",
            "execution_spec": ContiguousFixedShapeSpec(
                execution_type="CONTIGUOUS_FIXED_SHAPE",
                fixed_slot_shape_wh=(30, 40),
            ),
        }
    )
    scenario = scenario.model_copy(update={"loads": (fixed_load,)})
    account, dated = _facts()

    result = optimize_exact(
        validate_and_decompose_scenario(scenario),
        bundle,
        account,
        dated_facts=dated,
    )

    selected = tuple(
        slot.energy_wh for slot in result.selected.selected.schedule.occurrences[0].slots
    )
    assert result.search_status == "OPTIMAL"
    assert selected == (30, 40, 0)
    assert result.selected.selected.record.status == "VALID"


def test_off_peak_heuristic_is_deterministic_versioned_and_strictly_selected() -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-etoud-2026-07.json")
    scenario = _scenario(bundle)
    validated = validate_and_decompose_scenario(scenario)
    account, dated = _facts()

    first = optimize_off_peak_heuristic(validated, bundle, account, dated_facts=dated)
    second = optimize_off_peak_heuristic(validated, bundle, account, dated_facts=dated)

    assert first.search_status == "HEURISTIC_PROXY_OPTIMAL"
    assert first.selection_outcome == "HEURISTIC_INCUMBENT_SELECTED"
    assert first.incumbent_proxy_pair is not None
    assert first.incumbent_proxy_pair < first.reference_proxy_pair
    assert first.lowering_record.off_peak_ranks == (0, 0, 1)
    assert first.lowering_record.rank_calendar_sha256 == (
        second.lowering_record.rank_calendar_sha256
    )
    assert first.result_sha256 == second.result_sha256
    assert first.solver_configuration.num_search_workers == 1


def test_exact_solver_is_repeatable_under_locked_deterministic_configuration() -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-etoud-2026-07.json")
    validated = validate_and_decompose_scenario(_scenario(bundle))
    account, dated = _facts()
    configuration = default_solver_configuration(max_deterministic_time_per_stage=2.0)

    first = optimize_exact(
        validated,
        bundle,
        account,
        dated_facts=dated,
        configuration=configuration,
    )
    second = optimize_exact(
        validated,
        bundle,
        account,
        dated_facts=dated,
        configuration=configuration,
    )

    assert first.result_sha256 == second.result_sha256
    assert first.selected.selected.schedule == second.selected.selected.schedule
    assert first.stage_records == second.stage_records


def test_scenario_result_is_deterministic_complete_and_never_a_forecast() -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-etoud-2026-07.json")
    validated = validate_and_decompose_scenario(_scenario(bundle))
    account, dated = _facts()
    exact = optimize_exact(validated, bundle, account, dated_facts=dated)
    heuristic = optimize_off_peak_heuristic(validated, bundle, account, dated_facts=dated)

    first = build_scenario_result(validated, bundle, account, dated, exact, heuristic)
    second = build_scenario_result(validated, bundle, account, dated, exact, heuristic)

    assert first == second
    assert first.result_sha256 == second.result_sha256
    assert first.calculation_time_mode == "HISTORICAL_REPLAY"
    assert first.historical_addition_label == "HISTORICAL_COUNTERFACTUAL_NOT_FORECAST"
    assert first.exact.selected.verification.status == "VALID"
    assert (
        first.exact.selected.billing_result.result_sha256
        == first.exact.selected.verification.billing_result_sha256
    )
    assert first.heuristic.bill_optimality_claim is False
    assert first.manifest.solver_lowering_sha256 == exact.lowering_record.lowering_sha256
    assert first.manifest.rank_calendar_sha256 == heuristic.lowering_record.rank_calendar_sha256
    assert len(first.manifest.load_modes_and_reference_hashes) == 1


@pytest.mark.parametrize(
    "internal_status",
    ["UNKNOWN", "MODEL_INVALID", "MODEL_CONTRACT_VIOLATION", "UNVERIFIED_INCUMBENT"],
)
def test_unsuccessful_exact_status_cannot_become_a_scenario_resource(
    internal_status: str,
) -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-etoud-2026-07.json")
    validated = validate_and_decompose_scenario(_scenario(bundle))
    account, dated = _facts()
    exact = optimize_exact(validated, bundle, account, dated_facts=dated)
    heuristic = optimize_off_peak_heuristic(validated, bundle, account, dated_facts=dated)

    with pytest.raises(ScenarioResultError) as captured:
        build_scenario_result(
            validated,
            bundle,
            account,
            dated,
            exact.__class__(
                search_status=internal_status,  # type: ignore[arg-type]
                selected=exact.selected,
                stage_records=exact.stage_records,
                highest_objective_stage_proved_optimal=exact.highest_objective_stage_proved_optimal,
                first_open_stage=exact.first_open_stage,
                best_supported_cost_bound=exact.best_supported_cost_bound,
                absolute_cost_gap_cents=exact.absolute_cost_gap_cents,
                relative_cost_gap=exact.relative_cost_gap,
                solver_configuration=exact.solver_configuration,
                lowering_record=exact.lowering_record,
                result_sha256=exact.result_sha256,
            ),
            heuristic,
        )
    assert captured.value.code == f"EXACT_SOLVER_{internal_status}"


def test_unknown_ir_operator_refuses_optimization_with_exact_reason() -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-e1-2026-07.json")
    scenario = _scenario(bundle)
    validated = validate_and_decompose_scenario(scenario)
    account, dated = _facts()
    reference = verify_candidate_schedule(
        scenario,
        candidate_from_reference(scenario),
        bundle,
        account,
        dated_facts=dated,
    )
    corrupted_ir = bundle.ir.model_copy(update={"operators": (*bundle.ir.operators, object())})
    corrupted_bundle = bundle.model_copy(update={"ir": corrupted_ir})

    with pytest.raises(OptimizationLoweringError) as captured:
        compile_scenario_model(
            validated,
            corrupted_bundle,
            account,
            reference.billing_result,
        )
    assert captured.value.code == "UNSUPPORTED_IR_OPERATOR"
    assert captured.value.witness["operator"] == "object"


def test_zero_budget_unknown_and_heuristic_reference_fallback_are_distinct() -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-etoud-2026-07.json")
    validated = validate_and_decompose_scenario(_scenario(bundle))
    account, dated = _facts()
    configuration = default_solver_configuration(max_deterministic_time_per_stage=1e-12)

    exact = optimize_exact(
        validated,
        bundle,
        account,
        dated_facts=dated,
        configuration=configuration,
    )
    heuristic = optimize_off_peak_heuristic(
        validated,
        bundle,
        account,
        dated_facts=dated,
        configuration=configuration,
    )

    assert exact.search_status == "UNKNOWN"
    assert exact.stage_records[0].status == "UNKNOWN"
    assert exact.selected.selected_source == "REFERENCE"
    assert heuristic.search_status == "HEURISTIC_NO_INCUMBENT"
    assert heuristic.selection_outcome == "HEURISTIC_REFERENCE_FALLBACK"
    assert heuristic.selected is heuristic.reference


def test_first_solution_limit_is_preserved_as_verified_best_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-etoud-2026-07.json")
    validated = validate_and_decompose_scenario(_scenario(bundle))
    account, dated = _facts()
    original = solver_module._configured_solver

    def first_solution_solver(configuration: SolverConfiguration) -> cp_model.CpSolver:
        solver = original(configuration)
        solver.parameters.cp_model_presolve = False
        solver.parameters.stop_after_first_solution = True
        return solver

    monkeypatch.setattr(solver_module, "_configured_solver", first_solution_solver)
    result = optimize_exact(validated, bundle, account, dated_facts=dated)

    assert result.search_status == "BEST_FOUND"
    assert result.stage_records[0].status == "FEASIBLE"
    assert result.highest_objective_stage_proved_optimal == 0
    assert result.first_open_stage == 1
    assert result.selected.incumbent is not None
    assert result.selected.incumbent.record.status == "VALID"


@pytest.mark.parametrize(
    ("seeded_status", "expected_status"),
    [
        ("INFEASIBLE", "MODEL_CONTRACT_VIOLATION"),
        ("MODEL_INVALID", "MODEL_INVALID"),
    ],
)
def test_seeded_solver_contract_failures_never_become_success(
    monkeypatch: pytest.MonkeyPatch,
    seeded_status: str,
    expected_status: str,
) -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-etoud-2026-07.json")
    validated = validate_and_decompose_scenario(_scenario(bundle))
    account, dated = _facts()
    original = solver_module.compile_scenario_model

    def corrupted_model(
        value: ValidatedScenario,
        compiled: CompilationBundle,
        facts: AccountFacts,
        reference_billing: ReplayResult,
    ) -> LoweredScenarioModel:
        lowered = original(value, compiled, facts, reference_billing)
        if seeded_status == "INFEASIBLE":
            lowered.model.add(False)
        else:
            target = lowered.model.new_int_var(0, 1, "seeded_invalid_division")
            lowered.model.add_division_equality(target, 1, 0)
        return lowered

    monkeypatch.setattr(solver_module, "compile_scenario_model", corrupted_model)
    result = optimize_exact(validated, bundle, account, dated_facts=dated)

    assert result.search_status == expected_status
    assert result.selected.selected_source == "REFERENCE"
    assert result.selected.incumbent is None


def test_seeded_solver_schedule_corruption_fails_independent_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-etoud-2026-07.json")
    validated = validate_and_decompose_scenario(_scenario(bundle))
    account, dated = _facts()
    original = solver_module._extract_candidate

    def corrupt_candidate(
        lowered: LoweredScenarioModel,
        value: ValidatedScenario,
        solver: cp_model.CpSolver,
    ) -> CandidateSchedule:
        candidate = original(lowered, value, solver)
        occurrence = candidate.occurrences[0]
        slots = list(occurrence.slots)
        slots[0] = slots[0].model_copy(update={"energy_wh": slots[0].energy_wh + 1})
        return candidate.model_copy(
            update={"occurrences": (occurrence.model_copy(update={"slots": tuple(slots)}),)}
        )

    monkeypatch.setattr(solver_module, "_extract_candidate", corrupt_candidate)
    with pytest.raises(OptimizationExecutionError) as captured:
        optimize_exact(validated, bundle, account, dated_facts=dated)
    assert captured.value.code == "UNVERIFIED_SOLVER_INCUMBENT"
    assert captured.value.witness["verifier_code"] == "VERIFIER_ENERGY_CONSERVATION_FAILED"


def test_compiler_declared_unsupported_tariff_refuses_with_frozen_reason() -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-e1-2026-07.json")
    scenario = _scenario(bundle)
    validated = validate_and_decompose_scenario(scenario)
    account, dated = _facts()
    reference = verify_candidate_schedule(
        scenario,
        candidate_from_reference(scenario),
        bundle,
        account,
        dated_facts=dated,
    )
    reports = bundle.reports.model_copy(
        update={
            "solver_lowering_supported_operators": (),
            "solver_lowering_unsupported_reasons": ("SEEDED_UNSUPPORTED_OPERATOR",),
        }
    )
    unsupported = bundle.model_copy(update={"reports": reports})

    with pytest.raises(OptimizationLoweringError) as captured:
        compile_scenario_model(
            validated,
            unsupported,
            account,
            reference.billing_result,
        )
    assert captured.value.code == "TARIFF_OPTIMIZATION_UNAVAILABLE"
    assert captured.value.witness["reasons"] == ("SEEDED_UNSUPPORTED_OPERATOR",)


def test_signed_int64_overflow_is_rejected_before_solver_construction() -> None:
    bundle = _bundle(ROOT / "tariffs/definitions/pge-e1-2026-07.json")
    scenario = _scenario(bundle)
    oversized = scenario.loads[0].model_copy(
        update={
            "execution_spec": InterruptibleModulatingSpec(
                execution_type="INTERRUPTIBLE_MODULATING",
                maximum_power_w=2**63,
                minimum_power_when_active_w=0,
            )
        }
    )
    scenario = scenario.model_copy(update={"loads": (oversized,)})
    validated = validate_and_decompose_scenario(scenario)
    account, dated = _facts()
    reference = verify_candidate_schedule(
        scenario,
        candidate_from_reference(scenario),
        bundle,
        account,
        dated_facts=dated,
    )

    with pytest.raises(OptimizationLoweringError) as captured:
        compile_scenario_model(
            validated,
            bundle,
            account,
            reference.billing_result,
        )
    assert captured.value.code == "INT64_OBJECTIVE_BOUND"
    assert captured.value.witness["bound_name"] == "maximum_power_product"
