from __future__ import annotations

import json
import subprocess

import pytest

from scripts.qualify_m7_deployment import (
    QualificationError,
    _inject_unexpected_process_crash,
    _remove_created_dangling_images,
    _self_hash,
    parse_published_services,
    summarize_trivy_report,
    validate_deployment_evidence,
)


def test_worker_crash_injection_targets_workload_child_without_operator_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(
        command: tuple[str, ...],
        *,
        timeout: int,
        check: bool,
    ) -> subprocess.CompletedProcess[str]:
        observed.update(command=command, timeout=timeout, check=check)
        return subprocess.CompletedProcess(command, 137, "", "")

    monkeypatch.setattr("scripts.qualify_m7_deployment._run", fake_run)

    _inject_unexpected_process_crash("worker-container")

    assert observed["timeout"] == 30
    assert observed["check"] is False
    command = observed["command"]
    assert isinstance(command, tuple)
    assert command[:5] == ("docker", "exec", "worker-container", "python", "-c")
    assert "os.kill(pids[0], 9)" in command[5]
    assert "int(open(path+'/stat').read().split()[3]) == 1" in command[5]


def test_build_cleanup_removes_only_new_project_dangling_images(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removed: list[tuple[str, ...]] = []

    monkeypatch.setattr(
        "scripts.qualify_m7_deployment._dangling_project_images",
        lambda: frozenset({"existing", "new-b", "new-a"}),
    )

    def fake_run(command: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        removed.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("scripts.qualify_m7_deployment._run", fake_run)

    created = _remove_created_dangling_images(frozenset({"existing"}))

    assert created == ("new-a", "new-b")
    assert removed == [
        ("docker", "image", "rm", "new-a"),
        ("docker", "image", "rm", "new-b"),
    ]


def test_compose_port_parser_ignores_exposed_only_ports() -> None:
    rows = (
        {"Service": "api", "Publishers": [{"PublishedPort": 0, "TargetPort": 8000}]},
        {"Service": "proxy", "Publishers": [{"PublishedPort": 58443, "TargetPort": 58443}]},
        {"Service": "worker", "Publishers": None},
    )
    rendered = "\n".join(json.dumps(row) for row in rows)

    assert parse_published_services(rendered) == ["proxy"]


def test_trivy_summary_requires_detected_results_and_counts_critical() -> None:
    report = {
        "Results": [
            {
                "Target": "usr/bin/service",
                "Type": "gobinary",
                "Vulnerabilities": [{"VulnerabilityID": "CVE-TEST", "Severity": "CRITICAL"}],
            },
            {"Target": "alpine", "Type": "alpine", "Vulnerabilities": None},
        ]
    }

    assert summarize_trivy_report(report) == {
        "critical_findings": 1,
        "detector_types": ["alpine", "gobinary"],
        "targets_scanned": 2,
    }
    with pytest.raises(QualificationError, match="TRIVY_RESULTS_MISSING"):
        summarize_trivy_report({"Results": []})


def test_deployment_evidence_self_hash_and_gate_fields_fail_closed() -> None:
    payload = {
        "schema_version": "m7-local-deployment-evidence-v1",
        "evidence_level": "LOCAL_REPRODUCIBLE",
        "gate_result": "PASS",
        "security": {
            "ignored_critical_findings": 0,
            "critical_findings": 0,
            "dependency_audit_passed": True,
            "ephemeral_build_images_removed": 2,
        },
        "topology": {"published_services": ["proxy"], "https_ready": True},
        "failure_injections": [
            {"passed": True},
            {"passed": True},
            {"passed": True},
        ],
        "rollback": {
            "persistent_session_survived": True,
            "fresh_login_succeeded": True,
            "same_schema": True,
        },
        "claims_withheld": [
            "HOSTED_VALIDATED",
            "MANAGED_VOLUME_ENCRYPTION",
            "PRODUCTION_ACME_TLS",
            "PRODUCTION_NETWORK_ISOLATION",
            "PRODUCTION_ORCHESTRATOR_ROLLBACK",
        ],
    }
    payload["artifact_sha256"] = _self_hash(payload)
    validate_deployment_evidence(payload)

    payload["topology"]["published_services"] = ["api", "proxy"]
    with pytest.raises(QualificationError, match=r"ARTIFACT_HASH|PUBLICATION_SCOPE"):
        validate_deployment_evidence(payload)
