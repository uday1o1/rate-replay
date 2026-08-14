from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from ratereplay_api.abuse import SlidingWindowRateLimiter
from ratereplay_api.config import AppSettings
from ratereplay_api.main import create_app
from ratereplay_api.problems import ApiProblem
from ratereplay_persistence.object_store import ObjectStoreError
from sqlalchemy.exc import OperationalError


def _limiter(
    *,
    limit: int = 2,
    maximum_identifiers: int = 2,
    on_reject: Callable[[str], None] | None = None,
) -> SlidingWindowRateLimiter:
    return SlidingWindowRateLimiter(
        b"bounded-rate-limit-test-key-v1!!",
        limit=limit,
        window=timedelta(minutes=1),
        code="TEST_RATE_LIMITED",
        message="Request budget exhausted.",
        scope="READ",
        maximum_identifiers=maximum_identifiers,
        on_reject=on_reject,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_rotating_identifiers_share_a_bounded_overflow_budget() -> None:
    limiter = _limiter()
    now = datetime(2026, 8, 14, tzinfo=UTC)

    limiter.check("client-a", now=now)
    limiter.check("client-b", now=now)
    limiter.check("client-c", now=now)
    with pytest.raises(ApiProblem) as captured:
        limiter.check("client-d", now=now)

    assert captured.value.status_code == 429
    assert captured.value.headers == {"Retry-After": "60"}
    assert limiter.retained_identifier_count == 2


def test_expired_buckets_are_reclaimed_without_weakening_retry_after() -> None:
    limiter = _limiter(limit=1, maximum_identifiers=1)
    now = datetime(2026, 8, 14, tzinfo=UTC)

    limiter.check("first", now=now)
    with pytest.raises(ApiProblem) as captured:
        limiter.check("second", now=now + timedelta(seconds=17))
    assert captured.value.headers == {"Retry-After": "43"}

    limiter.check("third", now=now + timedelta(seconds=60))
    assert limiter.retained_identifier_count == 1


@pytest.mark.anyio
async def test_api_budget_returns_safe_retry_contract_and_metric() -> None:
    application = create_app(AppSettings.for_test())
    application.state.read_limiter = _limiter(
        limit=1,
        maximum_identifiers=8,
        on_reject=application.state.telemetry.record_rate_limit_rejection,
    )
    transport = httpx.ASGITransport(app=application, client=("198.51.100.7", 443))
    async with httpx.AsyncClient(transport=transport, base_url="https://ratereplay.test") as client:
        assert (await client.get("/v1/meta")).status_code == 200
        limited = await client.get("/v1/meta")
        metrics = await client.get("/metrics")

    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "60"
    assert limited.headers["cache-control"] == "no-store"
    assert limited.json()["code"] == "TEST_RATE_LIMITED"
    assert "198.51.100.7" not in limited.text
    assert 'scope="READ"' in metrics.text
    assert "429" in application.openapi()["paths"]["/v1/meta"]["get"]["responses"]


@pytest.mark.anyio
async def test_trusted_proxy_uses_rightmost_untrusted_forwarded_address() -> None:
    settings = replace(AppSettings.for_test(), trusted_proxy_cidrs=("10.0.0.0/8",))
    application = create_app(settings)
    application.state.read_limiter = _limiter(limit=1, maximum_identifiers=8)
    transport = httpx.ASGITransport(app=application, client=("10.0.0.9", 443))
    async with httpx.AsyncClient(transport=transport, base_url="https://ratereplay.test") as client:
        first = await client.get(
            "/v1/meta",
            headers={"X-Forwarded-For": "198.51.100.4, 10.0.0.8"},
        )
        second_client = await client.get(
            "/v1/meta",
            headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.8"},
        )
        limited = await client.get(
            "/v1/meta",
            headers={"X-Forwarded-For": "198.51.100.4, 10.0.0.8"},
        )

    assert first.status_code == 200
    assert second_client.status_code == 200
    assert limited.status_code == 429


@pytest.mark.anyio
async def test_untrusted_peer_cannot_spoof_forwarded_identity() -> None:
    application = create_app(AppSettings.for_test())
    application.state.read_limiter = _limiter(limit=1, maximum_identifiers=8)
    transport = httpx.ASGITransport(app=application, client=("192.0.2.9", 443))
    async with httpx.AsyncClient(transport=transport, base_url="https://ratereplay.test") as client:
        first = await client.get("/v1/meta", headers={"X-Forwarded-For": "198.51.100.1"})
        limited = await client.get("/v1/meta", headers={"X-Forwarded-For": "203.0.113.2"})

    assert first.status_code == 200
    assert limited.status_code == 429


@pytest.mark.anyio
async def test_readiness_fails_closed_without_dependency_details() -> None:
    application = create_app(AppSettings.for_test())

    class UnavailableStore:
        def list_prefix(self, prefix: str) -> tuple[str, ...]:
            raise ObjectStoreError("PRIVATE_BACKEND_FAILURE", f"unavailable at {prefix}")

    application.state.object_store = UnavailableStore()
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="https://ratereplay.test") as client:
        response = await client.get("/readyz")
        metrics = await client.get("/metrics")

    assert response.status_code == 503
    assert response.json()["code"] == "DEPENDENCY_UNAVAILABLE"
    assert "PRIVATE_BACKEND_FAILURE" not in response.text
    assert "__readiness__" not in response.text
    assert 'outcome="unready"' in metrics.text


@pytest.mark.anyio
async def test_dependency_and_unexpected_failures_use_redacted_problem_schema() -> None:
    application = create_app(AppSettings.for_test())

    def unavailable() -> None:
        raise OperationalError("private statement", {}, RuntimeError("private database host"))

    def unexpected() -> None:
        raise RuntimeError("private implementation detail")

    application.add_api_route("/test/dependency", unavailable)
    application.add_api_route("/test/unexpected", unexpected)
    transport = httpx.ASGITransport(app=application, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="https://ratereplay.test") as client:
        dependency = await client.get("/test/dependency")
        internal = await client.get("/test/unexpected")

    assert dependency.status_code == 503
    assert dependency.json()["code"] == "DEPENDENCY_UNAVAILABLE"
    assert internal.status_code == 500
    assert internal.json()["code"] == "UNEXPECTED_FAILURE"
    combined = dependency.text + internal.text
    assert "private" not in combined
    assert dependency.headers["cache-control"] == "no-store"
    assert internal.headers["cache-control"] == "no-store"
