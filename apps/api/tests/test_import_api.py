from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from ratereplay_api.config import AppSettings
from ratereplay_api.main import create_app
from ratereplay_persistence.imports import ImportService
from ratereplay_persistence.jobs import JobService
from ratereplay_persistence.models import (
    ImportReadingRecord,
    ImportRecord,
    JobRecord,
    ProfileVersionRecord,
    RawObjectRecord,
)
from ratereplay_worker.import_worker import ImportWorker
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "data/fixtures/espi/independent-pacific-hourly.xml"
SCHEMA = ROOT / "third_party/espi-schema/espi-4.0.xsd"
ORIGIN = "https://app.ratereplay.test"
PASSWORD = "correct horse battery staple"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def test_app(tmp_path: Path) -> FastAPI:
    return create_app(
        AppSettings.for_test(
            object_store_root=tmp_path / "objects",
            espi_schema_path=SCHEMA,
        )
    )


@pytest.fixture
async def client(test_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as value:
        yield value


async def register(client: httpx.AsyncClient, username: str) -> str:
    response = await client.post(
        "/v1/auth/register",
        headers={"Origin": ORIGIN},
        json={"username": username, "password": PASSWORD},
    )
    assert response.status_code == 201, response.text
    return cast(str, response.json()["csrf_token"])


@pytest.mark.anyio
async def test_real_upload_quality_confirmation_and_profile_path(
    client: httpx.AsyncClient,
    test_app: FastAPI,
    caplog: pytest.LogCaptureFixture,
) -> None:
    unauthenticated = await client.post(
        "/v1/imports",
        headers={"Origin": ORIGIN, "Idempotency-Key": "api-import-one"},
        data={"adapter": "ESPI_XML"},
        files={"file": ("private-household.xml", FIXTURE.read_bytes(), "application/xml")},
    )
    assert unauthenticated.status_code == 401

    csrf = await register(client, "api_owner")
    missing_csrf = await client.post(
        "/v1/imports",
        headers={"Origin": ORIGIN, "Idempotency-Key": "api-import-one"},
        data={"adapter": "ESPI_XML"},
        files={"file": ("private-household.xml", FIXTURE.read_bytes(), "application/xml")},
    )
    assert missing_csrf.status_code == 403

    submitted = await client.post(
        "/v1/imports",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "api-import-one",
        },
        data={"adapter": "ESPI_XML"},
        files={"file": ("private-household.xml", FIXTURE.read_bytes(), "application/xml")},
    )
    assert submitted.status_code == 202, submitted.text
    operation = submitted.json()
    assert operation["state_url"] == f"/v1/imports/{operation['import_id']}"

    app_state = cast(Any, test_app.state)
    worker = ImportWorker(
        worker_id="api-test-worker",
        jobs=cast(JobService, app_state.job_service),
        imports=cast(ImportService, app_state.import_service),
        espi_schema_path=SCHEMA,
    )
    assert worker.run_once(now=datetime.now(UTC))

    quality = await client.get(operation["state_url"])
    assert quality.status_code == 200, quality.text
    quality_body = quality.json()
    assert quality_body["state"] == "READY"
    assert quality_body["job_state"] == "SUCCEEDED"
    assert quality_body["reading_count"] == 362
    assert quality_body["findings"] == []
    assert quality_body["coverage_start_utc_ns"] < quality_body["coverage_end_utc_ns"]

    confirmed = await client.post(
        f"/v1/imports/{operation['import_id']}/confirm",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={
            "billing_period_start_utc_ns": quality_body["coverage_start_utc_ns"],
            "billing_period_end_utc_ns": quality_body["coverage_end_utc_ns"],
            "acknowledged_warning_ids": [],
            "pge_service_attested": True,
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    profile = confirmed.json()
    assert len(profile["content_hash"]) == 64
    fetched = await client.get(f"/v1/profiles/{profile['profile_version_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == profile
    listed = await client.get("/v1/profiles", params={"page_size": 1})
    assert listed.status_code == 200
    assert listed.json() == {
        "schema_version": "profile-list-v1",
        "items": [profile],
        "next_cursor": None,
    }

    with app_state.session_factory() as database:
        imported = database.scalar(
            select(ImportRecord).where(ImportRecord.id == operation["import_id"])
        )
        assert imported is not None
        assert "private-household.xml" not in repr(imported.__dict__)
    assert "private-household.xml" not in caplog.text
    assert "610314" not in caplog.text


@pytest.mark.anyio
async def test_built_in_simulated_profile_is_locked_idempotent_and_owner_scoped(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    route = "/v1/imports/built-in-simulated-profile"
    unauthenticated = await client.post(
        route,
        headers={"Origin": ORIGIN, "Idempotency-Key": "demo-profile-one"},
    )
    assert unauthenticated.status_code == 401
    csrf = await register(client, "simulated_owner")
    missing_csrf = await client.post(
        route,
        headers={"Origin": ORIGIN, "Idempotency-Key": "demo-profile-one"},
    )
    assert missing_csrf.status_code == 403
    created = await client.post(
        route,
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "demo-profile-one",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["simulated"] is True
    assert body["label"].startswith("SIMULATED ")
    assert body["source_artifact_sha256"] == (
        "47b449f47039960cde24666a5ed2723781b7773d624dbdd2b74de78e02da19ce"
    )
    assert body["repeated"] is False
    assert body["profile"]["interval_resolution_seconds"] == 900

    repeated = await client.post(
        route,
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "demo-profile-one",
        },
    )
    semantic_reuse = await client.post(
        route,
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "demo-profile-two",
        },
    )
    assert repeated.status_code == semantic_reuse.status_code == 201
    assert repeated.json()["repeated"] is True
    assert semantic_reuse.json()["repeated"] is True
    assert repeated.json()["profile"] == body["profile"]
    assert semantic_reuse.json()["profile"] == body["profile"]

    second_transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=second_transport, base_url=ORIGIN) as other:
        other_csrf = await register(other, "simulated_owner_two")
        other_created = await other.post(
            route,
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": other_csrf,
                "Idempotency-Key": "demo-profile-one",
            },
        )
        assert other_created.status_code == 201
        assert (
            other_created.json()["profile"]["profile_version_id"]
            != (body["profile"]["profile_version_id"])
        )
        assert other_created.json()["profile"]["content_hash"] == (body["profile"]["content_hash"])

    app_state = cast(Any, test_app.state)
    with app_state.session_factory() as database:
        profile = database.get(
            ProfileVersionRecord,
            body["profile"]["profile_version_id"],
        )
        assert profile is not None
        assert len(profile.canonical_content) > 0
        assert database.scalar(select(func.count(ImportRecord.id))) == 2
        assert database.scalar(select(func.count(ImportReadingRecord.id))) == 5_952
        assert database.scalar(select(func.count(RawObjectRecord.id))) == 0
        assert database.scalar(select(func.count(JobRecord.id))) == 0


@pytest.mark.anyio
async def test_api_idempotency_and_cross_owner_authorization(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    csrf = await register(client, "first_owner")

    async def post_import() -> httpx.Response:
        return await client.post(
            "/v1/imports",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": "same-import-key",
            },
            data={"adapter": "ESPI_XML"},
            files={"file": ("usage.xml", FIXTURE.read_bytes(), "application/xml")},
        )

    first = await post_import()
    second = await post_import()
    assert first.status_code == second.status_code == 202
    assert second.json()["repeated"] is True
    assert second.json()["import_id"] == first.json()["import_id"]
    operation = first.json()
    app_state = cast(Any, test_app.state)
    worker = ImportWorker(
        worker_id="authorization-worker",
        jobs=cast(JobService, app_state.job_service),
        imports=cast(ImportService, app_state.import_service),
        espi_schema_path=SCHEMA,
    )
    assert worker.run_once(now=datetime.now(UTC))
    quality = (await client.get(operation["state_url"])).json()
    confirmed = await client.post(
        f"/v1/imports/{operation['import_id']}/confirm",
        headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
        json={
            "billing_period_start_utc_ns": quality["coverage_start_utc_ns"],
            "billing_period_end_utc_ns": quality["coverage_end_utc_ns"],
            "acknowledged_warning_ids": [],
            "pge_service_attested": True,
        },
    )
    assert confirmed.status_code == 200
    profile_id = confirmed.json()["profile_version_id"]

    second_transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=second_transport, base_url=ORIGIN) as other:
        other_csrf = await register(other, "second_owner")
        denied = await other.get(f"/v1/imports/{first.json()['import_id']}")
        assert denied.status_code == 404
        assert denied.json()["code"] == "IMPORT_NOT_FOUND"
        denied_confirmation = await other.post(
            f"/v1/imports/{first.json()['import_id']}/confirm",
            headers={"Origin": ORIGIN, "X-CSRF-Token": other_csrf},
            json={
                "billing_period_start_utc_ns": quality["coverage_start_utc_ns"],
                "billing_period_end_utc_ns": quality["coverage_end_utc_ns"],
                "acknowledged_warning_ids": [],
                "pge_service_attested": True,
            },
        )
        assert denied_confirmation.status_code == 404
        denied_profile = await other.get(f"/v1/profiles/{profile_id}")
        assert denied_profile.status_code == 404
        assert denied_profile.json()["code"] == "PROFILE_NOT_FOUND"
        listed = await other.get("/v1/profiles")
        assert listed.status_code == 200
        assert listed.json()["items"] == []


@pytest.mark.anyio
async def test_upload_rate_limit_and_invalid_profile_cursor_are_safe(
    client: httpx.AsyncClient,
) -> None:
    csrf = await register(client, "limited_owner")
    for index in range(10):
        response = await client.post(
            "/v1/imports",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": f"limited-import-{index}",
            },
            data={"adapter": "ESPI_XML"},
            files={"file": ("usage.xml", FIXTURE.read_bytes(), "application/xml")},
        )
        assert response.status_code == 202
    limited = await client.post(
        "/v1/imports",
        headers={
            "Origin": ORIGIN,
            "X-CSRF-Token": csrf,
            "Idempotency-Key": "limited-import-overflow",
        },
        data={"adapter": "ESPI_XML"},
        files={"file": ("usage.xml", FIXTURE.read_bytes(), "application/xml")},
    )
    assert limited.status_code == 429
    assert limited.json()["code"] == "UPLOAD_RATE_LIMITED"
    invalid_cursor = await client.get("/v1/profiles", params={"cursor": "not-valid"})
    assert invalid_cursor.status_code == 422
    assert invalid_cursor.json()["code"] == "INVALID_CURSOR"


@pytest.mark.anyio
async def test_profile_list_uses_owner_scoped_signed_cursor(
    client: httpx.AsyncClient,
    test_app: FastAPI,
) -> None:
    csrf = await register(client, "cursor_owner")
    app_state = cast(Any, test_app.state)
    worker = ImportWorker(
        worker_id="cursor-worker",
        jobs=cast(JobService, app_state.job_service),
        imports=cast(ImportService, app_state.import_service),
        espi_schema_path=SCHEMA,
    )
    profile_ids: set[str] = set()
    for index, payload in enumerate(
        (
            FIXTURE.read_bytes(),
            FIXTURE.read_bytes().replace(b"<value>703</value>", b"<value>704</value>"),
        )
    ):
        submitted = await client.post(
            "/v1/imports",
            headers={
                "Origin": ORIGIN,
                "X-CSRF-Token": csrf,
                "Idempotency-Key": f"cursor-import-{index}",
            },
            data={"adapter": "ESPI_XML"},
            files={"file": ("usage.xml", payload, "application/xml")},
        )
        assert submitted.status_code == 202
        operation = submitted.json()
        assert worker.run_once(now=datetime.now(UTC))
        quality = (await client.get(operation["state_url"])).json()
        confirmed = await client.post(
            f"/v1/imports/{operation['import_id']}/confirm",
            headers={"Origin": ORIGIN, "X-CSRF-Token": csrf},
            json={
                "billing_period_start_utc_ns": quality["coverage_start_utc_ns"],
                "billing_period_end_utc_ns": quality["coverage_end_utc_ns"],
                "acknowledged_warning_ids": [],
                "pge_service_attested": True,
            },
        )
        assert confirmed.status_code == 200
        profile_ids.add(cast(str, confirmed.json()["profile_version_id"]))

    first_page = await client.get("/v1/profiles", params={"page_size": 1})
    assert first_page.status_code == 200
    cursor = cast(str, first_page.json()["next_cursor"])
    assert cursor
    second_page = await client.get(
        "/v1/profiles",
        params={"page_size": 1, "cursor": cursor},
    )
    assert second_page.status_code == 200
    assert second_page.json()["next_cursor"] is None
    listed_ids = {
        first_page.json()["items"][0]["profile_version_id"],
        second_page.json()["items"][0]["profile_version_id"],
    }
    assert listed_ids == profile_ids
    tampered = await client.get(
        "/v1/profiles",
        params={"cursor": cursor[:-1] + ("A" if cursor[-1] != "A" else "B")},
    )
    assert tampered.status_code == 422
    assert tampered.json()["code"] == "INVALID_CURSOR"


def test_openapi_upload_contract_never_requests_utility_credentials(test_app: FastAPI) -> None:
    schema = test_app.openapi()
    operation = schema["paths"]["/v1/imports"]["post"]
    serialized = str(operation).lower()
    assert "utility_password" not in serialized
    assert "utility_username" not in serialized
    multipart = operation["requestBody"]["content"]["multipart/form-data"]["schema"]
    reference = multipart["$ref"].rsplit("/", 1)[1]
    body_schema = schema["components"]["schemas"][reference]
    assert body_schema["properties"]["file"]["contentMediaType"] == "application/octet-stream"
    assert set(body_schema["properties"]) == {"adapter", "file"}
