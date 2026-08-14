"""Canonical, version-sensitive semantic calculation identities."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class SemanticCalculationIdentity:
    job_kind: str
    request_schema_version: str
    calculation_contract_version: str
    environment_lock_hash: str
    tariff_compiler_version: str | None = None
    billing_evaluator_version: str | None = None
    profile_version_hash: str | None = None
    tariff_ast_hashes: tuple[str, ...] = ()
    component_vector_hashes: tuple[str, ...] = ()
    account_facts_hash: str | None = None
    billing_period_identity_hash: str | None = None
    reconciliation_inputs_hash: str | None = None
    reconciliation_policy_hash: str | None = None
    comparison_coverage_version: str | None = None
    scenario_and_reference_hashes: tuple[str, ...] = ()
    heuristic_contract_version: str | None = None
    heuristic_rank_calendar_hash: str | None = None
    heuristic_solver_configuration_hash: str | None = None
    solver_lowering_version: str | None = None
    solver_name_and_version: str | None = None
    solver_configuration_hash: str | None = None
    verifier_version: str | None = None
    report_template_version: str | None = None

    def __post_init__(self) -> None:
        if self.job_kind not in {"REPLAY", "COMPARISON", "SCENARIO", "REPORT"}:
            raise ValueError("Semantic calculation job kind is unsupported")
        for label, value in (
            ("request_schema_version", self.request_schema_version),
            ("calculation_contract_version", self.calculation_contract_version),
        ):
            if not value or len(value) > 64:
                raise ValueError(f"{label} is invalid")
        hash_fields = (
            ("environment_lock_hash", self.environment_lock_hash),
            ("profile_version_hash", self.profile_version_hash),
            ("account_facts_hash", self.account_facts_hash),
            ("billing_period_identity_hash", self.billing_period_identity_hash),
            ("reconciliation_inputs_hash", self.reconciliation_inputs_hash),
            ("reconciliation_policy_hash", self.reconciliation_policy_hash),
            ("heuristic_rank_calendar_hash", self.heuristic_rank_calendar_hash),
            (
                "heuristic_solver_configuration_hash",
                self.heuristic_solver_configuration_hash,
            ),
            ("solver_configuration_hash", self.solver_configuration_hash),
        )
        for hash_label, hash_value in hash_fields:
            if hash_value is not None and _SHA256.fullmatch(hash_value) is None:
                raise ValueError(f"{hash_label} must be a lowercase SHA-256 digest")
        for label, values in (
            ("tariff_ast_hashes", self.tariff_ast_hashes),
            ("component_vector_hashes", self.component_vector_hashes),
            ("scenario_and_reference_hashes", self.scenario_and_reference_hashes),
        ):
            if len(set(values)) != len(values) or any(
                _SHA256.fullmatch(value) is None for value in values
            ):
                raise ValueError(f"{label} must contain unique lowercase SHA-256 digests")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "job_kind": self.job_kind,
            "request_schema_version": self.request_schema_version,
            "calculation_contract_version": self.calculation_contract_version,
            "environment_lock_hash": self.environment_lock_hash,
            "tariff_compiler_version": self.tariff_compiler_version,
            "billing_evaluator_version": self.billing_evaluator_version,
            "profile_version_hash": self.profile_version_hash,
            "tariff_ast_hashes": sorted(self.tariff_ast_hashes),
            "component_vector_hashes": sorted(self.component_vector_hashes),
            "account_facts_hash": self.account_facts_hash,
            "billing_period_identity_hash": self.billing_period_identity_hash,
            "reconciliation_inputs_hash": self.reconciliation_inputs_hash,
            "reconciliation_policy_hash": self.reconciliation_policy_hash,
            "comparison_coverage_version": self.comparison_coverage_version,
            "scenario_and_reference_hashes": sorted(self.scenario_and_reference_hashes),
            "heuristic_contract_version": self.heuristic_contract_version,
            "heuristic_rank_calendar_hash": self.heuristic_rank_calendar_hash,
            "heuristic_solver_configuration_hash": (self.heuristic_solver_configuration_hash),
            "solver_lowering_version": self.solver_lowering_version,
            "solver_name_and_version": self.solver_name_and_version,
            "solver_configuration_hash": self.solver_configuration_hash,
            "verifier_version": self.verifier_version,
            "report_template_version": self.report_template_version,
        }

    def sha256(self) -> str:
        payload = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        return hashlib.sha256(
            b"RateReplay.SemanticCalculationIdentity.v1\x00" + payload
        ).hexdigest()
