#!/usr/bin/env python3
"""Initialize and validate the frozen Milestone 6 comprehension study."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Never, cast

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATH = ROOT / "studies/user-comprehension/protocol-v1.json"
SCHEMA_PATH = ROOT / "studies/user-comprehension/result.schema.v1.json"
RESULT_GLOB = "m6-comprehension-v1-run-*.json"

PROTOCOL_VERSION = "user-comprehension-protocol-v1"
RUN_SCHEMA_VERSION = "user-comprehension-run-v1"
COHORT_SIZE = 5
PASS_THRESHOLD = 4
WORKFLOW_STEP_IDS = (
    "import_review",
    "bill_replay",
    "plan_comparison",
    "load_scheduling",
    "report_review",
)
ACCEPTED_ANSWERS = {
    "residual": "ENTERED_MINUS_SUPPORTED_AND_EXPLICIT_UNSUPPORTED",
    "unsupported_scope": "CURRENT_RECONCILIATION_ONLY_EXCLUDED_FROM_ALTERNATIVES",
    "reference_schedule": "USER_SUPPLIED_UNOPTIMIZED_COMPARISON_BASELINE",
    "historical_addition": "PAST_PERIOD_COUNTERFACTUAL_NOT_FORECAST",
    "solver_status": "OPTIMAL_ALL_FOUR_STAGES_PROVED",
}
RUN_KEYS = {
    "schema_version",
    "protocol_version",
    "protocol_sha256",
    "demo_manifest_sha256",
    "run_id",
    "attempt_number",
    "previous_run",
    "environment",
    "run_attestation",
    "participants",
}
ENVIRONMENT_KEYS = {"git_commit", "demo_url", "browser_name", "browser_version"}
ATTESTATION_KEYS = {
    "all_participants_included",
    "implementation_documents_not_shared",
    "no_coaching_given",
    "responses_paraphrased_and_anonymized",
    "no_sensitive_source_data_recorded",
}
PARTICIPANT_KEYS = {
    "participant_id",
    "first_time_user",
    "implementation_documents_read",
    "coaching_received",
    "duration_seconds",
    "workflow",
    "answers",
    "anonymized_observations",
}


class StudyValidationError(RuntimeError):
    """A frozen study invariant does not hold."""


@dataclass(frozen=True)
class Question:
    """One frozen question and its complete coding vocabulary."""

    question_id: str
    accepted_answer_id: str
    allowed_answer_ids: frozenset[str]


@dataclass(frozen=True)
class Protocol:
    """Validated frozen protocol metadata used to score run records."""

    sha256: str
    demo_manifest_sha256: str
    questions: tuple[Question, ...]


@dataclass(frozen=True)
class RunSummary:
    """Derived, non-sensitive qualification summary for one run."""

    path: Path
    sha256: str
    run_id: str
    attempt_number: int
    previous_run_id: str | None
    previous_run_sha256: str | None
    participant_ids: frozenset[str]
    successful_participants: int

    @property
    def passed(self) -> bool:
        """Return whether this run reaches the frozen four-of-five threshold."""

        return self.successful_participants >= PASS_THRESHOLD


def _fail(code: str) -> Never:
    raise StudyValidationError(code)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_object(path: Path) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as error:
        raise StudyValidationError(f"INVALID_JSON:{path}") from error
    return _object(value, f"ROOT_NOT_OBJECT:{path}")


def _object(value: object, code: str) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _fail(code)
    return cast(dict[str, object], value)


def _array(value: object, code: str) -> list[object]:
    if not isinstance(value, list):
        _fail(code)
    return cast(list[object], value)


def _string(value: object, code: str, *, minimum: int = 1, maximum: int = 500) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        _fail(code)
    return value


def _integer(value: object, code: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        _fail(code)
    return value


def _exact_keys(value: dict[str, object], keys: set[str], code: str) -> None:
    if set(value) != keys:
        _fail(code)


def _true(value: object, code: str) -> None:
    if value is not True:
        _fail(code)


def _false(value: object, code: str) -> None:
    if value is not False:
        _fail(code)


def _string_list(
    value: object,
    code: str,
    *,
    maximum_items: int,
    maximum_length: int,
) -> list[str]:
    items = _array(value, code)
    if len(items) > maximum_items:
        _fail(code)
    return [_string(item, code, maximum=maximum_length) for item in items]


def load_protocol() -> Protocol:
    """Load the protocol and prove its frozen invariants and static-demo binding."""

    value = _load_object(PROTOCOL_PATH)
    _exact_keys(
        value,
        {
            "protocol_version",
            "demo_manifest",
            "result_schema_path",
            "cohort_size",
            "pass_threshold",
            "requirements",
            "workflow_steps",
            "questions",
        },
        "PROTOCOL_KEYS_DRIFT",
    )
    if value["protocol_version"] != PROTOCOL_VERSION:
        _fail("PROTOCOL_VERSION_DRIFT")
    if value["result_schema_path"] != str(SCHEMA_PATH.relative_to(ROOT)):
        _fail("RESULT_SCHEMA_PATH_DRIFT")
    if value["cohort_size"] != COHORT_SIZE or value["pass_threshold"] != PASS_THRESHOLD:
        _fail("PROTOCOL_THRESHOLD_DRIFT")

    requirements = _object(value["requirements"], "PROTOCOL_REQUIREMENTS_INVALID")
    _exact_keys(
        requirements,
        {
            "first_time_users_only",
            "implementation_documents_unread",
            "uncoached",
            "all_participants_recorded",
            "fresh_participants_after_failed_run",
            "sensitive_source_data_prohibited",
        },
        "PROTOCOL_REQUIREMENT_KEYS_DRIFT",
    )
    for requirement, enabled in requirements.items():
        _true(enabled, f"PROTOCOL_REQUIREMENT_DISABLED:{requirement}")

    manifest = _object(value["demo_manifest"], "PROTOCOL_DEMO_MANIFEST_INVALID")
    _exact_keys(manifest, {"path", "sha256"}, "PROTOCOL_DEMO_MANIFEST_KEYS_DRIFT")
    manifest_path = ROOT / _string(manifest["path"], "PROTOCOL_DEMO_PATH_INVALID")
    manifest_sha256 = _string(
        manifest["sha256"], "PROTOCOL_DEMO_HASH_INVALID", minimum=64, maximum=64
    )
    if not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        _fail("PROTOCOL_DEMO_HASH_INVALID")
    if _sha256(manifest_path) != manifest_sha256:
        _fail("PROTOCOL_DEMO_HASH_MISMATCH")

    steps = _array(value["workflow_steps"], "PROTOCOL_WORKFLOW_INVALID")
    observed_step_ids: list[str] = []
    for step in steps:
        item = _object(step, "PROTOCOL_WORKFLOW_STEP_INVALID")
        _exact_keys(item, {"step_id", "instruction"}, "PROTOCOL_WORKFLOW_STEP_KEYS_DRIFT")
        observed_step_ids.append(_string(item["step_id"], "PROTOCOL_WORKFLOW_STEP_ID_INVALID"))
        _string(item["instruction"], "PROTOCOL_WORKFLOW_INSTRUCTION_INVALID")
    if tuple(observed_step_ids) != WORKFLOW_STEP_IDS:
        _fail("PROTOCOL_WORKFLOW_ORDER_DRIFT")

    raw_questions = _array(value["questions"], "PROTOCOL_QUESTIONS_INVALID")
    questions: list[Question] = []
    for raw_question in raw_questions:
        item = _object(raw_question, "PROTOCOL_QUESTION_INVALID")
        _exact_keys(
            item,
            {"question_id", "prompt", "accepted_answer_id", "answer_options"},
            "PROTOCOL_QUESTION_KEYS_DRIFT",
        )
        question_id = _string(item["question_id"], "PROTOCOL_QUESTION_ID_INVALID")
        _string(item["prompt"], "PROTOCOL_QUESTION_PROMPT_INVALID")
        accepted = _string(item["accepted_answer_id"], "PROTOCOL_ACCEPTED_ANSWER_INVALID")
        options = _array(item["answer_options"], "PROTOCOL_ANSWER_OPTIONS_INVALID")
        allowed: set[str] = set()
        for raw_option in options:
            option = _object(raw_option, "PROTOCOL_ANSWER_OPTION_INVALID")
            _exact_keys(option, {"answer_id", "rubric"}, "PROTOCOL_ANSWER_OPTION_KEYS_DRIFT")
            answer_id = _string(option["answer_id"], "PROTOCOL_ANSWER_ID_INVALID")
            _string(option["rubric"], "PROTOCOL_ANSWER_RUBRIC_INVALID")
            if answer_id in allowed:
                _fail(f"PROTOCOL_DUPLICATE_ANSWER_ID:{question_id}:{answer_id}")
            allowed.add(answer_id)
        if ACCEPTED_ANSWERS.get(question_id) != accepted:
            _fail(f"PROTOCOL_ACCEPTED_ANSWER_DRIFT:{question_id}")
        if accepted not in allowed:
            _fail(f"PROTOCOL_ACCEPTED_ANSWER_MISSING:{question_id}")
        questions.append(Question(question_id, accepted, frozenset(allowed)))
    if tuple(question.question_id for question in questions) != tuple(ACCEPTED_ANSWERS):
        _fail("PROTOCOL_QUESTION_ORDER_DRIFT")

    schema = _load_object(SCHEMA_PATH)
    if schema.get("$schema") != "https://json-schema.org/draft/2020-12/schema":
        _fail("RESULT_SCHEMA_DIALECT_DRIFT")
    if schema.get("additionalProperties") is not False:
        _fail("RESULT_SCHEMA_NOT_CLOSED")
    return Protocol(_sha256(PROTOCOL_PATH), manifest_sha256, tuple(questions))


def _validate_workflow(value: object, participant_id: str) -> bool:
    workflow = _array(value, f"WORKFLOW_INVALID:{participant_id}")
    if len(workflow) != len(WORKFLOW_STEP_IDS):
        _fail(f"WORKFLOW_STEP_COUNT_INVALID:{participant_id}")
    completed = True
    observed: list[str] = []
    for raw_step in workflow:
        step = _object(raw_step, f"WORKFLOW_STEP_INVALID:{participant_id}")
        _exact_keys(
            step,
            {"step_id", "completed_independently", "wrong_turns"},
            f"WORKFLOW_STEP_KEYS_INVALID:{participant_id}",
        )
        step_id = _string(step["step_id"], f"WORKFLOW_STEP_ID_INVALID:{participant_id}")
        observed.append(step_id)
        if not isinstance(step["completed_independently"], bool):
            _fail(f"WORKFLOW_COMPLETION_INVALID:{participant_id}:{step_id}")
        completed = completed and step["completed_independently"]
        _string_list(
            step["wrong_turns"],
            f"WRONG_TURNS_INVALID:{participant_id}:{step_id}",
            maximum_items=20,
            maximum_length=300,
        )
    if tuple(observed) != WORKFLOW_STEP_IDS:
        _fail(f"WORKFLOW_STEP_ORDER_INVALID:{participant_id}")
    return completed


def _validate_answers(value: object, participant_id: str, protocol: Protocol) -> bool:
    answers = _array(value, f"ANSWERS_INVALID:{participant_id}")
    if len(answers) != len(protocol.questions):
        _fail(f"ANSWER_COUNT_INVALID:{participant_id}")
    all_correct = True
    observed: list[str] = []
    for raw_answer, question in zip(answers, protocol.questions, strict=True):
        answer = _object(raw_answer, f"ANSWER_INVALID:{participant_id}")
        _exact_keys(
            answer,
            {"question_id", "answer_id", "response_summary"},
            f"ANSWER_KEYS_INVALID:{participant_id}",
        )
        question_id = _string(answer["question_id"], f"QUESTION_ID_INVALID:{participant_id}")
        observed.append(question_id)
        answer_id = _string(answer["answer_id"], f"ANSWER_ID_INVALID:{participant_id}", maximum=100)
        _string(
            answer["response_summary"],
            f"RESPONSE_SUMMARY_INVALID:{participant_id}:{question_id}",
        )
        if question_id != question.question_id:
            _fail(f"QUESTION_ORDER_INVALID:{participant_id}")
        if answer_id not in question.allowed_answer_ids:
            _fail(f"UNKNOWN_ANSWER_ID:{participant_id}:{question_id}")
        all_correct = all_correct and answer_id == question.accepted_answer_id
    if tuple(observed) != tuple(question.question_id for question in protocol.questions):
        _fail(f"QUESTION_COVERAGE_INVALID:{participant_id}")
    return all_correct


def validate_run(path: Path, protocol: Protocol) -> RunSummary:
    """Validate and derive the score for one complete study run."""

    value = _load_object(path)
    _exact_keys(value, RUN_KEYS, f"RUN_KEYS_INVALID:{path.name}")
    if value["schema_version"] != RUN_SCHEMA_VERSION:
        _fail(f"RUN_SCHEMA_VERSION_INVALID:{path.name}")
    if value["protocol_version"] != PROTOCOL_VERSION:
        _fail(f"RUN_PROTOCOL_VERSION_INVALID:{path.name}")
    if value["protocol_sha256"] != protocol.sha256:
        _fail(f"RUN_PROTOCOL_HASH_MISMATCH:{path.name}")
    if value["demo_manifest_sha256"] != protocol.demo_manifest_sha256:
        _fail(f"RUN_DEMO_HASH_MISMATCH:{path.name}")

    attempt = _integer(
        value["attempt_number"], f"ATTEMPT_INVALID:{path.name}", minimum=1, maximum=99
    )
    expected_run_id = f"m6-comprehension-v1-run-{attempt:02d}"
    run_id = _string(value["run_id"], f"RUN_ID_INVALID:{path.name}")
    if run_id != expected_run_id or path.name != f"{run_id}.json":
        _fail(f"RUN_ID_OR_FILENAME_MISMATCH:{path.name}")

    previous = value["previous_run"]
    previous_id: str | None = None
    previous_sha256: str | None = None
    if attempt == 1:
        if previous is not None:
            _fail(f"UNEXPECTED_PREVIOUS_RUN:{path.name}")
    else:
        previous_object = _object(previous, f"PREVIOUS_RUN_INVALID:{path.name}")
        _exact_keys(previous_object, {"run_id", "sha256"}, f"PREVIOUS_RUN_KEYS_INVALID:{path.name}")
        previous_id = _string(previous_object["run_id"], f"PREVIOUS_RUN_ID_INVALID:{path.name}")
        previous_sha256 = _string(
            previous_object["sha256"],
            f"PREVIOUS_RUN_HASH_INVALID:{path.name}",
            minimum=64,
            maximum=64,
        )
        if not re.fullmatch(r"[0-9a-f]{64}", previous_sha256):
            _fail(f"PREVIOUS_RUN_HASH_INVALID:{path.name}")

    environment = _object(value["environment"], f"ENVIRONMENT_INVALID:{path.name}")
    _exact_keys(environment, ENVIRONMENT_KEYS, f"ENVIRONMENT_KEYS_INVALID:{path.name}")
    git_commit = _string(
        environment["git_commit"], f"GIT_COMMIT_INVALID:{path.name}", minimum=40, maximum=40
    )
    if not re.fullmatch(r"[0-9a-f]{40}", git_commit):
        _fail(f"GIT_COMMIT_INVALID:{path.name}")
    demo_url = _string(environment["demo_url"], f"DEMO_URL_INVALID:{path.name}")
    if not demo_url.startswith(("http://127.0.0.1:", "http://localhost:")) or not demo_url.endswith(
        "/#demo"
    ):
        _fail(f"DEMO_URL_NOT_LOCAL_PUBLIC_DEMO:{path.name}")
    _string(environment["browser_name"], f"BROWSER_NAME_INVALID:{path.name}", maximum=100)
    _string(environment["browser_version"], f"BROWSER_VERSION_INVALID:{path.name}", maximum=100)

    attestation = _object(value["run_attestation"], f"ATTESTATION_INVALID:{path.name}")
    _exact_keys(attestation, ATTESTATION_KEYS, f"ATTESTATION_KEYS_INVALID:{path.name}")
    for name, confirmed in attestation.items():
        _true(confirmed, f"ATTESTATION_NOT_CONFIRMED:{path.name}:{name}")

    raw_participants = _array(value["participants"], f"PARTICIPANTS_INVALID:{path.name}")
    if len(raw_participants) != COHORT_SIZE:
        _fail(f"COHORT_SIZE_INVALID:{path.name}")
    participant_ids: set[str] = set()
    successful = 0
    for raw_participant in raw_participants:
        participant = _object(raw_participant, f"PARTICIPANT_INVALID:{path.name}")
        _exact_keys(participant, PARTICIPANT_KEYS, f"PARTICIPANT_KEYS_INVALID:{path.name}")
        participant_id = _string(
            participant["participant_id"], f"PARTICIPANT_ID_INVALID:{path.name}", maximum=32
        )
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,31}", participant_id):
            _fail(f"PARTICIPANT_ID_INVALID:{path.name}")
        if participant_id in participant_ids:
            _fail(f"DUPLICATE_PARTICIPANT_ID:{path.name}:{participant_id}")
        participant_ids.add(participant_id)
        _true(participant["first_time_user"], f"PARTICIPANT_NOT_FIRST_TIME:{participant_id}")
        _false(
            participant["implementation_documents_read"],
            f"PARTICIPANT_READ_IMPLEMENTATION_DOCS:{participant_id}",
        )
        _false(participant["coaching_received"], f"PARTICIPANT_COACHED:{participant_id}")
        _integer(
            participant["duration_seconds"],
            f"DURATION_INVALID:{participant_id}",
            minimum=1,
            maximum=14_400,
        )
        workflow_complete = _validate_workflow(participant["workflow"], participant_id)
        answers_correct = _validate_answers(participant["answers"], participant_id, protocol)
        _string_list(
            participant["anonymized_observations"],
            f"OBSERVATIONS_INVALID:{participant_id}",
            maximum_items=20,
            maximum_length=500,
        )
        if workflow_complete and answers_correct:
            successful += 1

    return RunSummary(
        path=path,
        sha256=_sha256(path),
        run_id=run_id,
        attempt_number=attempt,
        previous_run_id=previous_id,
        previous_run_sha256=previous_sha256,
        participant_ids=frozenset(participant_ids),
        successful_participants=successful,
    )


def validate_run_chain(paths: list[Path], protocol: Protocol) -> tuple[RunSummary, ...]:
    """Validate a complete retry chain and require its latest run to pass."""

    existing = sorted({path.resolve() for path in paths if path.is_file()})
    if not existing:
        _fail("USER_STUDY_EVIDENCE_MISSING")
    summaries = tuple(
        sorted(
            (validate_run(path, protocol) for path in existing),
            key=lambda item: item.attempt_number,
        )
    )
    expected_attempts = tuple(range(1, len(summaries) + 1))
    if tuple(summary.attempt_number for summary in summaries) != expected_attempts:
        _fail("RUN_CHAIN_INCOMPLETE")

    prior_participants: set[str] = set()
    for index, summary in enumerate(summaries):
        reused = prior_participants & summary.participant_ids
        if reused:
            _fail(f"PARTICIPANT_REUSED_ACROSS_RUNS:{summary.run_id}")
        prior_participants.update(summary.participant_ids)
        if index == 0:
            continue
        previous = summaries[index - 1]
        if previous.passed:
            _fail(f"RETRY_AFTER_PASS:{summary.run_id}")
        if (
            summary.previous_run_id != previous.run_id
            or summary.previous_run_sha256 != previous.sha256
        ):
            _fail(f"PREVIOUS_RUN_LINK_MISMATCH:{summary.run_id}")
    latest = summaries[-1]
    if not latest.passed:
        _fail(
            f"USER_STUDY_GATE_FAILED:{latest.run_id}:{latest.successful_participants}/{COHORT_SIZE}"
        )
    return summaries


def initialize_run(
    *,
    output: Path,
    attempt: int,
    git_commit: str,
    demo_url: str,
    browser_name: str,
    browser_version: str,
    previous_run: Path | None,
) -> None:
    """Create a deliberately incomplete, non-overwriting study record."""

    if output.exists():
        _fail(f"OUTPUT_ALREADY_EXISTS:{output}")
    if not 1 <= attempt <= 99:
        _fail("ATTEMPT_INVALID")
    run_id = f"m6-comprehension-v1-run-{attempt:02d}"
    if output.name != f"{run_id}.json":
        _fail("OUTPUT_FILENAME_DOES_NOT_MATCH_ATTEMPT")
    if not re.fullmatch(r"[0-9a-f]{40}", git_commit):
        _fail("GIT_COMMIT_INVALID")

    protocol = load_protocol()
    previous_reference: dict[str, object] | None = None
    if attempt == 1:
        if previous_run is not None:
            _fail("UNEXPECTED_PREVIOUS_RUN")
    else:
        if previous_run is None:
            _fail("PREVIOUS_RUN_REQUIRED")
        previous_summary = validate_run(previous_run, protocol)
        if previous_summary.attempt_number != attempt - 1:
            _fail("PREVIOUS_RUN_ATTEMPT_INVALID")
        if previous_summary.passed:
            _fail("PREVIOUS_RUN_ALREADY_PASSED")
        previous_reference = {
            "run_id": previous_summary.run_id,
            "sha256": previous_summary.sha256,
        }

    participants: list[dict[str, object]] = []
    for _ in range(COHORT_SIZE):
        participants.append(
            {
                "participant_id": f"p-{secrets.token_hex(5)}",
                "first_time_user": False,
                "implementation_documents_read": False,
                "coaching_received": False,
                "duration_seconds": 0,
                "workflow": [
                    {
                        "step_id": step_id,
                        "completed_independently": False,
                        "wrong_turns": [],
                    }
                    for step_id in WORKFLOW_STEP_IDS
                ],
                "answers": [
                    {
                        "question_id": question.question_id,
                        "answer_id": "UNRECORDED",
                        "response_summary": "",
                    }
                    for question in protocol.questions
                ],
                "anonymized_observations": [],
            }
        )
    document: dict[str, object] = {
        "schema_version": RUN_SCHEMA_VERSION,
        "protocol_version": PROTOCOL_VERSION,
        "protocol_sha256": protocol.sha256,
        "demo_manifest_sha256": protocol.demo_manifest_sha256,
        "run_id": run_id,
        "attempt_number": attempt,
        "previous_run": previous_reference,
        "environment": {
            "git_commit": git_commit,
            "demo_url": demo_url,
            "browser_name": browser_name,
            "browser_version": browser_version,
        },
        "run_attestation": {name: False for name in sorted(ATTESTATION_KEYS)},
        "participants": participants,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("protocol", help="validate the frozen protocol and demo binding")

    initialize = commands.add_parser(
        "init", help="create an incomplete five-participant run record"
    )
    initialize.add_argument("--attempt", required=True, type=int)
    initialize.add_argument("--git-commit", required=True)
    initialize.add_argument("--demo-url", required=True)
    initialize.add_argument("--browser-name", required=True)
    initialize.add_argument("--browser-version", required=True)
    initialize.add_argument("--output", required=True, type=Path)
    initialize.add_argument("--previous-run", type=Path)

    validate = commands.add_parser("validate", help="validate a complete run or retry chain")
    validate.add_argument("paths", nargs="*", type=Path)
    validate.add_argument("--results-dir", type=Path)
    return parser


def main() -> int:
    """Run the study command-line interface."""

    args = _parser().parse_args()
    try:
        protocol = load_protocol()
        if args.command == "protocol":
            print(
                f"PASS protocol={PROTOCOL_VERSION} protocol_sha256={protocol.sha256} "
                f"demo_manifest_sha256={protocol.demo_manifest_sha256}"
            )
            return 0
        if args.command == "init":
            initialize_run(
                output=args.output,
                attempt=args.attempt,
                git_commit=args.git_commit,
                demo_url=args.demo_url,
                browser_name=args.browser_name,
                browser_version=args.browser_version,
                previous_run=args.previous_run,
            )
            print(f"CREATED incomplete_run={args.output}")
            return 0

        paths = list(args.paths)
        if args.results_dir is not None and args.results_dir.is_dir():
            paths.extend(sorted(args.results_dir.glob(RESULT_GLOB)))
        summaries = validate_run_chain(paths, protocol)
        latest = summaries[-1]
        print(
            f"PASS latest_run={latest.run_id} successful_participants="
            f"{latest.successful_participants}/{COHORT_SIZE} attempts={len(summaries)}"
        )
        return 0
    except StudyValidationError as error:
        print(f"FAIL {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
