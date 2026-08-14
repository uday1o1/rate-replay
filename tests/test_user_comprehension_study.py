import json
from pathlib import Path

import pytest

from scripts.user_comprehension_study import (
    ATTESTATION_KEYS,
    COHORT_SIZE,
    PASS_THRESHOLD,
    RUN_SCHEMA_VERSION,
    WORKFLOW_STEP_IDS,
    StudyValidationError,
    initialize_run,
    load_protocol,
    validate_run,
    validate_run_chain,
)


def _write_run(
    path: Path,
    *,
    attempt: int,
    successful_participants: int,
    participant_prefix: str = "p",
    previous: tuple[str, str] | None = None,
) -> None:
    protocol = load_protocol()
    participants: list[dict[str, object]] = []
    for index in range(COHORT_SIZE):
        answers = []
        for question_index, question in enumerate(protocol.questions):
            answer_id = question.accepted_answer_id
            if index >= successful_participants and question_index == 0:
                answer_id = next(
                    candidate
                    for candidate in question.allowed_answer_ids
                    if candidate != question.accepted_answer_id
                )
            answers.append(
                {
                    "question_id": question.question_id,
                    "answer_id": answer_id,
                    "response_summary": "An anonymized summary of the participant response.",
                }
            )
        participants.append(
            {
                "participant_id": f"{participant_prefix}-{index:02d}",
                "first_time_user": True,
                "implementation_documents_read": False,
                "coaching_received": False,
                "duration_seconds": 420 + index,
                "workflow": [
                    {
                        "step_id": step_id,
                        "completed_independently": True,
                        "wrong_turns": [],
                    }
                    for step_id in WORKFLOW_STEP_IDS
                ],
                "answers": answers,
                "anonymized_observations": [],
            }
        )
    document = {
        "schema_version": RUN_SCHEMA_VERSION,
        "protocol_version": "user-comprehension-protocol-v1",
        "protocol_sha256": protocol.sha256,
        "demo_manifest_sha256": protocol.demo_manifest_sha256,
        "run_id": f"m6-comprehension-v1-run-{attempt:02d}",
        "attempt_number": attempt,
        "previous_run": (
            None if previous is None else {"run_id": previous[0], "sha256": previous[1]}
        ),
        "environment": {
            "git_commit": "a" * 40,
            "demo_url": "http://127.0.0.1:4173/#demo",
            "browser_name": "Test browser",
            "browser_version": "1.0",
        },
        "run_attestation": {name: True for name in ATTESTATION_KEYS},
        "participants": participants,
    }
    path.write_text(json.dumps(document), encoding="utf-8")


def test_frozen_protocol_matches_the_demo_and_exact_scoring_contract() -> None:
    protocol = load_protocol()

    assert len(protocol.questions) == 5
    assert protocol.demo_manifest_sha256 == (
        "8bdac2156589cf07522c4784813acf268a5334b03d079f3a364c9514c2967559"
    )


def test_four_of_five_complete_first_time_participants_pass(tmp_path: Path) -> None:
    run = tmp_path / "m6-comprehension-v1-run-01.json"
    _write_run(run, attempt=1, successful_participants=PASS_THRESHOLD)

    summaries = validate_run_chain([run], load_protocol())

    assert summaries[-1].passed is True
    assert summaries[-1].successful_participants == PASS_THRESHOLD


def test_three_of_five_is_a_gate_failure_without_rewriting_the_run(tmp_path: Path) -> None:
    run = tmp_path / "m6-comprehension-v1-run-01.json"
    _write_run(run, attempt=1, successful_participants=PASS_THRESHOLD - 1)

    with pytest.raises(StudyValidationError, match="USER_STUDY_GATE_FAILED"):
        validate_run_chain([run], load_protocol())

    assert validate_run(run, load_protocol()).successful_participants == PASS_THRESHOLD - 1


def test_retry_requires_preserved_failure_and_fresh_participants(tmp_path: Path) -> None:
    failed = tmp_path / "m6-comprehension-v1-run-01.json"
    _write_run(failed, attempt=1, successful_participants=PASS_THRESHOLD - 1)
    failed_summary = validate_run(failed, load_protocol())
    passed = tmp_path / "m6-comprehension-v1-run-02.json"
    _write_run(
        passed,
        attempt=2,
        successful_participants=PASS_THRESHOLD,
        participant_prefix="q",
        previous=(failed_summary.run_id, failed_summary.sha256),
    )

    summaries = validate_run_chain([passed, failed], load_protocol())

    assert [summary.passed for summary in summaries] == [False, True]


def test_retry_rejects_reused_participant_identifiers(tmp_path: Path) -> None:
    failed = tmp_path / "m6-comprehension-v1-run-01.json"
    _write_run(failed, attempt=1, successful_participants=PASS_THRESHOLD - 1)
    failed_summary = validate_run(failed, load_protocol())
    retry = tmp_path / "m6-comprehension-v1-run-02.json"
    _write_run(
        retry,
        attempt=2,
        successful_participants=PASS_THRESHOLD,
        previous=(failed_summary.run_id, failed_summary.sha256),
    )

    with pytest.raises(StudyValidationError, match="PARTICIPANT_REUSED_ACROSS_RUNS"):
        validate_run_chain([failed, retry], load_protocol())


def test_coaching_or_missing_participant_attestation_fails_closed(tmp_path: Path) -> None:
    run = tmp_path / "m6-comprehension-v1-run-01.json"
    _write_run(run, attempt=1, successful_participants=COHORT_SIZE)
    document = json.loads(run.read_text(encoding="utf-8"))
    document["participants"][0]["coaching_received"] = True
    run.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(StudyValidationError, match="PARTICIPANT_COACHED"):
        validate_run_chain([run], load_protocol())

    document["participants"][0]["coaching_received"] = False
    document["run_attestation"]["all_participants_included"] = False
    run.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(StudyValidationError, match="ATTESTATION_NOT_CONFIRMED"):
        validate_run_chain([run], load_protocol())


def test_initializer_is_incomplete_and_refuses_to_overwrite(tmp_path: Path) -> None:
    output = tmp_path / "m6-comprehension-v1-run-01.json"
    initialize_run(
        output=output,
        attempt=1,
        git_commit="b" * 40,
        demo_url="http://localhost:4173/#demo",
        browser_name="Test browser",
        browser_version="1.0",
        previous_run=None,
    )

    with pytest.raises(StudyValidationError, match="ATTESTATION_NOT_CONFIRMED"):
        validate_run(output, load_protocol())
    with pytest.raises(StudyValidationError, match="OUTPUT_ALREADY_EXISTS"):
        initialize_run(
            output=output,
            attempt=1,
            git_commit="b" * 40,
            demo_url="http://localhost:4173/#demo",
            browser_name="Test browser",
            browser_version="1.0",
            previous_run=None,
        )


def test_missing_evidence_never_passes() -> None:
    with pytest.raises(StudyValidationError, match="USER_STUDY_EVIDENCE_MISSING"):
        validate_run_chain([], load_protocol())
