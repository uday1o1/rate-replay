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
from ratereplay_persistence.deletion_sweep import DeletionSweepService
from ratereplay_persistence.models import (
    DeletionAuditRecord,
    DeletionControlOperationRecord,
    DeletionReceiptRecord,
    ImportReadingRecord,
    ImportRecord,
    ProfileVersionRecord,
    SessionRecord,
    UserRecord,
)
from ratereplay_worker.deletion_worker import DeletionWorker
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

    app = cast(Any, test_app)
    worker = DeletionWorker(
        worker_id="api-test-deletion-worker",
        jobs=app.state.job_service,
        sweeps=DeletionSweepService(
            app.state.session_factory,
            app.state.object_store,
            app.state.deletion_ledger,
        ),
    )
    worker_now = app.state.auth_service.now
    assert worker.run_once(now=worker_now)

    client.cookies.clear()
    receipt = await client.get(
        f"/v1/deletions/{deletion_id}",
        headers={"X-Deletion-Receipt-Secret": _encoded(SECRET)},
    )
    assert receipt.status_code == 200, receipt.text
    assert receipt.headers["cache-control"] == "no-store"
    receipt_body = receipt.json()
    assert receipt_body["schema_version"] == "deletion-status-v1"
    assert receipt_body["deletion_id"] == deletion_id
    assert receipt_body["status"] == "DELETED"
    assert receipt_body["artifact_counts"]["sessions"] == 1
    assert receipt_body["completed_at"] == worker_now.isoformat()

    assert tuple(event.phase for event in app.state.deletion_ledger.chain(deletion_id)) == (
        "PREPARED",
        "REQUESTED",
        "COMPLETED",
    )
    with app.state.session_factory() as database:
        user = database.scalar(select(UserRecord))
        session = database.scalar(select(SessionRecord))
        control = database.get(DeletionControlOperationRecord, deletion_id)
        stored_receipt = database.get(DeletionReceiptRecord, deletion_id)
        audit = database.get(DeletionAuditRecord, deletion_id)
        assert user is None and session is None and control is None
        assert stored_receipt is not None and stored_receipt.status == "DELETED"
        assert audit is not None and audit.status_code == "VERIFIED_COMPLETE"
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


@pytest.mark.anyio
async def test_profile_then_import_deletion_use_durable_receipts_and_scoped_sweeps(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    csrf = await _register(client)
    installed = await client.post(
        "/v1/imports/built-in-simulated-profile",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "resource-delete-fixture",
        },
    )
    assert installed.status_code == 201, installed.text
    profile_id = cast(str, installed.json()["profile"]["profile_version_id"])
    import_id = cast(str, installed.json()["profile"]["import_id"])

    app = cast(Any, test_app)
    worker = DeletionWorker(
        worker_id="api-resource-deletion-worker",
        jobs=app.state.job_service,
        sweeps=DeletionSweepService(
            app.state.session_factory,
            app.state.object_store,
            app.state.deletion_ledger,
        ),
    )
    profile_headers = _intent_headers(csrf, key="delete-profile")
    accepted_profile = await client.delete(
        f"/v1/profiles/{profile_id}",
        headers=profile_headers,
    )
    assert accepted_profile.status_code == 202, accepted_profile.text
    profile_deletion_id = cast(str, accepted_profile.json()["deletion_id"])
    repeated_profile = await client.delete(
        f"/v1/profiles/{profile_id}",
        headers=profile_headers,
    )
    assert repeated_profile.status_code == 202
    assert repeated_profile.json()["deletion_id"] == profile_deletion_id
    assert worker.run_once(now=app.state.auth_service.now)

    profile_receipt = await client.get(
        f"/v1/deletions/{profile_deletion_id}",
        headers={"X-Deletion-Receipt-Secret": _encoded(SECRET)},
    )
    assert profile_receipt.status_code == 200
    assert profile_receipt.json()["status"] == "DELETED"
    assert (await client.get(f"/v1/profiles/{profile_id}")).status_code == 404
    remaining_import = await client.get(f"/v1/imports/{import_id}")
    assert remaining_import.status_code == 200
    assert remaining_import.json()["state"] == "READY"

    import_headers = _intent_headers(csrf, key="delete-import")
    accepted_import = await client.delete(
        f"/v1/imports/{import_id}",
        headers=import_headers,
    )
    assert accepted_import.status_code == 202, accepted_import.text
    import_deletion_id = cast(str, accepted_import.json()["deletion_id"])
    assert import_deletion_id != profile_deletion_id
    assert worker.run_once(now=app.state.auth_service.now)

    import_receipt = await client.get(
        f"/v1/deletions/{import_deletion_id}",
        headers={"X-Deletion-Receipt-Secret": _encoded(SECRET)},
    )
    assert import_receipt.status_code == 200
    assert import_receipt.json()["status"] == "DELETED"
    assert (await client.get(f"/v1/imports/{import_id}")).status_code == 404
    with app.state.session_factory() as database:
        assert database.get(ProfileVersionRecord, profile_id) is None
        assert database.get(ImportRecord, import_id) is None
        assert not database.scalars(
            select(ImportReadingRecord).where(ImportReadingRecord.import_id == import_id)
        ).first()
        profile_audit = database.get(DeletionAuditRecord, profile_deletion_id)
        import_audit = database.get(DeletionAuditRecord, import_deletion_id)
        assert profile_audit is not None and profile_audit.target_kind == "PROFILE"
        assert import_audit is not None and import_audit.target_kind == "IMPORT"
    assert tuple(event.phase for event in app.state.deletion_ledger.chain(profile_deletion_id)) == (
        "PREPARED",
        "REQUESTED",
        "COMPLETED",
    )
    assert tuple(event.phase for event in app.state.deletion_ledger.chain(import_deletion_id)) == (
        "PREPARED",
        "REQUESTED",
        "COMPLETED",
    )


@pytest.mark.anyio
async def test_account_deletion_fences_and_completes_pending_child_deletion(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    csrf = await _register(client)
    installed = await client.post(
        "/v1/imports/built-in-simulated-profile",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "subsumed-delete-fixture",
        },
    )
    profile_id = cast(str, installed.json()["profile"]["profile_version_id"])
    child = await client.delete(
        f"/v1/profiles/{profile_id}",
        headers=_intent_headers(csrf, key="pending-profile-delete"),
    )
    assert child.status_code == 202, child.text
    child_deletion_id = cast(str, child.json()["deletion_id"])

    account_intent = await client.post(
        "/v1/account/deletion-intents",
        headers=_intent_headers(csrf, key="parent-account-delete"),
    )
    assert account_intent.status_code == 201, account_intent.text
    account_deletion_id = cast(str, account_intent.json()["deletion_id"])
    account = await client.request(
        "DELETE",
        "/v1/account",
        headers=_intent_headers(csrf, key="parent-account-delete"),
        json={"deletion_id": account_deletion_id},
    )
    assert account.status_code == 202, account.text

    app = cast(Any, test_app)
    worker = DeletionWorker(
        worker_id="api-parent-deletion-worker",
        jobs=app.state.job_service,
        sweeps=DeletionSweepService(
            app.state.session_factory,
            app.state.object_store,
            app.state.deletion_ledger,
        ),
    )
    assert worker.run_once(now=app.state.auth_service.now)
    client.cookies.clear()
    for deletion_id in (child_deletion_id, account_deletion_id):
        receipt = await client.get(
            f"/v1/deletions/{deletion_id}",
            headers={"X-Deletion-Receipt-Secret": _encoded(SECRET)},
        )
        assert receipt.status_code == 200, receipt.text
        assert receipt.json()["status"] == "DELETED"
    with app.state.session_factory() as database:
        child_audit = database.get(DeletionAuditRecord, child_deletion_id)
        assert child_audit is not None
        assert child_audit.status_code == "SUBSUMED_BY_PARENT_DELETION"
        assert database.scalar(select(UserRecord)) is None
