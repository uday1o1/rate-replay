"""RateReplay modular-monolith HTTP application."""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from ratereplay_persistence import models as persistence_models  # noqa: F401
from ratereplay_persistence.database import Base, make_engine, make_session_factory

from ratereplay_api.auth import AuthService, LoginRateLimiter
from ratereplay_api.auth_routes import router as auth_router
from ratereplay_api.config import AppSettings
from ratereplay_api.problems import install_problem_handler


def create_app(settings: AppSettings | None = None) -> FastAPI:
    resolved = settings or AppSettings.from_environment()
    application = FastAPI(title="RateReplay API", version="0.1.0")
    engine = make_engine(resolved.database_url)
    if resolved.auto_create_schema:
        Base.metadata.create_all(engine)
    application.state.settings = resolved
    application.state.engine = engine
    application.state.session_factory = make_session_factory(engine)
    application.state.auth_service = AuthService(resolved.session_key)
    application.state.login_limiter = LoginRateLimiter(resolved.session_key)

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
    return application


app = create_app()
