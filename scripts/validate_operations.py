"""Validate the versioned local observability contract and operator assets."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import yaml  # type: ignore[import-untyped]

REPOSITORY_ROOT: Final = Path(__file__).resolve().parents[1]
CONTRACT_PATH: Final = REPOSITORY_ROOT / "ops/observability/sli-contract.v1.json"
ALERTS_PATH: Final = REPOSITORY_ROOT / "ops/prometheus/alerts.v1.yml"
DASHBOARD_PATH: Final = REPOSITORY_ROOT / "ops/grafana/ratereplay-overview.v1.json"

REQUIRED_METRICS: Final = frozenset(
    {
        "ratereplay_deletion_results_total",
        "ratereplay_http_request_duration_seconds",
        "ratereplay_http_requests_total",
        "ratereplay_import_requests_total",
        "ratereplay_job_oldest_lease_age_seconds",
        "ratereplay_job_queue_depth",
        "ratereplay_job_retry_attempts",
        "ratereplay_parser_duration_seconds",
        "ratereplay_parser_peak_resident_memory_bytes",
        "ratereplay_quality_findings_total",
        "ratereplay_report_generation_duration_seconds",
        "ratereplay_scenario_duration_seconds",
        "ratereplay_solver_duration_seconds",
        "ratereplay_solver_results_total",
        "ratereplay_worker_runs_total",
    }
)
REQUIRED_ALERTS: Final = frozenset(
    {
        "RateReplayApiErrorBudgetBurn",
        "RateReplayDeletionFailures",
        "RateReplayLeaseStalled",
        "RateReplayScenarioBacklog",
        "RateReplayWorkerRetries",
    }
)


class OperationsContractError(RuntimeError):
    """Raised when a versioned operator asset is incomplete or unsafe."""


def _object(path: Path) -> dict[str, Any]:
    try:
        source = path.read_text(encoding="utf-8")
        payload = yaml.safe_load(source) if path.suffix in {".yaml", ".yml"} else json.loads(source)
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as error:
        raise OperationsContractError(
            f"{path.relative_to(REPOSITORY_ROOT)} is invalid structured data"
        ) from error
    if not isinstance(payload, dict):
        raise OperationsContractError(f"{path.relative_to(REPOSITORY_ROOT)} must be an object")
    return payload


def validate_operations_assets() -> dict[str, int]:
    contract = _object(CONTRACT_PATH)
    alerts = _object(ALERTS_PATH)
    dashboard = _object(DASHBOARD_PATH)

    indicators = contract.get("indicators")
    if not isinstance(indicators, list) or not indicators:
        raise OperationsContractError("SLI contract must define indicators")
    metrics = {
        metric
        for indicator in indicators
        if isinstance(indicator, dict)
        for metric in indicator.get("metrics", [])
        if isinstance(metric, str)
    }
    missing_metrics = REQUIRED_METRICS - metrics
    if missing_metrics:
        raise OperationsContractError(
            f"SLI contract is missing metrics: {', '.join(sorted(missing_metrics))}"
        )
    for indicator in indicators:
        if not isinstance(indicator, dict):
            raise OperationsContractError("Every SLI must be an object")
        if not all(
            isinstance(indicator.get(field), str) and indicator[field]
            for field in ("id", "description", "query", "interpretation")
        ):
            raise OperationsContractError("Every SLI needs an id, query, and interpretation")

    groups = alerts.get("groups")
    if not isinstance(groups, list) or not groups:
        raise OperationsContractError("Alert rules must define a group")
    rules = [
        rule
        for group in groups
        if isinstance(group, dict)
        for rule in group.get("rules", [])
        if isinstance(rule, dict)
    ]
    alert_names = {rule.get("alert") for rule in rules}
    missing_alerts = REQUIRED_ALERTS - alert_names
    if missing_alerts:
        raise OperationsContractError(
            f"Alert rules are missing: {', '.join(sorted(missing_alerts))}"
        )
    for rule in rules:
        annotations = rule.get("annotations")
        labels = rule.get("labels")
        if not isinstance(rule.get("expr"), str) or not rule["expr"]:
            raise OperationsContractError("Every alert must define an expression")
        if not isinstance(annotations, dict) or not isinstance(annotations.get("runbook"), str):
            raise OperationsContractError("Every alert must link a runbook section")
        if not isinstance(labels, dict) or labels.get("severity") not in {"page", "ticket"}:
            raise OperationsContractError("Every alert must use an allowed severity")

    panels = dashboard.get("panels")
    if not isinstance(panels, list) or len(panels) < 5:
        raise OperationsContractError("Dashboard must contain at least five panels")
    expressions = {
        target.get("expr")
        for panel in panels
        if isinstance(panel, dict)
        for target in panel.get("targets", [])
        if isinstance(target, dict)
    }
    if not any(
        isinstance(expression, str) and "ratereplay_http_requests_total" in expression
        for expression in expressions
    ):
        raise OperationsContractError("Dashboard must expose API errors")
    if not any(
        isinstance(expression, str) and "ratereplay_job_queue_depth" in expression
        for expression in expressions
    ):
        raise OperationsContractError("Dashboard must expose durable job backlog")

    telemetry_source = (
        REPOSITORY_ROOT / "packages/domain/ratereplay_domain/telemetry.py"
    ).read_text(encoding="utf-8")
    unimplemented = {metric for metric in REQUIRED_METRICS if metric not in telemetry_source}
    if unimplemented:
        raise OperationsContractError(
            f"Contract references unimplemented metrics: {', '.join(sorted(unimplemented))}"
        )
    return {
        "alerts": len(rules),
        "indicators": len(indicators),
        "panels": len(panels),
    }


def main() -> None:
    result = validate_operations_assets()
    print(
        "OPERATIONS_CONFIG_OK "
        f"indicators={result['indicators']} alerts={result['alerts']} panels={result['panels']}"
    )


if __name__ == "__main__":
    main()
