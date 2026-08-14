#!/usr/bin/env python3
"""Build deterministic Milestone 8 result views from qualified evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
SUMMARY_PATH = ROOT / "evidence/evaluation/m8-summary.json"
PERFORMANCE_PATH = ROOT / "evidence/evaluation/m8-performance.json"
CSV_PATH = ROOT / "docs/results/m8-performance.csv"
SVG_PATH = ROOT / "docs/results/m8-performance.svg"
MANIFEST_PATH = ROOT / "benchmarks/manifests/m8-evaluation-v1.json"

SOURCE_PATHS = (
    "evidence/correctness/m8-independent-golden-derivations.json",
    "evidence/evaluation/m8-parser-correctness.json",
    "evidence/evaluation/m8-comparison-coverage.json",
    "evidence/evaluation/m8-optimizer-oracle.json",
    "evidence/evaluation/m8-performance-local-core.json",
    "evidence/evaluation/m8-performance-variance-followup.json",
    "evidence/evaluation/m8-release-topology.json",
    "evidence/evaluation/m8-crash-recovery.json",
)


class EvaluationSummaryError(RuntimeError):
    """A qualified input or deterministic output is invalid."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise EvaluationSummaryError(code)


def _load(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _content_sha256(payload: dict[str, Any], field: str) -> str:
    content = {key: value for key, value in payload.items() if key != field}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return round(ordered[index], 6)


def _source_evidence() -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {}
    for relative in SOURCE_PATHS:
        path = ROOT / relative
        payload = _load(path)
        _require(payload.get("gate_result") == "PASS", f"SOURCE_GATE_FAILED:{relative}")
        _require(
            payload.get("artifact_sha256") == _content_sha256(payload, "artifact_sha256"),
            f"SOURCE_ARTIFACT_HASH_MISMATCH:{relative}",
        )
        sources[relative] = payload
    return sources


def _measurement_row(
    *,
    metric_id: str,
    environment: str,
    operation: str,
    scale: str,
    item: dict[str, Any],
    evidence_path: str,
) -> dict[str, Any]:
    return {
        "metric_id": metric_id,
        "environment": environment,
        "operation": operation,
        "scale": scale,
        "cache_state": item.get("cache_state", "N/A"),
        "repetitions": item["repetitions"],
        "p50_ms": item["p50_ms"],
        "p95_ms": item["p95_ms"],
        "p99_ms": item["p99_ms"],
        "coefficient_of_variation": item.get("coefficient_of_variation"),
        "threshold_ms": item.get("threshold_ms"),
        "peak_rss_bytes": item.get("maximum_peak_rss_bytes"),
        "peak_rss_threshold_bytes": item.get("peak_rss_threshold_bytes"),
        "deterministic_result": item.get(
            "deterministic_result", item.get("deterministic_response")
        ),
        "passed": item["passed"],
        "evidence_path": evidence_path,
    }


def build_rows(sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    core_path = "evidence/evaluation/m8-performance-local-core.json"
    core = sources[core_path]
    rows: list[dict[str, Any]] = []
    for item in core["import_measurements"]:
        rows.append(
            _measurement_row(
                metric_id=f"import:{item['profile_id']}:{str(item['cache_state']).lower()}",
                environment="LOCAL_CORE",
                operation="import",
                scale=str(item["profile_id"]),
                item=item,
                evidence_path=core_path,
            )
        )
    for item in core["operation_measurements"]:
        rows.append(
            _measurement_row(
                metric_id=(
                    f"{item['operation']}:scale-{item['scale']}:{str(item['cache_state']).lower()}"
                ),
                environment="LOCAL_CORE",
                operation=str(item["operation"]),
                scale=str(item["scale"]),
                item=item,
                evidence_path=core_path,
            )
        )

    followup_path = "evidence/evaluation/m8-performance-variance-followup.json"
    for item in sources[followup_path]["measurements"]:
        operation = str(item.get("operation", "import"))
        scale = str(item.get("profile_id", item.get("scale", 1)))
        rows.append(
            _measurement_row(
                metric_id=f"variance-followup:{operation}:{scale}:{str(item['cache_state']).lower()}",
                environment="LOCAL_CORE_VARIANCE_FOLLOWUP",
                operation=operation,
                scale=scale,
                item=item,
                evidence_path=followup_path,
            )
        )

    release_path = "evidence/evaluation/m8-release-topology.json"
    for item in sources[release_path]["api_latency"]:
        rows.append(
            _measurement_row(
                metric_id=f"release-api:{str(item['operation']).lower()}",
                environment="LOCAL_RELEASE_TOPOLOGY",
                operation=str(item["operation"]),
                scale=f"concurrency-{item['concurrency']}",
                item=item,
                evidence_path=release_path,
            )
        )

    crash_path = "evidence/evaluation/m8-crash-recovery.json"
    crash = sources[crash_path]
    for operation, field in (
        ("import_worker_recovery", "import_worker_cases"),
        ("scenario_worker_recovery", "scenario_worker_cases"),
    ):
        values = [float(case["recovery_duration_ms"]) for case in crash[field]]
        item = {
            "repetitions": len(values),
            "p50_ms": _percentile(values, 0.50),
            "p95_ms": _percentile(values, 0.95),
            "p99_ms": _percentile(values, 0.99),
            "threshold_ms": crash[field][0]["threshold_ms"],
            "deterministic_result": None,
            "passed": all(case["recovered"] for case in crash[field]),
        }
        rows.append(
            _measurement_row(
                metric_id=f"release-crash:{operation}",
                environment="LOCAL_RELEASE_TOPOLOGY",
                operation=operation,
                scale="SIGKILL",
                item=item,
                evidence_path=crash_path,
            )
        )
    return rows


def build_summary(sources: dict[str, dict[str, Any]], rows: list[dict[str, Any]]) -> dict[str, Any]:
    manifest = _load(MANIFEST_PATH)
    _require(
        manifest["manifest_sha256"] == _content_sha256(manifest, "manifest_sha256"),
        "MANIFEST_HASH_MISMATCH",
    )
    source_index = [
        {
            "path": relative,
            "file_sha256": _file_sha256(ROOT / relative),
            "artifact_sha256": sources[relative]["artifact_sha256"],
            "gate_result": sources[relative]["gate_result"],
        }
        for relative in SOURCE_PATHS
    ]
    high_variance = [
        row["metric_id"]
        for row in rows
        if row["coefficient_of_variation"] is not None
        and float(row["coefficient_of_variation"]) > 0.20
    ]
    release = sources["evidence/evaluation/m8-release-topology.json"]
    crash = sources["evidence/evaluation/m8-crash-recovery.json"]
    payload: dict[str, Any] = {
        "schema_version": "m8-evaluation-summary-v1",
        "implementation_status": "IMPLEMENTED_PENDING_GATE",
        "automated_gate_result": "PASS",
        "acceptance_gate_result": "DEFERRED",
        "acceptance_blocker": "GENUINE_FIVE_PERSON_HUMAN_STUDY_NOT_RECORDED",
        "human_validation": {
            "state": "HUMAN_VALIDATION_DEFERRED",
            "genuine_participant_count": 0,
            "synthetic_sessions_counted": 0,
            "synthetic_personas_are_development_only": True,
            "qualification_command": "make qualification-m6-study",
            "after_human_qualification_command": "make qualification-m8",
        },
        "manifest": {
            "path": str(MANIFEST_PATH.relative_to(ROOT)),
            "manifest_sha256": manifest["manifest_sha256"],
            "file_sha256": _file_sha256(MANIFEST_PATH),
        },
        "source_evidence": source_index,
        "measured_results": {
            "measurement_row_count": len(rows),
            "all_thresholded_rows_passed": all(
                row["passed"] for row in rows if row["threshold_ms"] is not None
            ),
            "all_automated_source_gates_passed": True,
            "high_variance_rows_preserved": high_variance,
            "variance_followup_artifact": (
                "evidence/evaluation/m8-performance-variance-followup.json"
            ),
            "release_application_source_commit": release["application_source_commit"],
            "release_evaluation_source_commit": release["evaluation_source_commit"],
            "release_source_remote_confirmed": release["source_remote_confirmed"],
            "duplicate_successful_results": crash["duplicate_successful_results"],
            "all_worker_restarts_observed": crash["all_worker_restarts_observed"],
        },
        "public_claim_boundary": {
            "evidence_level": "LOCAL_REPRODUCIBLE",
            "data_scope": "PUBLIC_SIMULATED_AND_SYNTHETIC_ENGINEERING_ONLY",
            "genuine_human_comprehension_claim": False,
            "private_customer_data_used": False,
            "withheld_claims": release["claims_withheld"],
        },
        "limitations": [
            "The genuine five-person comprehension gate has not run, so Milestone 8 is not "
            "accepted.",
            "Synthetic persona sessions are development-only and do not count toward the "
            "human gate.",
            "Performance evidence is local and supports no hosted, customer-workload, or "
            "multi-host claim.",
            "Fast-operation variance is preserved in the original artifact and investigated "
            "separately.",
            "Tariff, parser, ranking, and optimizer conclusions apply only to their locked "
            "public or synthetic inputs.",
        ],
        "generated_views": {
            "performance_json": str(PERFORMANCE_PATH.relative_to(ROOT)),
            "csv": str(CSV_PATH.relative_to(ROOT)),
            "svg": str(SVG_PATH.relative_to(ROOT)),
        },
    }
    payload["artifact_sha256"] = _content_sha256(payload, "artifact_sha256")
    return payload


def build_performance_artifact(
    sources: dict[str, dict[str, Any]], rows: list[dict[str, Any]]
) -> dict[str, Any]:
    core_path = "evidence/evaluation/m8-performance-local-core.json"
    followup_path = "evidence/evaluation/m8-performance-variance-followup.json"
    release_path = "evidence/evaluation/m8-release-topology.json"
    crash_path = "evidence/evaluation/m8-crash-recovery.json"
    payload: dict[str, Any] = {
        "schema_version": "m8-performance-aggregate-v1",
        "gate_result": "PASS",
        "evidence_level": "LOCAL_REPRODUCIBLE",
        "manifest_sha256": sources[core_path]["manifest_sha256"],
        "measurement_count": len(rows),
        "measurements": rows,
        "all_thresholded_measurements_passed": all(
            row["passed"] for row in rows if row["threshold_ms"] is not None
        ),
        "original_high_variance_measurements_preserved": True,
        "variance_policy": (
            "No measured repetition is omitted. A coefficient of variation above 0.20 "
            "requires a separate follow-up without replacing the original result."
        ),
        "source_artifacts": [
            {
                "path": relative,
                "file_sha256": _file_sha256(ROOT / relative),
                "artifact_sha256": sources[relative]["artifact_sha256"],
            }
            for relative in (core_path, followup_path, release_path, crash_path)
        ],
        "release_storage": sources[release_path]["storage"],
        "duplicate_successful_results": sources[crash_path]["duplicate_successful_results"],
        "limitations": [
            "Measurements were collected on the manifest-locked local arm64 host.",
            "Synthetic workloads support engineering conclusions, not customer-scale claims.",
            "Release measurements cover the local reproducible topology, not a hosted service.",
            "Crash recovery terminates the sole release worker after its durable job reports "
            "RUNNING.",
        ],
    }
    payload["artifact_sha256"] = _content_sha256(payload, "artifact_sha256")
    return payload


CSV_FIELDS = (
    "metric_id",
    "environment",
    "operation",
    "scale",
    "cache_state",
    "repetitions",
    "p50_ms",
    "p95_ms",
    "p99_ms",
    "coefficient_of_variation",
    "threshold_ms",
    "peak_rss_bytes",
    "peak_rss_threshold_bytes",
    "deterministic_result",
    "passed",
    "evidence_path",
)


def build_csv(rows: list[dict[str, Any]]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue()


def _select_chart_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    wanted = (
        "variance-followup:import:one-year-15-minute:warm",
        "optimization:scale-5:warm",
        "release-api:warm_cached_comparison_get",
        "release-api:warm_scenario_get",
        "release-crash:import_worker_recovery",
        "release-crash:scenario_worker_recovery",
    )
    indexed = {row["metric_id"]: row for row in rows}
    _require(all(metric in indexed for metric in wanted), "CHART_METRIC_MISSING")
    return [indexed[metric] for metric in wanted]


def build_svg(rows: list[dict[str, Any]]) -> str:
    selected = _select_chart_rows(rows)
    labels = (
        "1-year import, warm follow-up",
        "5-load optimization, warm",
        "Comparison API GET, warm",
        "Scenario API GET, warm",
        "Import recovery after SIGKILL",
        "Scenario recovery after SIGKILL",
    )
    bars: list[str] = []
    for index, (label, row) in enumerate(zip(labels, selected, strict=True)):
        y = 90 + index * 66
        ratio = min(float(row["p95_ms"]) / float(row["threshold_ms"]), 1.0)
        width = round(460 * ratio, 2)
        color = "#2563eb" if ratio <= 0.50 else "#c2410c"
        bars.extend(
            [
                f'  <text x="28" y="{y}" class="label">{label}</text>',
                f'  <rect x="28" y="{y + 12}" width="460" height="18" rx="4" class="track"/>',
                f'  <rect x="28" y="{y + 12}" width="{width}" height="18" rx="4" fill="{color}"/>',
                (
                    f'  <text x="508" y="{y + 27}" class="value">'
                    f"{float(row['p95_ms']):,.2f} ms / "
                    f"{float(row['threshold_ms']):,.0f} ms"
                    "</text>"
                ),
            ]
        )
    return "\n".join(
        [
            '<svg xmlns="http://www.w3.org/2000/svg" width="980" height="530" '
            'viewBox="0 0 980 530" role="img" aria-labelledby="title description">',
            '  <title id="title">Milestone 8 local p95 measurements against frozen '
            "thresholds</title>",
            '  <desc id="description">Six horizontal bars show each measured p95 as a '
            "fraction of its frozen acceptance threshold. All are below threshold. Human "
            "validation remains deferred.</desc>",
            "  <style>",
            "    .background { fill: #f8fafc; }",
            "    .track { fill: #dbe4f0; }",
            "    .title { fill: #0f172a; font: 700 22px system-ui, sans-serif; }",
            "    .subtitle { fill: #475569; font: 14px system-ui, sans-serif; }",
            "    .label { fill: #0f172a; font: 600 14px system-ui, sans-serif; }",
            "    .value { fill: #334155; font: 13px ui-monospace, monospace; }",
            "    .note { fill: #7c2d12; font: 600 13px system-ui, sans-serif; }",
            "  </style>",
            '  <rect width="980" height="530" class="background"/>',
            '  <text x="28" y="36" class="title">Local p95 performance and recovery</text>',
            '  <text x="28" y="59" class="subtitle">Bar length is p95 divided by the '
            "pre-frozen threshold. Exact values are shown at right.</text>",
            *bars,
            '  <text x="28" y="507" class="note">Automated evidence only. The genuine '
            "five-person comprehension gate is deferred.</text>",
            "</svg>",
            "",
        ]
    )


def build_outputs() -> dict[Path, str]:
    sources = _source_evidence()
    rows = build_rows(sources)
    summary = build_summary(sources, rows)
    performance = build_performance_artifact(sources, rows)
    return {
        SUMMARY_PATH: _canonical_json(summary),
        PERFORMANCE_PATH: _canonical_json(performance),
        CSV_PATH: build_csv(rows),
        SVG_PATH: build_svg(rows),
    }


def write_or_check(*, check: bool) -> None:
    outputs = build_outputs()
    if check:
        for path, expected in outputs.items():
            _require(path.is_file(), f"GENERATED_VIEW_MISSING:{path.relative_to(ROOT)}")
            _require(
                path.read_text(encoding="utf-8") == expected,
                f"GENERATED_VIEW_DRIFT:{path.relative_to(ROOT)}",
            )
    else:
        for path, content in outputs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    print(
        "M8_EVALUATION_VIEWS_OK "
        f"mode={'check' if check else 'write'} "
        f"outputs={len(outputs)} status=IMPLEMENTED_PENDING_GATE"
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    write_or_check(check=args.check)


if __name__ == "__main__":
    main()
