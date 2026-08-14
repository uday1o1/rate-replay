from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from ratereplay_domain.semantic_identity import SemanticCalculationIdentity
from ratereplay_persistence.artifacts import ArtifactService
from ratereplay_persistence.calculations import (
    CalculationSubmission,
    CalculationSubmissionError,
    CalculationSubmissionService,
)
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.models import ImportRecord, JobRecord, ProfileVersionRecord, UserRecord
from ratereplay_persistence.object_store import FilesystemObjectStore
from sqlalchemy.orm import Session, sessionmaker

NOW = datetime(2026, 8, 14, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class CalculationHarness:
    sessions: sessionmaker[Session]
    submissions: CalculationSubmissionService
    jobs: JobService
    artifacts: ArtifactService


@pytest.fixture
def harness(tmp_path: Path) -> CalculationHarness:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = make_session_factory(engine)
    for prefix in ("1", "2"):
        owner_id = prefix * 32
        import_id = f"{prefix}i" * 16
        profile_id = f"{prefix}p" * 16
        with sessions.begin() as database:
            database.add(
                UserRecord(
                    id=owner_id,
                    username_canonical=f"calculation_owner_{prefix}",
                    password_hash="test-only",
                    created_at=NOW,
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                )
            )
            database.flush()
            database.add(
                ImportRecord(
                    id=import_id,
                    owner_user_id=owner_id,
                    state="CONFIRMED",
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                    adapter="TEST_CANONICAL",
                    raw_content_hash="a" * 64,
                    created_at=NOW,
                    published_at=NOW,
                    confirmed_at=NOW,
                    profile_version_id=profile_id,
                )
            )
            database.flush()
            database.add(
                ProfileVersionRecord(
                    id=profile_id,
                    owner_user_id=owner_id,
                    import_id=import_id,
                    content_hash="1" * 64,
                    canonical_content=b"shared-profile",
                    billing_period_start_utc_ns=0,
                    billing_period_end_utc_ns=1,
                    tariff_timezone="America/Los_Angeles",
                    interval_resolution_seconds=900,
                    lifecycle_state="ACTIVE",
                    lifecycle_generation=0,
                    created_at=NOW,
                )
            )
    return CalculationHarness(
        sessions=sessions,
        submissions=CalculationSubmissionService(sessions),
        jobs=JobService(sessions),
        artifacts=ArtifactService(sessions, FilesystemObjectStore(tmp_path / "objects")),
    )


def _identity() -> SemanticCalculationIdentity:
    return SemanticCalculationIdentity(
        job_kind="REPLAY",
        request_schema_version="replay-operation-v1",
        calculation_contract_version="replay-contract-v1",
        environment_lock_hash="0" * 64,
        tariff_compiler_version="tariff-compiler-v1",
        billing_evaluator_version="billing-evaluator-v1",
        profile_version_hash="1" * 64,
        tariff_ast_hashes=("a" * 64,),
        component_vector_hashes=("b" * 64,),
        account_facts_hash="c" * 64,
        billing_period_identity_hash="d" * 64,
        reconciliation_inputs_hash="e" * 64,
        reconciliation_policy_hash="f" * 64,
    )


def _submit(
    harness: CalculationHarness,
    *,
    key: str,
    payload: dict[str, object] | None = None,
    identity: SemanticCalculationIdentity | None = None,
    owner_prefix: str = "1",
) -> CalculationSubmission:
    return harness.submissions.submit(
        owner_user_id=owner_prefix * 32,
        profile_version_id=f"{owner_prefix}p" * 16,
        job_kind="REPLAY",
        request_schema_version="replay-operation-v1",
        idempotency_key=key,
        operation_payload=payload or {"profile": owner_prefix, "tariff": "E-1"},
        semantic_identity=identity or _identity(),
        now=NOW,
    )


def test_operation_retry_returns_original_job_and_payload_conflict_fails(
    harness: CalculationHarness,
) -> None:
    first = _submit(harness, key="replay-key-one", payload={"b": 2, "a": 1})
    repeated = _submit(harness, key="replay-key-one", payload={"a": 1, "b": 2})
    assert repeated.repeated_operation
    assert repeated.job_id == first.job_id
    assert repeated.operation_request_hash == first.operation_request_hash
    with pytest.raises(CalculationSubmissionError) as raised:
        _submit(harness, key="replay-key-one", payload={"a": 1, "b": 3})
    assert raised.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_completed_semantic_result_is_reused_but_contract_change_is_new(
    harness: CalculationHarness,
) -> None:
    identity = _identity()
    first = _submit(harness, key="replay-key-one", identity=identity)
    lease = harness.jobs.lease_next(
        worker_id="replay-worker",
        now=NOW,
        kinds=frozenset({"REPLAY"}),
    )
    assert lease is not None and lease.job_id == first.job_id
    assert harness.jobs.start(lease, now=NOW)
    finalized = harness.artifacts.finalize(
        owner_user_id="1" * 32,
        lease=lease,
        semantic_hash=identity.sha256(),
        calculation_contract_version=identity.calculation_contract_version,
        result_type="REPLAY",
        result_id="9" * 32,
        artifact_registration_ids=(),
        now=NOW,
    )
    semantic_reuse = _submit(harness, key="replay-key-two", identity=identity)
    assert semantic_reuse.semantic_reuse
    assert semantic_reuse.job_id == finalized.accepted_job_id
    assert semantic_reuse.result_id == finalized.result_id
    changed_contract = replace(identity, calculation_contract_version="replay-contract-v2")
    new_calculation = _submit(
        harness,
        key="replay-key-three",
        identity=changed_contract,
    )
    assert not new_calculation.semantic_reuse
    assert new_calculation.semantic_hash != first.semantic_hash
    assert new_calculation.job_id != first.job_id


def test_same_semantics_in_two_accounts_create_separate_operations(
    harness: CalculationHarness,
) -> None:
    first = _submit(harness, key="replay-key-one", owner_prefix="1")
    second_identity = _identity()
    second = _submit(
        harness,
        key="replay-key-one",
        owner_prefix="2",
        identity=second_identity,
    )
    assert first.job_id != second.job_id
    assert first.semantic_hash == second.semantic_hash
    with harness.sessions() as database:
        first_job = database.get(JobRecord, first.job_id)
        second_job = database.get(JobRecord, second.job_id)
        assert first_job is not None and first_job.owner_user_id == "1" * 32
        assert second_job is not None and second_job.owner_user_id == "2" * 32
