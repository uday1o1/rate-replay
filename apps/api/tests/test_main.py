import asyncio

import httpx
from ratereplay_api.main import app


def test_public_metadata_is_narrow_and_versioned() -> None:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.get("/v1/meta")

    response = asyncio.run(request())
    assert response.status_code == 200
    assert response.json() == {
        "calculation_time_mode": "HISTORICAL_REPLAY",
        "evidence_level": "FOUNDATION_ONLY",
        "schema_version": "v1",
    }
    assert "/v1/meta" in app.openapi()["paths"]
