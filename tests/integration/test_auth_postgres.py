from __future__ import annotations

import os
import secrets

import httpx
import pytest
from ratereplay_api.config import AppSettings
from ratereplay_api.main import create_app
from ratereplay_persistence.models import UserRecord
from sqlalchemy import delete

ORIGIN = "https://app.ratereplay.test"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.postgres
async def test_register_login_logout_against_migrated_postgres() -> None:
    database_url = os.getenv("RATEREPLAY_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("RATEREPLAY_TEST_DATABASE_URL is not configured")
    settings = AppSettings.for_test(database_url=database_url)
    app = create_app(settings)
    username = f"integration_{secrets.token_hex(4)}"
    password = "integration password only"

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            registered = await client.post(
                "/v1/auth/register",
                headers={"Origin": ORIGIN},
                json={"username": username, "password": password},
            )
            assert registered.status_code == 201, registered.text
            csrf = registered.json()["csrf_token"]
            old_session = client.cookies.get("__Host-ratereplay_session")

            logged_in = await client.post(
                "/v1/auth/login",
                headers={"Origin": ORIGIN},
                json={"username": username, "password": password},
            )
            assert logged_in.status_code == 200, logged_in.text
            assert client.cookies.get("__Host-ratereplay_session") != old_session
            csrf = logged_in.json()["csrf_token"]

            logged_out = await client.post(
                "/v1/auth/logout",
                headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
            )
            assert logged_out.status_code == 204, logged_out.text
            assert (await client.get("/v1/auth/session")).status_code == 401
    finally:
        with app.state.session_factory.begin() as database:
            database.execute(delete(UserRecord).where(UserRecord.username_canonical == username))
        app.state.engine.dispose()
