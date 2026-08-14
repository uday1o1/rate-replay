from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from ratereplay_api.config import AppSettings
from ratereplay_api.main import create_app
from ratereplay_persistence.models import (
    DeletionControlOperationRecord,
    DeletionReceiptRecord,
    SessionRecord,
    UserRecord,
)
from sqlalchemy import select

ORIGIN = "https://app.ratereplay.test"
PASSWORD = "correct horse battery staple"
SECRET = b"d" * 32
OTHER_SECRET = b"x" * 32


def _encoded(secret: bytes) -> str:
    return base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def test_app(tmp_path: Path) -> FastAPI:
    return create_app(
        AppSettings.for_test(
            object_store_root=tmp_path / "objects",
            deletion_ledger_root=tmp_path / "deletion-ledger",
        )
    )


@pytest.fixture
async def client(test_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as test_client:
        yield test_client


async def _register(client: httpx.AsyncClient) -> str:
    response = await client.post(
        "/v1/auth/register",
        headers={"Origin": ORIGIN},
        json={"username": "deletion_owner", "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return cast(str, response.json()["csrf_token"])


def _intent_headers(
    csrf: str, *, secret: bytes = SECRET, key: str = "delete-key"
) -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "X-CSRF-Token": csrf,
        "Idempotency-Key": key,
        "X-Deletion-Receipt-Secret": _encoded(secret),
    }


@pytest.mark.anyio
async def test_user_path_prepares_deletes_and_polls_without_session(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    csrf = await _register(client)
    intent = await client.post(
        "/v1/account/deletion-intents",
        headers=_intent_headers(csrf),
    )
    assert intent.status_code == 201, intent.text
    assert intent.headers["cache-control"] == "no-store"
    deletion_id = cast(str, intent.json()["deletion_id"])
    repeated = await client.post(
        "/v1/account/deletion-intents",
        headers=_intent_headers(csrf),
    )
    assert repeated.status_code == 201
    assert repeated.json()["deletion_id"] == deletion_id

    accepted = await client.request(
        "DELETE",
        "/v1/account",
        headers=_intent_headers(csrf),
        json={"deletion_id": deletion_id},
    )
    assert accepted.status_code == 202, accepted.text
    assert accepted.headers["cache-control"] == "no-store"
    assert accepted.json()["status"] == "DELETING"
    assert (await client.get("/v1/auth/session")).status_code == 401

    client.cookies.clear()
    receipt = await client.get(
        f"/v1/deletions/{deletion_id}",
        headers={"X-Deletion-Receipt-Secret": _encoded(SECRET)},
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.headers["cache-control"] == "no-store"
    assert receipt.json() == {
        "schema_version": "deletion-status-v1",
        "deletion_id": deletion_id,
        "status": "DELETING",
        "artifact_counts": {},
        "completed_at": None,
    }

    app = cast(Any, test_app)
    assert tuple(event.phase for event in app.state.deletion_ledger.chain(deletion_id)) == (
        "PREPARED",
        "REQUESTED",
    )
    with app.state.session_factory() as database:
        user = database.scalar(select(UserRecord))
        session = database.scalar(select(SessionRecord))
        control = database.get(DeletionControlOperationRecord, deletion_id)
        stored_receipt = database.get(DeletionReceiptRecord, deletion_id)
        assert user is not None and user.lifecycle_state == "DELETING"
        assert session is not None and session.revoked_at is not None
        assert control is not None and control.deletion_job_id is not None
        assert stored_receipt is not None
        assert _encoded(SECRET) not in stored_receipt.receipt_verifier


@pytest.mark.anyio
async def test_deletion_headers_are_csrf_and_receipt_protected(
    client: httpx.AsyncClient,
) -> None:
    csrf = await _register(client)
    missing_csrf = await client.post(
        "/v1/account/deletion-intents",
        headers={
            "Origin": ORIGIN,
            "Idempotency-Key": "missing-csrf",
            "X-Deletion-Receipt-Secret": _encoded(SECRET),
        },
    )
    assert (missing_csrf.status_code, missing_csrf.json()["code"]) == (403, "CSRF_REJECTED")

    malformed = await client.post(
        "/v1/account/deletion-intents",
        headers={
            **_intent_headers(csrf, key="malformed"),
            "X-Deletion-Receipt-Secret": "not-base64",
        },
    )
    assert (malformed.status_code, malformed.json()["code"]) == (
        404,
        "INVALID_DELETION_PROOF",
    )


@pytest.mark.anyio
async def test_wrong_receipt_cannot_recover_deletion_status(
    client: httpx.AsyncClient,
) -> None:
    csrf = await _register(client)
    intent = await client.post(
        "/v1/account/deletion-intents",
        headers=_intent_headers(csrf),
    )
    deletion_id = cast(str, intent.json()["deletion_id"])
    denied = await client.get(
        f"/v1/deletions/{deletion_id}",
        headers={"X-Deletion-Receipt-Secret": _encoded(OTHER_SECRET)},
    )
    assert (denied.status_code, denied.json()["code"]) == (
        404,
        "INVALID_DELETION_PROOF",
    )
    assert deletion_id not in denied.text
