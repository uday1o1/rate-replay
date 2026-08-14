"""Low-cardinality, data-minimizing metrics and traces."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, Literal, TypeVar

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    ConsoleSpanExporter,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.trace import Status, StatusCode
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

TraceExporterName = Literal["none", "console"]
T = TypeVar("T")

TELEMETRY_SCHEMA_VERSION: Final = "ratereplay-telemetry-v1"
SAFE_HTTP_METHODS: Final = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
SAFE_WORKER_KINDS: Final = frozenset(
    {"COMPARISON", "DELETION", "IMPORT", "REPLAY", "REPORT", "RETENTION", "SCENARIO"}
)
SAFE_RATE_LIMIT_SCOPES: Final = frozenset({"AUTH", "MUTATION", "READ", "UPLOAD"})
SAFE_IMPORT_ADAPTERS: Final = frozenset({"ESPI_XML", "PGE_CSV", "SIMULATED"})
SAFE_IMPORT_OUTCOMES: Final = frozenset({"ACCEPTED", "FAILED", "REPEATED"})
SAFE_QUALITY_CODES: Final = frozenset(
    {
        "ESTIMATED_USING_REFERENCE_DAY",
        "INTERVAL_GAP",
        "MANUALLY_EDITED",
        "NON_MONOTONIC_INTERVALS",
    }
)
SAFE_SEVERITIES: Final = frozenset({"FATAL", "INFO", "WARNING"})
SAFE_SOLVER_STATUSES: Final = frozenset(
    {
        "BEST_FOUND",
        "MODEL_CONTRACT_VIOLATION",
        "MODEL_INVALID",
        "OPTIMAL",
        "REFERENCE",
        "UNKNOWN",
    }
)
SAFE_OPERATION_OUTCOMES: Final = frozenset({"FAILED", "SUCCEEDED"})
SAFE_WORKLOAD_SIZES: Final = frozenset({"0", "1", "2_5", "6_PLUS"})
SAFE_LOG_VALUE = re.compile(r"[A-Za-z0-9_.:/{}-]{1,128}\Z", re.ASCII)


@dataclass(frozen=True, slots=True)
class TelemetryConfiguration:
    """Exporter configuration that never accepts arbitrary resource attributes."""

    service_name: str
    environment: str
    trace_exporter: TraceExporterName = "none"

    @classmethod
    def from_environment(
        cls,
        *,
        service_name: str,
        environment: str,
    ) -> TelemetryConfiguration:
        exporter = os.getenv("RATEREPLAY_TRACE_EXPORTER", "none")
        if exporter not in {"none", "console"}:
            raise RuntimeError("RATEREPLAY_TRACE_EXPORTER must be 'none' or 'console'")
        selected_exporter: TraceExporterName = "console" if exporter == "console" else "none"
        return cls(
            service_name=_safe_service_name(service_name),
            environment=_safe_environment(environment),
            trace_exporter=selected_exporter,
        )


class Telemetry:
    """Own process-local telemetry providers with an intentionally tiny data surface."""

    def __init__(
        self,
        configuration: TelemetryConfiguration,
        *,
        span_exporter: SpanExporter | None = None,
    ) -> None:
        self.configuration = configuration
        self.registry = CollectorRegistry(auto_describe=True)
        self._http_requests = Counter(
            "ratereplay_http_requests_total",
            "HTTP requests completed by normalized route, method, and status.",
            ("route", "method", "status"),
            registry=self.registry,
        )
        self._http_duration = Histogram(
            "ratereplay_http_request_duration_seconds",
            "HTTP request duration by normalized route and method.",
            ("route", "method"),
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
        )
        self._worker_runs = Counter(
            "ratereplay_worker_runs_total",
            "Durable worker polling outcomes by fixed job kind.",
            ("kind", "outcome"),
            registry=self.registry,
        )
        self._worker_duration = Histogram(
            "ratereplay_worker_run_duration_seconds",
            "Durable worker polling duration by fixed job kind.",
            ("kind",),
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
        )
        self._rate_limit_rejections = Counter(
            "ratereplay_rate_limit_rejections_total",
            "Rejected requests by fixed request-budget scope.",
            ("scope",),
            registry=self.registry,
        )
        self._readiness_checks = Counter(
            "ratereplay_readiness_checks_total",
            "Readiness checks by fixed outcome.",
            ("outcome",),
            registry=self.registry,
        )
        self._import_requests = Counter(
            "ratereplay_import_requests_total",
            "Import submissions by fixed adapter and outcome.",
            ("adapter", "outcome"),
            registry=self.registry,
        )
        self._parser_duration = Histogram(
            "ratereplay_parser_duration_seconds",
            "Parser and normalization duration by fixed adapter.",
            ("adapter",),
            registry=self.registry,
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
        )
        self._parser_peak_memory = Histogram(
            "ratereplay_parser_peak_resident_memory_bytes",
            "Process peak resident memory observed after parsing.",
            ("adapter",),
            registry=self.registry,
            buckets=(
                16 * 1024 * 1024,
                32 * 1024 * 1024,
                64 * 1024 * 1024,
                128 * 1024 * 1024,
                256 * 1024 * 1024,
                512 * 1024 * 1024,
                1024 * 1024 * 1024,
            ),
        )
        self._quality_findings = Counter(
            "ratereplay_quality_findings_total",
            "Import quality findings by fixed code and severity.",
            ("code", "severity"),
            registry=self.registry,
        )
        self._job_queue_depth = Gauge(
            "ratereplay_job_queue_depth",
            "Runnable durable jobs by fixed kind.",
            ("kind",),
            registry=self.registry,
        )
        self._job_oldest_lease_age = Gauge(
            "ratereplay_job_oldest_lease_age_seconds",
            "Age of the oldest live lease by fixed kind.",
            ("kind",),
            registry=self.registry,
        )
        self._job_retry_attempts = Gauge(
            "ratereplay_job_retry_attempts",
            "Current retry attempts across nonterminal jobs by fixed kind.",
            ("kind",),
            registry=self.registry,
        )
        self._scenario_duration = Histogram(
            "ratereplay_scenario_duration_seconds",
            "Scenario end-to-end worker duration by workload-size bucket.",
            ("workload_size",),
            registry=self.registry,
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60),
        )
        self._solver_duration = Histogram(
            "ratereplay_solver_duration_seconds",
            "Exact solver duration by fixed terminal search status.",
            ("status",),
            registry=self.registry,
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
        )
        self._solver_results = Counter(
            "ratereplay_solver_results_total",
            "Exact solver results by fixed terminal search status.",
            ("status",),
            registry=self.registry,
        )
        self._report_duration = Histogram(
            "ratereplay_report_generation_duration_seconds",
            "Redacted report generation duration by fixed outcome.",
            ("outcome",),
            registry=self.registry,
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
        )
        self._deletion_results = Counter(
            "ratereplay_deletion_results_total",
            "Deletion worker outcomes by fixed terminal class.",
            ("outcome",),
            registry=self.registry,
        )
        self._logger = logging.getLogger("ratereplay.telemetry")
        resource = Resource.create(
            {
                "service.name": configuration.service_name,
                "deployment.environment.name": configuration.environment,
                "telemetry.schema.version": TELEMETRY_SCHEMA_VERSION,
            }
        )
        self._provider = TracerProvider(resource=resource)
        exporter = span_exporter
        if exporter is None and configuration.trace_exporter == "console":
            exporter = ConsoleSpanExporter()
        if exporter is not None:
            self._provider.add_span_processor(SimpleSpanProcessor(exporter))
        self.tracer = self._provider.get_tracer(
            "ratereplay.telemetry",
            instrumenting_library_version=TELEMETRY_SCHEMA_VERSION,
        )

    @contextmanager
    def http_request(
        self,
        method: str,
        *,
        request_id: str = "unavailable",
    ) -> Iterator[HttpRequestObservation]:
        """Trace one HTTP request without recording its URL, payload, or identifiers."""

        safe_method = method if method in SAFE_HTTP_METHODS else "OTHER"
        observation = HttpRequestObservation(
            self,
            safe_method,
            time.perf_counter(),
            _safe_log_value(request_id),
        )
        span = self.tracer.start_span(
            "http.server.request",
            attributes={
                "http.request.method": safe_method,
                "ratereplay.request.id": observation.request_id,
            },
        )
        observation._span = span
        with trace.use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                yield observation
            except Exception:
                observation.finish(route="unmatched", status_code=500, failed=True)
                raise
            finally:
                if not observation.finished:
                    observation.finish(route="unmatched", status_code=500, failed=True)

    def run_worker(self, kind: str, operation: Callable[[], bool]) -> bool:
        """Measure one worker poll without job IDs, inputs, results, or error messages."""

        safe_kind = _safe_worker_kind(kind)
        started = time.perf_counter()
        span = self.tracer.start_span(
            "worker.poll",
            attributes={"job.kind": safe_kind},
        )
        with trace.use_span(
            span,
            end_on_exit=False,
            record_exception=False,
            set_status_on_exception=False,
        ):
            try:
                processed = operation()
            except Exception:
                self._worker_runs.labels(kind=safe_kind, outcome="error").inc()
                span.set_attribute("worker.outcome", "error")
                span.set_status(Status(StatusCode.ERROR))
                raise
            else:
                outcome = "processed" if processed else "idle"
                self._worker_runs.labels(kind=safe_kind, outcome=outcome).inc()
                span.set_attribute("worker.outcome", outcome)
                span.set_status(Status(StatusCode.OK))
                return processed
            finally:
                self._worker_duration.labels(kind=safe_kind).observe(
                    max(0.0, time.perf_counter() - started)
                )
                span.end()

    def prometheus_bytes(self) -> bytes:
        """Render only this process's explicitly registered metrics."""

        return generate_latest(self.registry)

    def record_rate_limit_rejection(self, scope: str) -> None:
        """Count one rejected request without retaining a client or owner identity."""

        normalized = scope.upper()
        safe_scope = normalized if normalized in SAFE_RATE_LIMIT_SCOPES else "UNKNOWN"
        self._rate_limit_rejections.labels(scope=safe_scope).inc()

    def record_readiness(self, *, ready: bool) -> None:
        """Count readiness outcomes without recording dependency error details."""

        self._readiness_checks.labels(outcome="ready" if ready else "unready").inc()

    def record_import(self, *, adapter: str, outcome: str) -> None:
        self._import_requests.labels(
            adapter=_fixed_value(adapter, SAFE_IMPORT_ADAPTERS),
            outcome=_fixed_value(outcome, SAFE_IMPORT_OUTCOMES),
        ).inc()

    def observe_parser(self, *, adapter: str, duration_seconds: float, peak_bytes: int) -> None:
        safe_adapter = _fixed_value(adapter, SAFE_IMPORT_ADAPTERS)
        self._parser_duration.labels(adapter=safe_adapter).observe(max(0.0, duration_seconds))
        self._parser_peak_memory.labels(adapter=safe_adapter).observe(max(0, peak_bytes))

    def record_quality_finding(self, *, code: str, severity: str) -> None:
        self._quality_findings.labels(
            code=_fixed_value(code, SAFE_QUALITY_CODES),
            severity=_fixed_value(severity, SAFE_SEVERITIES),
        ).inc()

    def set_job_snapshot(
        self,
        *,
        kind: str,
        queue_depth: int,
        oldest_lease_age_seconds: float,
        retry_attempts: int,
    ) -> None:
        safe_kind = _safe_worker_kind(kind)
        self._job_queue_depth.labels(kind=safe_kind).set(max(0, queue_depth))
        self._job_oldest_lease_age.labels(kind=safe_kind).set(max(0.0, oldest_lease_age_seconds))
        self._job_retry_attempts.labels(kind=safe_kind).set(max(0, retry_attempts))

    def record_job_lease(self, *, kind: str, job_id: str, attempt_number: int) -> None:
        """Correlate the active worker span and structured event by random job identity."""

        safe_kind = _safe_worker_kind(kind)
        safe_job_id = _safe_log_value(job_id)
        current_span = trace.get_current_span()
        current_span.set_attribute("ratereplay.job.id", safe_job_id)
        current_span.set_attribute("ratereplay.job.attempt", max(1, attempt_number))
        self.log_event(
            "job_leased",
            job_id=safe_job_id,
            kind=safe_kind,
            version=TELEMETRY_SCHEMA_VERSION,
        )

    def observe_scenario(self, *, load_count: int, duration_seconds: float) -> None:
        self._scenario_duration.labels(workload_size=_workload_size(load_count)).observe(
            max(0.0, duration_seconds)
        )

    def observe_solver(self, *, status: str, duration_seconds: float) -> None:
        safe_status = _fixed_value(status, SAFE_SOLVER_STATUSES)
        self._solver_duration.labels(status=safe_status).observe(max(0.0, duration_seconds))
        self._solver_results.labels(status=safe_status).inc()

    def observe_report(self, *, outcome: str, duration_seconds: float) -> None:
        self._report_duration.labels(
            outcome=_fixed_value(outcome, SAFE_OPERATION_OUTCOMES)
        ).observe(max(0.0, duration_seconds))

    def record_deletion(self, *, outcome: str) -> None:
        self._deletion_results.labels(outcome=_fixed_value(outcome, SAFE_OPERATION_OUTCOMES)).inc()

    def log_event(self, event: str, **fields: str | int | float | None) -> None:
        """Emit one schema-bound JSON event and discard arbitrary unsafe values."""

        payload: dict[str, str | int | float] = {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "event": _safe_log_value(event),
        }
        permitted_fields = {
            "duration_ms",
            "error_code",
            "job_id",
            "kind",
            "outcome",
            "request_id",
            "route",
            "status",
            "user_pseudonym",
            "version",
        }
        for name, value in fields.items():
            if name not in permitted_fields or value is None:
                continue
            if name == "duration_ms" and isinstance(value, (int, float)):
                payload[name] = max(0.0, round(float(value), 3))
            elif isinstance(value, int) and name == "status" and 100 <= value <= 599:
                payload[name] = value
            elif isinstance(value, str):
                payload[name] = _safe_log_value(value)
        self._logger.info(json.dumps(payload, sort_keys=True, separators=(",", ":")))

    def shutdown(self) -> None:
        """Flush configured trace processors before process shutdown."""

        self._provider.shutdown()


class HttpRequestObservation:
    """Mutable request timing state retained only for the middleware call."""

    def __init__(
        self,
        telemetry: Telemetry,
        method: str,
        started: float,
        request_id: str,
    ) -> None:
        self._telemetry = telemetry
        self._method = method
        self._started = started
        self.request_id = request_id
        self._span: trace.Span | None = None
        self.finished = False

    def finish(
        self,
        *,
        route: str,
        status_code: int,
        failed: bool = False,
        error_code: str | None = None,
        user_pseudonym: str | None = None,
        job_id: str | None = None,
    ) -> None:
        if self.finished:
            return
        safe_route = _safe_route(route)
        safe_status = str(status_code if 100 <= status_code <= 599 else 500)
        duration = max(0.0, time.perf_counter() - self._started)
        self._telemetry._http_requests.labels(
            route=safe_route,
            method=self._method,
            status=safe_status,
        ).inc()
        self._telemetry._http_duration.labels(
            route=safe_route,
            method=self._method,
        ).observe(duration)
        if self._span is not None:
            self._span.set_attribute("http.route", safe_route)
            self._span.set_attribute("http.response.status_code", int(safe_status))
            if job_id is not None:
                self._span.set_attribute("ratereplay.job.id", _safe_log_value(job_id))
            if failed:
                self._span.set_status(Status(StatusCode.ERROR))
            self._span.end()
        self._telemetry.log_event(
            "http_request_completed",
            request_id=self.request_id,
            route=safe_route,
            status=int(safe_status),
            duration_ms=duration * 1000,
            error_code=error_code,
            user_pseudonym=user_pseudonym,
            job_id=job_id,
            version=TELEMETRY_SCHEMA_VERSION,
        )
        self.finished = True


def _safe_route(route: str) -> str:
    if not route.startswith("/") or len(route) > 128:
        return "unmatched"
    permitted = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_/{}.")
    if any(character not in permitted for character in route):
        return "unmatched"
    return route


def _safe_worker_kind(kind: str) -> str:
    normalized = kind.upper()
    return normalized if normalized in SAFE_WORKER_KINDS else "UNKNOWN"


def _safe_service_name(value: str) -> str:
    if value not in {"ratereplay-api", "ratereplay-worker"}:
        raise ValueError("Telemetry service name is not allowed")
    return value


def _safe_environment(value: str) -> str:
    if value not in {"development", "production", "staging", "test"}:
        raise ValueError("Telemetry environment is not allowed")
    return value


def _fixed_value(value: str, allowed: frozenset[str]) -> str:
    normalized = value.upper()
    return normalized if normalized in allowed else "OTHER"


def _workload_size(load_count: int) -> str:
    if load_count <= 0:
        return "0"
    if load_count == 1:
        return "1"
    if load_count <= 5:
        return "2_5"
    return "6_PLUS"


def _safe_log_value(value: str) -> str:
    return value if SAFE_LOG_VALUE.fullmatch(value) is not None else "redacted"
