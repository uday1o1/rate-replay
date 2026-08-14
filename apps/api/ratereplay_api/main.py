"""RateReplay modular-monolith HTTP application."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from ratereplay_persistence import models as persistence_models  # noqa: F401
from ratereplay_persistence.comparisons import ComparisonService
from ratereplay_persistence.database import Base, make_engine, make_session_factory
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.object_store import FilesystemObjectStore
from ratereplay_persistence.replays import ReplayService
from ratereplay_persistence.scenarios import ScenarioService
from ratereplay_tariffs.admission import load_all_admitted_tariffs

from ratereplay_api.auth import AuthService, LoginRateLimiter
from ratereplay_api.auth_routes import router as auth_router
from ratereplay_api.comparison_routes import router as comparison_router
from ratereplay_api.config import AppSettings
from ratereplay_api.import_routes import router as import_router
from ratereplay_api.problems import install_problem_handler
from ratereplay_api.replay_routes import router as replay_router
from ratereplay_api.scenario_routes import router as scenario_router


def create_app(settings: AppSettings | None = None) -> FastAPI:
    resolved = settings or AppSettings.from_environment()
    application = FastAPI(title="RateReplay API", version="0.1.0")
    engine = make_engine(resolved.database_url)
    if resolved.auto_create_schema:
        Base.metadata.create_all(engine)
    application.state.settings = resolved
    application.state.engine = engine
    application.state.session_factory = make_session_factory(engine)
    application.state.object_store = FilesystemObjectStore(resolved.object_store_root)
    application.state.import_service = ImportService(
        application.state.session_factory,
        application.state.object_store,
    )
    application.state.job_service = JobService(application.state.session_factory)
    application.state.replay_service = ReplayService(application.state.session_factory)
    application.state.comparison_service = ComparisonService(application.state.session_factory)
    application.state.scenario_service = ScenarioService(application.state.session_factory)
    admitted_tariffs = load_all_admitted_tariffs(resolved.repository_root)
    application.state.admitted_tariffs = {
        admitted.lock.tariff_version_id: admitted for admitted in admitted_tariffs
    }
    application.state.admitted_e1 = application.state.admitted_tariffs["pge-e1-2026-07"]
    application.state.auth_service = AuthService(resolved.session_key)
    application.state.login_limiter = LoginRateLimiter(resolved.session_key)
    application.state.upload_limiter = LoginRateLimiter(
        resolved.session_key,
        limit=10,
        code="UPLOAD_RATE_LIMITED",
        message="Too many import requests. Try again later.",
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=[resolved.allowed_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "X-CSRF-Token", "Idempotency-Key"],
    )
    install_problem_handler(application)

    @application.middleware("http")
    async def attach_request_id(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_id = secrets.token_hex(12)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @application.get("/healthz", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

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
    return application


app = create_app()
