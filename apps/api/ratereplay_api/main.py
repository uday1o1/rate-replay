"""RateReplay modular-monolith HTTP application."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import cast

from fastapi import Depends, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from ratereplay_domain.environment import environment_lock_hash
from ratereplay_domain.telemetry import Telemetry, TelemetryConfiguration
from ratereplay_ingestion.simulated import load_locked_simulated_profile
from ratereplay_persistence import models as persistence_models  # noqa: F401
from ratereplay_persistence.comparisons import ComparisonService
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.deletion_ledger import FilesystemDeletionLedger
from ratereplay_persistence.deletions import DeletionCoordinator
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.object_store import ObjectStoreError
from ratereplay_persistence.replays import ReplayService
from ratereplay_persistence.reports import ReportService
from ratereplay_persistence.scenarios import ScenarioService
from ratereplay_tariffs.admission import load_all_admitted_tariffs
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from ratereplay_api.abuse import SlidingWindowRateLimiter, enforce_request_budget
from ratereplay_api.auth import AuthService, LoginRateLimiter
from ratereplay_api.auth_routes import router as auth_router
from ratereplay_api.comparison_routes import router as comparison_router
from ratereplay_api.config import AppSettings
from ratereplay_api.deletion_routes import router as deletion_router
from ratereplay_api.import_routes import router as import_router
from ratereplay_api.problems import ApiProblem, install_problem_handler, problem_openapi_responses
from ratereplay_api.replay_routes import router as replay_router
from ratereplay_api.resource_routes import router as resource_router
from ratereplay_api.scenario_routes import router as scenario_router


def create_app(
    settings: AppSettings | None = None,
    *,
    telemetry: Telemetry | None = None,
) -> FastAPI:
    resolved = settings or AppSettings.from_environment()
    application = FastAPI(
        title="RateReplay API",
        version="0.1.0",
        dependencies=[Depends(enforce_request_budget)],
        responses=problem_openapi_responses(429),
    )
    application.state.telemetry = telemetry or Telemetry(
        TelemetryConfiguration.from_environment(
            service_name="ratereplay-api",
            environment=resolved.environment,
        )
    )
    engine = make_engine(resolved.database_url)
    if resolved.auto_create_schema:
        Base.metadata.create_all(engine)
    application.state.settings = resolved
    application.state.engine = engine
    application.state.session_factory = make_session_factory(engine)
    application.state.object_store = resolved.object_store_configuration.build(
        ensure_bucket=resolved.environment == "development"
    )
    application.state.deletion_ledger = FilesystemDeletionLedger(
        resolved.deletion_ledger_root,
        integrity_key=resolved.deletion_ledger_key,
        restore_key_version=resolved.restore_key_version,
        actor="API_COORDINATOR",
    )
    application.state.built_in_simulated_profile = load_locked_simulated_profile(
        resolved.repository_root
    )
    application.state.import_service = ImportService(
        application.state.session_factory,
        application.state.object_store,
    )
    application.state.job_service = JobService(application.state.session_factory)
    application.state.deletion_coordinator = DeletionCoordinator(
        application.state.session_factory,
        application.state.deletion_ledger,
        restore_key=resolved.restore_suppression_key,
        restore_key_version=resolved.restore_key_version,
    )
    application.state.replay_service = ReplayService(application.state.session_factory)
    application.state.comparison_service = ComparisonService(application.state.session_factory)
    application.state.scenario_service = ScenarioService(application.state.session_factory)
    application.state.environment_lock_hash = environment_lock_hash(resolved.repository_root)
    application.state.report_service = ReportService(
        application.state.session_factory,
        environment_lock_hash=application.state.environment_lock_hash,
    )
    admitted_tariffs = load_all_admitted_tariffs(resolved.repository_root)
    application.state.admitted_tariffs = {
        admitted.lock.tariff_version_id: admitted for admitted in admitted_tariffs
    }
    application.state.admitted_e1 = application.state.admitted_tariffs["pge-e1-2026-07"]
    application.state.auth_service = AuthService(resolved.session_key)
    process_telemetry = cast(Telemetry, application.state.telemetry)
    on_rate_limit = process_telemetry.record_rate_limit_rejection
    application.state.login_limiter = LoginRateLimiter(
        resolved.session_key,
        on_reject=on_rate_limit,
    )
    application.state.upload_limiter = LoginRateLimiter(
        resolved.session_key,
        limit=10,
        code="UPLOAD_RATE_LIMITED",
        message="Too many import requests. Try again later.",
        scope="UPLOAD",
        on_reject=on_rate_limit,
    )
    application.state.read_limiter = SlidingWindowRateLimiter(
        resolved.session_key,
        limit=240,
        window=timedelta(minutes=1),
        code="READ_RATE_LIMITED",
        message="Too many read requests. Try again later.",
        scope="READ",
        on_reject=on_rate_limit,
    )
    application.state.mutation_limiter = SlidingWindowRateLimiter(
        resolved.session_key,
        limit=60,
        window=timedelta(minutes=1),
        code="MUTATION_RATE_LIMITED",
        message="Too many changes. Try again later.",
        scope="MUTATION",
        on_reject=on_rate_limit,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=[resolved.allowed_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=[
            "Content-Type",
            "X-CSRF-Token",
            "Idempotency-Key",
            "X-Deletion-Receipt-Secret",
        ],
    )
    install_problem_handler(application)

    @application.middleware("http")
    async def attach_request_id(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_id = secrets.token_hex(12)
        process_telemetry = cast(Telemetry, request.app.state.telemetry)
        with process_telemetry.http_request(
            request.method,
            request_id=request.state.request_id,
        ) as observation:
            response = await call_next(request)
            route = request.scope.get("route")
            route_path = getattr(route, "path", "unmatched")
            observation.finish(
                route=route_path,
                status_code=response.status_code,
                failed=response.status_code >= 500,
                error_code=getattr(request.state, "error_code", None),
                user_pseudonym=getattr(request.state, "user_pseudonym", None),
                job_id=getattr(request.state, "job_id", None),
            )
            response.headers["X-Request-ID"] = request.state.request_id
            return response

    @application.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/readyz", include_in_schema=False)
    def readiness(request: Request) -> dict[str, str]:
        ready = False
        try:
            with request.app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            request.app.state.object_store.list_prefix("__readiness__")
            ready = True
        except (ObjectStoreError, OSError, SQLAlchemyError) as error:
            raise ApiProblem(
                status_code=503,
                code="DEPENDENCY_UNAVAILABLE",
                message="A required service is unavailable.",
            ) from error
        finally:
            cast(Telemetry, request.app.state.telemetry).record_readiness(ready=ready)
        return {"status": "ready"}

    @application.get("/metrics", include_in_schema=False)
    def metrics(request: Request) -> Response:
        process_telemetry = cast(Telemetry, request.app.state.telemetry)
        return Response(
            content=process_telemetry.prometheus_bytes(),
            media_type="text/plain; version=0.0.4; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @application.get("/v1/meta")
    def metadata() -> dict[str, str]:
        return {
            "calculation_time_mode": "HISTORICAL_REPLAY",
            "evidence_level": "FOUNDATION_ONLY",
            "schema_version": "v1",
        }

    application.include_router(auth_router)
    application.include_router(import_router)
    application.include_router(replay_router)
    application.include_router(comparison_router)
    application.include_router(scenario_router)
    application.include_router(resource_router)
    application.include_router(deletion_router)
    return application


app = create_app()
