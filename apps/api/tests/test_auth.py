from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from ratereplay_api.auth import AuthService
from ratereplay_api.config import AppSettings
from ratereplay_api.main import create_app
from ratereplay_persistence.models import SessionRecord, UserRecord
from sqlalchemy import select

ORIGIN = "https://app.ratereplay.test"
PASSWORD = "correct horse battery staple"


@dataclass
class MutableClock:
    value: datetime = datetime(2026, 8, 13, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, delta: timedelta) -> None:
        self.value += delta


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def test_app(clock: MutableClock) -> FastAPI:
    settings = AppSettings.for_test()
    app = create_app(settings)
    app.state.auth_service = AuthService(settings.session_key, clock=clock)
    return app


@pytest.fixture
async def client(test_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as test_client:
        yield test_client


async def register(client: httpx.AsyncClient, *, username: str = "owner_one") -> dict[str, Any]:
    response = await client.post(
        "/v1/auth/register",
        headers={"Origin": ORIGIN},
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


@pytest.mark.anyio
async def test_registration_canonicalizes_username_and_sets_hardened_cookie(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    body = await register(client, username="Owner_One")
    assert body["schema_version"] == "auth-session-v1"
    assert body["user"]["username"] == "owner_one"
    assert isinstance(body["csrf_token"], str)
    cookie = client.cookies.get("__Host-ratereplay_session")
    assert cookie is not None

    response = await client.get("/v1/auth/session")
    assert response.status_code == 200
    assert response.json()["csrf_token"] is None
    assert response.headers["cache-control"] == "no-store"

    app = cast(Any, test_app)
    with app.state.session_factory() as database:
        user = database.scalar(select(UserRecord))
        assert user is not None
        assert user.username_canonical == "owner_one"
        assert PASSWORD not in user.password_hash
        assert user.password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=4$")


@pytest.mark.anyio
async def test_cookie_attributes_are_host_only_secure_http_only_and_strict(
    client: httpx.AsyncClient,
) -> None:
    response = await client.post(
        "/v1/auth/register",
        headers={"Origin": ORIGIN},
        json={"username": "cookie_owner", "password": PASSWORD},
    )
    cookie = response.headers["set-cookie"]
    assert "__Host-ratereplay_session=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie
    assert "Domain=" not in cookie


@pytest.mark.anyio
async def test_duplicate_username_and_validation_use_safe_problem_schema(
    client: httpx.AsyncClient,
) -> None:
    await register(client)
    duplicate = await client.post(
        "/v1/auth/register",
        headers={"Origin": ORIGIN},
        json={"username": "OWNER_ONE", "password": PASSWORD},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "USERNAME_UNAVAILABLE"
    assert "owner_one" not in duplicate.text

    invalid = await client.post(
        "/v1/auth/register",
        headers={"Origin": ORIGIN},
        json={"username": "invalid-name", "password": PASSWORD},
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "INVALID_USERNAME"


@pytest.mark.anyio
async def test_login_rotates_existing_session_and_uses_generic_failure(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    await register(client)
    old_token = client.cookies.get("__Host-ratereplay_session")
    login = await client.post(
        "/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"username": "owner_one", "password": PASSWORD},
    )
    assert login.status_code == 200
    new_token = client.cookies.get("__Host-ratereplay_session")
    assert old_token is not None
    assert new_token != old_token

    stale_transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=stale_transport, base_url=ORIGIN) as stale:
        stale.cookies.set("__Host-ratereplay_session", old_token)
        assert (await stale.get("/v1/auth/session")).status_code == 401
    assert (await client.get("/v1/auth/session")).status_code == 200

    wrong = await client.post(
        "/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"username": "missing_user", "password": "incorrect password value"},
    )
    assert wrong.status_code == 401
    assert wrong.json()["code"] == "INVALID_CREDENTIALS"
    assert "missing_user" not in wrong.text


@pytest.mark.anyio
async def test_logout_requires_origin_and_csrf_then_revokes_server_session(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    csrf = (await register(client))["csrf_token"]
    assert (await client.post("/v1/auth/logout", headers={"Origin": ORIGIN})).status_code == 403
    assert (
        await client.post(
            "/v1/auth/logout",
            headers={"Origin": "https://attacker.invalid", "X-CSRF-Token": str(csrf)},
        )
    ).status_code == 403
    accepted = await client.post(
        "/v1/auth/logout",
        headers={"Origin": ORIGIN, "X-CSRF-Token": str(csrf)},
    )
    assert accepted.status_code == 204
    assert (await client.get("/v1/auth/session")).status_code == 401
    app = cast(Any, test_app)
    with app.state.session_factory() as database:
        record = database.scalar(select(SessionRecord))
        assert record is not None and record.revoked_at is not None


@pytest.mark.anyio
async def test_idle_expiry_is_enforced_server_side(
    client: httpx.AsyncClient, clock: MutableClock
) -> None:
    await register(client)
    clock.advance(timedelta(minutes=30))
    response = await client.get("/v1/auth/session")
    assert response.status_code == 401
    assert response.json()["code"] == "AUTHENTICATION_REQUIRED"


@pytest.mark.anyio
async def test_absolute_expiry_cannot_be_extended_by_activity(
    client: httpx.AsyncClient, clock: MutableClock
) -> None:
    await register(client)
    for _ in range(47):
        clock.advance(timedelta(minutes=30) - timedelta(seconds=1))
        assert (await client.get("/v1/auth/session")).status_code == 200
    clock.advance(timedelta(minutes=31))
    assert (await client.get("/v1/auth/session")).status_code == 401


@pytest.mark.anyio
async def test_rate_limit_bounds_password_hash_work(client: httpx.AsyncClient) -> None:
    for _ in range(5):
        response = await client.post(
            "/v1/auth/login",
            headers={"Origin": ORIGIN},
            json={"username": "unknown_owner", "password": PASSWORD},
        )
        assert response.status_code == 401
    limited = await client.post(
        "/v1/auth/login",
        headers={"Origin": ORIGIN},
        json={"username": "unknown_owner", "password": PASSWORD},
    )
    assert limited.status_code == 429
    assert limited.json()["code"] == "AUTH_RATE_LIMITED"


@pytest.mark.anyio
async def test_cross_origin_registration_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/register",
        headers={"Origin": "https://attacker.invalid"},
        json={"username": "owner_one", "password": PASSWORD},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "ORIGIN_REJECTED"


def test_production_configuration_requires_external_session_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RATEREPLAY_ENV", "production")
    monkeypatch.delenv("RATEREPLAY_SESSION_SECRET_FILE", raising=False)
    with pytest.raises(RuntimeError, match="RATEREPLAY_SESSION_SECRET_FILE"):
        AppSettings.from_environment()
