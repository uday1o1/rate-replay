from __future__ import annotations

from dataclasses import fields, replace
from typing import Any, cast

from ratereplay_domain.semantic_identity import SemanticCalculationIdentity


def _identity() -> SemanticCalculationIdentity:
    return SemanticCalculationIdentity(
        job_kind="SCENARIO",
        request_schema_version="scenario-operation-v1",
        calculation_contract_version="scenario-contract-v1",
        environment_lock_hash="0" * 64,
        tariff_compiler_version="tariff-compiler-v1",
        billing_evaluator_version="billing-evaluator-v1",
        profile_version_hash="1" * 64,
        tariff_ast_hashes=("2" * 64, "3" * 64),
        component_vector_hashes=("4" * 64, "5" * 64),
        account_facts_hash="6" * 64,
        billing_period_identity_hash="7" * 64,
        reconciliation_inputs_hash="8" * 64,
        reconciliation_policy_hash="9" * 64,
        comparison_coverage_version="comparison-coverage-v1",
        scenario_and_reference_hashes=("a" * 64, "b" * 64),
        heuristic_contract_version="heuristic-v1",
        heuristic_rank_calendar_hash="c" * 64,
        heuristic_solver_configuration_hash="d" * 64,
        solver_lowering_version="solver-lowering-v1",
        solver_name_and_version="cp-sat-9.15",
        solver_configuration_hash="e" * 64,
        verifier_version="verifier-v1",
        report_template_version="redacted-report-v1",
    )


def test_every_semantic_identity_field_changes_the_hash() -> None:
    identity = _identity()
    baseline = identity.sha256()
    replacements: dict[str, object] = {
        "job_kind": "REPORT",
        "request_schema_version": "report-operation-v1",
        "calculation_contract_version": "scenario-contract-v2",
        "environment_lock_hash": "f" * 64,
        "tariff_compiler_version": "tariff-compiler-v2",
        "billing_evaluator_version": "billing-evaluator-v2",
        "profile_version_hash": None,
        "tariff_ast_hashes": ("2" * 64,),
        "component_vector_hashes": ("4" * 64,),
        "account_facts_hash": None,
        "billing_period_identity_hash": None,
        "reconciliation_inputs_hash": None,
        "reconciliation_policy_hash": None,
        "comparison_coverage_version": None,
        "scenario_and_reference_hashes": ("a" * 64,),
        "heuristic_contract_version": None,
        "heuristic_rank_calendar_hash": None,
        "heuristic_solver_configuration_hash": None,
        "solver_lowering_version": None,
        "solver_name_and_version": None,
        "solver_configuration_hash": None,
        "verifier_version": None,
        "report_template_version": None,
    }
    assert set(replacements) == {field.name for field in fields(identity)}
    replace_identity = cast(Any, replace)
    assert all(
        replace_identity(identity, **{name: value}).sha256() != baseline
        for name, value in replacements.items()
    )


def test_set_like_hash_vectors_are_order_independent() -> None:
    identity = _identity()
    reordered = replace(
        identity,
        tariff_ast_hashes=tuple(reversed(identity.tariff_ast_hashes)),
        component_vector_hashes=tuple(reversed(identity.component_vector_hashes)),
        scenario_and_reference_hashes=tuple(reversed(identity.scenario_and_reference_hashes)),
    )
    assert reordered.sha256() == identity.sha256()


def test_current_replay_reconciliation_inputs_and_policy_are_semantic() -> None:
    identity = replace(
        _identity(),
        job_kind="REPLAY",
        request_schema_version="replay-operation-v1",
        scenario_and_reference_hashes=(),
    )
    changed_input = replace(identity, reconciliation_inputs_hash="f" * 64)
    changed_policy = replace(identity, reconciliation_policy_hash="f" * 64)
    unreconciled = replace(
        identity,
        reconciliation_inputs_hash=None,
        reconciliation_policy_hash=None,
    )
    assert (
        len(
            {
                identity.sha256(),
                changed_input.sha256(),
                changed_policy.sha256(),
                unreconciled.sha256(),
            }
        )
        == 4
    )
