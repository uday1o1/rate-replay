"""Public authentication routes and reusable authorization dependencies."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, cast

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy.orm import Session

from ratereplay_api.auth import AuthenticatedSession, AuthService, LoginRateLimiter, SessionGrant
from ratereplay_api.problems import ApiProblem, problem_openapi_responses

SESSION_COOKIE = "__Host-ratereplay_session"
CSRF_COOKIE = "__Host-ratereplay_csrf"


class RegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=3, max_length=64)
    password: SecretStr = Field(min_length=12, max_length=128)


class LoginRequest(RegistrationRequest):
    pass


class AuthenticatedUserResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    username: str


class SessionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = "auth-session-v1"
    user: AuthenticatedUserResponse
    csrf_token: str | None = None
    idle_expires_at: str
    absolute_expires_at: str


def get_database(request: Request) -> Iterator[Session]:
    database = request.app.state.session_factory()
    try:
        yield database
    finally:
        database.close()


def get_auth_service(request: Request) -> AuthService:
    return cast(AuthService, request.app.state.auth_service)


def require_same_origin(request: Request) -> None:
    if request.headers.get("origin") != request.app.state.settings.allowed_origin:
        raise ApiProblem(
            status_code=403,
            code="ORIGIN_REJECTED",
            message="The request origin is not allowed.",
        )


def get_authenticated_session(
    database: Annotated[Session, Depends(get_database)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> AuthenticatedSession:
    return auth.authenticate(database, session_token=session_token)


def require_csrf_session(
    request: Request,
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
) -> AuthenticatedSession:
    require_same_origin(request)
    auth.verify_csrf(authenticated, csrf_token)
    return authenticated


def _set_session_cookie(response: Response, grant: SessionGrant, *, secure: bool) -> None:
    response.set_cookie(
        key=SESSION_COOKIE,
        value=grant.session_token,
        max_age=24 * 60 * 60,
        secure=secure,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.set_cookie(
        key=CSRF_COOKIE,
        value=grant.csrf_token,
        max_age=24 * 60 * 60,
        secure=secure,
        httponly=False,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"


def _grant_response(grant: SessionGrant) -> SessionResponse:
    return SessionResponse(
        user=AuthenticatedUserResponse(user_id=grant.user_id, username=grant.username),
        csrf_token=grant.csrf_token,
        idle_expires_at=grant.idle_expires_at.isoformat(),
        absolute_expires_at=grant.absolute_expires_at.isoformat(),
    )


router = APIRouter(prefix="/v1/auth", tags=["authentication"])


@router.post(
    "/register",
    response_model=SessionResponse,
    status_code=status.HTTP_201_CREATED,
    responses=problem_openapi_responses(403, 409, 422, 429),
)
def register(
    payload: RegistrationRequest,
    request: Request,
    response: Response,
    database: Annotated[Session, Depends(get_database)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> SessionResponse:
    require_same_origin(request)
    limiter: LoginRateLimiter = request.app.state.login_limiter
    now = auth.now
    limiter.check(f"client:{request.client.host if request.client else 'unknown'}", now=now)
    grant = auth.register(
        database,
        username=payload.username,
        password=payload.password.get_secret_value(),
    )
    _set_session_cookie(response, grant, secure=request.app.state.settings.secure_cookies)
    return _grant_response(grant)


@router.post(
    "/login",
    response_model=SessionResponse,
    responses=problem_openapi_responses(401, 403, 422, 429),
)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    database: Annotated[Session, Depends(get_database)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
    prior_session_token: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> SessionResponse:
    require_same_origin(request)
    limiter: LoginRateLimiter = request.app.state.login_limiter
    now = auth.now
    client = request.client.host if request.client else "unknown"
    limiter.check(f"client:{client}", now=now)
    limiter.check(f"principal:{payload.username}", now=now)
    grant = auth.login(
        database,
        username=payload.username,
        password=payload.password.get_secret_value(),
        prior_session_token=prior_session_token,
    )
    _set_session_cookie(response, grant, secure=request.app.state.settings.secure_cookies)
    return _grant_response(grant)


@router.get(
    "/session",
    response_model=SessionResponse,
    responses=problem_openapi_responses(401),
)
def session_status(
    authenticated: Annotated[AuthenticatedSession, Depends(get_authenticated_session)],
    response: Response,
) -> SessionResponse:
    response.headers["Cache-Control"] = "no-store"
    return SessionResponse(
        user=AuthenticatedUserResponse(
            user_id=authenticated.user_id,
            username=authenticated.username,
        ),
        idle_expires_at=authenticated.idle_expires_at.isoformat(),
        absolute_expires_at=authenticated.absolute_expires_at.isoformat(),
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problem_openapi_responses(401, 403),
)
def logout(
    response: Response,
    authenticated: Annotated[AuthenticatedSession, Depends(require_csrf_session)],
    database: Annotated[Session, Depends(get_database)],
    auth: Annotated[AuthService, Depends(get_auth_service)],
) -> None:
    auth.logout(database, authenticated=authenticated)
    response.delete_cookie(
        key=SESSION_COOKIE,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    response.delete_cookie(
        key=CSRF_COOKIE,
        secure=True,
        httponly=False,
        samesite="strict",
        path="/",
    )
    response.headers["Cache-Control"] = "no-store"
