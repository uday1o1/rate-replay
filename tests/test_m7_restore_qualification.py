from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.qualify_m7_restore import (
    EVIDENCE_SCHEMA,
    LOCAL_EVIDENCE_LEVEL,
    QualificationError,
    _artifact_hash,
    _command_label,
    verify_evidence,
    write_evidence,
)


def _evidence() -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": EVIDENCE_SCHEMA,
        "evidence_level": LOCAL_EVIDENCE_LEVEL,
        "gate_result": "PASS",
        "source_commit": "a" * 40,
        "generated_at": "2026-08-14T12:00:00+00:00",
        "environment": {},
        "inputs": {},
        "topology": {},
        "backup": {},
        "failure_injections": [
            {"id": "MISSING_LEDGER", "expected": "LEDGER_MISSING", "passed": True}
        ],
        "reconciliation": {},
        "backup_retention": {},
        "rollback": {},
        "claims_withheld": ["HOSTED_VALIDATED"],
    }
    payload["artifact_sha256"] = _artifact_hash(payload)
    return payload


def test_m7_evidence_is_content_addressed_and_written_without_private_values(
    tmp_path: Path,
) -> None:
    payload = _evidence()
    destination = tmp_path / "m7.json"

    write_evidence(destination, payload)

    assert verify_evidence(json.loads(destination.read_text(encoding="ascii"))) == payload
    assert destination.stat().st_mode & 0o777 == 0o644
    encoded = destination.read_text(encoding="ascii")
    assert "postgresql://" not in encoded
    assert "/private/" not in encoded


def test_m7_evidence_rejects_failed_controls_and_digest_tampering() -> None:
    failed = _evidence()
    failed["failure_injections"] = [
        {"id": "MISSING_LEDGER", "expected": "LEDGER_MISSING", "passed": False}
    ]
    failed["artifact_sha256"] = _artifact_hash(failed)
    with pytest.raises(QualificationError, match="M7_EVIDENCE_INVALID"):
        verify_evidence(failed)

    tampered = _evidence()
    tampered["gate_result"] = "FAILED"
    with pytest.raises(QualificationError):
        verify_evidence(tampered)


def test_m7_command_labels_disclose_only_allowlisted_operations() -> None:
    assert _command_label(("uv", "run", "alembic", "upgrade", "head")) == ("uv:alembic:upgrade")
    assert _command_label(("uv", "run", "unknown", "secret-value")) == "uv"
