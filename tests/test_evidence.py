from scripts.validate_evidence import (
    _validate_csv,
    _validate_external_sources,
    _validate_generated_evidence,
    _validate_m1_evidence,
    _validate_m2_evidence,
    _validate_m3_evidence,
    _validate_m4_correctness_evidence,
    _validate_m4_performance_charter,
    _validate_tariffs,
)
from scripts.validate_m3_goldens import validate as validate_m3_goldens


def test_external_source_hashes_are_locked() -> None:
    _validate_external_sources()


def test_provider_csv_contract_is_locked() -> None:
    _validate_csv()


def test_tariff_sources_and_admission_matrix_are_consistent() -> None:
    _validate_tariffs()


def test_generated_evidence_is_content_addressed() -> None:
    _validate_generated_evidence()


def test_milestone_one_recovery_evidence_matches_frozen_charter() -> None:
    _validate_m1_evidence()


def test_milestone_four_performance_charter_is_frozen_before_tuning() -> None:
    _validate_m4_performance_charter()


def test_milestone_four_optimizer_evidence_remains_historically_locked() -> None:
    _validate_m4_correctness_evidence()


def test_milestone_two_tariff_evidence_remains_historically_locked() -> None:
    _validate_m2_evidence()


def test_milestone_three_prefrozen_goldens_are_independently_consistent() -> None:
    validate_m3_goldens()


def test_milestone_three_comparison_evidence_remains_historically_locked() -> None:
    _validate_m3_evidence()
