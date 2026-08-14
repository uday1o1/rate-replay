"""Low-cardinality, data-minimizing metrics and traces."""

from __future__ import annotations

import os
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
from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest

TraceExporterName = Literal["none", "console"]
T = TypeVar("T")

TELEMETRY_SCHEMA_VERSION: Final = "ratereplay-telemetry-v1"
SAFE_HTTP_METHODS: Final = frozenset({"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"})
SAFE_WORKER_KINDS: Final = frozenset(
    {"COMPARISON", "DELETION", "IMPORT", "REPLAY", "REPORT", "RETENTION", "SCENARIO"}
)


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
    def http_request(self, method: str) -> Iterator[HttpRequestObservation]:
        """Trace one HTTP request without recording its URL, payload, or identifiers."""

        safe_method = method if method in SAFE_HTTP_METHODS else "OTHER"
        observation = HttpRequestObservation(self, safe_method, time.perf_counter())
        span = self.tracer.start_span(
            "http.server.request",
            attributes={"http.request.method": safe_method},
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

    def shutdown(self) -> None:
        """Flush configured trace processors before process shutdown."""

        self._provider.shutdown()


class HttpRequestObservation:
    """Mutable request timing state retained only for the middleware call."""

    def __init__(self, telemetry: Telemetry, method: str, started: float) -> None:
        self._telemetry = telemetry
        self._method = method
        self._started = started
        self._span: trace.Span | None = None
        self.finished = False

    def finish(self, *, route: str, status_code: int, failed: bool = False) -> None:
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
            if failed:
                self._span.set_status(Status(StatusCode.ERROR))
            self._span.end()
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
