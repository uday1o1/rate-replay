from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI
from ratereplay_api.config import AppSettings
from ratereplay_api.main import create_app

ROOT = Path(__file__).resolve().parents[3]
ORIGIN = "https://app.ratereplay.test"
PASSWORD = "correct horse battery staple"
CANDIDATES = [
    "pge-e1-2026-07",
    "pge-eelec-2026-07",
    "pge-etouc-2026-07",
    "pge-etoud-2026-07",
    "pge-ev2a-2026-07",
]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def test_app(tmp_path: Path) -> FastAPI:
    return create_app(
        AppSettings.for_test(
            object_store_root=tmp_path / "objects",
            repository_root=ROOT,
        )
    )


@pytest.fixture
async def client(test_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as value:
        yield value


def _facts() -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            (ROOT / "tariffs/examples/m3-comparison-account.json").read_text(encoding="utf-8")
        ),
    )


def _headers(csrf: str, key: str) -> dict[str, str]:
    return {
        "Origin": ORIGIN,
        "Content-Type": "application/json",
        "X-CSRF-Token": csrf,
        "Idempotency-Key": key,
    }


@pytest.mark.anyio
async def test_private_account_completes_portfolio_core_through_public_api(
    client: httpx.AsyncClient,
) -> None:
    registered = await client.post(
        "/v1/auth/register",
        headers={"Origin": ORIGIN},
        json={"username": "portfolio_owner", "password": PASSWORD},
    )
    assert registered.status_code == 201, registered.text
    csrf = cast(str, registered.json()["csrf_token"])
    installed = await client.post(
        "/v1/imports/built-in-simulated-profile",
        headers=_headers(csrf, "portfolio-built-in-profile"),
    )
    assert installed.status_code == 201, installed.text
    installed_body = installed.json()
    assert installed_body["simulated"] is True
    profile = installed_body["profile"]

    facts = _facts()
    replayed = await client.post(
        "/v1/replays",
        headers=_headers(csrf, "portfolio-replay"),
        json={
            "request_schema_version": "replay-operation-v1",
            "profile_version_id": profile["profile_version_id"],
            "tariff_version_id": "pge-e1-2026-07",
            "account_facts": facts["account_facts"],
            "current_bill_total_cents": 30_000,
            "user_unsupported_lines": [
                {
                    "line_item_key": "local_tax",
                    "description": "Current bill only",
                    "amount_cents": 300,
                }
            ],
        },
    )
    assert replayed.status_code == 201, replayed.text
    replay = replayed.json()
    assert replay["result"]["supported_calculated_cents"] == 27_728
    assert replay["result"]["reconciliation"]["unexplained_residual_cents"] == 1_972
    assert replay["result"]["provenance_sources"]

    compared = await client.post(
        "/v1/comparisons",
        headers=_headers(csrf, "portfolio-comparison"),
        json={
            "request_schema_version": "comparison-operation-v1",
            "replay_id": replay["replay_id"],
            "candidate_tariff_version_ids": CANDIDATES,
            "account_facts": facts["account_facts"],
            "dated_eligibility_facts": facts["dated_eligibility_facts"],
        },
    )
    assert compared.status_code == 201, compared.text
    comparison = compared.json()["result"]
    assert comparison["rankable"] is True
    assert comparison["winner_tariff_version_ids"] == ["pge-etoud-2026-07"]
    assert comparison["savings_against_current_supported_cents"] == 1_707
    assert all(candidate["component_coverage"] for candidate in comparison["candidates"])

    slot_response = await client.get(f"/v1/profiles/{profile['profile_version_id']}/scenario-slots")
    assert slot_response.status_code == 200, slot_response.text
    slots = cast(list[dict[str, object]], slot_response.json()["slots"])
    positive_starts = {
        "2026-07-07T00:00:00Z",
        "2026-07-07T00:15:00Z",
        "2026-07-07T00:30:00Z",
        "2026-07-07T00:45:00Z",
    }
    reference_schedule = [
        {
            "slot_start_utc": slot["slot_start_utc"],
            "duration_seconds": slot["duration_seconds"],
            "energy_wh": 1_800 if slot["slot_start_utc"] in positive_starts else 0,
        }
        for slot in slots
    ]
    scenario_response = await client.post(
        "/v1/scenarios",
        headers=_headers(csrf, "portfolio-scenario"),
        json={
            "request_schema_version": "scenario-operation-v1",
            "profile_version_id": profile["profile_version_id"],
            "tariff_version_id": "pge-etoud-2026-07",
            "account_facts": facts["account_facts"],
            "dated_eligibility_facts": facts["dated_eligibility_facts"],
            "electrical_constraints": {
                "site_import_cap_w": None,
                "flexible_load_aggregate_cap_w": 7_200,
                "energy_basis": "METER_SIDE",
            },
            "loads": [
                {
                    "load_id": "00000000-0000-0000-0000-000000000001",
                    "physical_asset_key": "portfolio-ev-1",
                    "kind": "EV",
                    "mode": "HISTORICAL_ADDITION",
                    "execution_spec": {
                        "execution_type": "INTERRUPTIBLE_MODULATING",
                        "maximum_power_w": 7_200,
                        "minimum_power_when_active_w": 0,
                    },
                    "occurrences": [
                        {
                            "occurrence_id": "10000000-0000-0000-0000-000000000001",
                            "required_energy_wh": 7_200,
                            "earliest_start_utc": "2026-07-07T00:00:00Z",
                            "deadline_utc": "2026-07-07T07:00:00Z",
                            "reference_schedule": reference_schedule,
                        }
                    ],
                }
            ],
            "shift_existing_attestation_load_ids": [],
        },
    )
    assert scenario_response.status_code == 201, scenario_response.text
    scenario = scenario_response.json()["result"]
    assert scenario["historical_addition_label"] == ("HISTORICAL_COUNTERFACTUAL_NOT_FORECAST")
    assert scenario["decomposition"]["exact_measured_reconstruction"] is True
    assert scenario["exact"]["search_status"] == "OPTIMAL"
    assert scenario["exact"]["selected"]["verification"]["status"] == "VALID"
    assert scenario["heuristic"]["bill_optimality_claim"] is False
    assert scenario["manifest"]["tariff_compiler_content_sha256"]
    assert scenario["manifest"]["selected_verification_sha256"]
