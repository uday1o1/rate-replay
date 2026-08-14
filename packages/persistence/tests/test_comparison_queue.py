from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

from ratereplay_persistence.comparisons import comparison_semantic_identity
from ratereplay_tariffs.admission import load_all_admitted_tariffs
from ratereplay_tariffs.billing import IntervalReplayRequest, ReplayInterval
from ratereplay_tariffs.comparison import load_required_component_keys
from ratereplay_tariffs.schema import AccountFacts, DatedEligibilityFacts

ROOT = Path(__file__).resolve().parents[3]


def _request(*, annual_usage_wh: int | None = 3_500_000) -> IntervalReplayRequest:
    payload = cast(
        dict[str, Any],
        json.loads((ROOT / "tariffs/examples/m3-comparison-account.json").read_bytes()),
    )
    dated = cast(dict[str, object], payload["dated_eligibility_facts"])
    dated["annual_usage_wh"] = annual_usage_wh
    return IntervalReplayRequest(
        request_version="interval-replay-request-v1",
        profile_content_sha256="a" * 64,
        account_facts=AccountFacts.model_validate_json(json.dumps(payload["account_facts"])),
        energy_wh=100,
        intervals=(
            ReplayInterval(
                start_utc_ns=1_782_889_200_000_000_000,
                duration_seconds=900,
                energy_wh=100,
            ),
        ),
        dated_eligibility_facts=DatedEligibilityFacts.model_validate_json(json.dumps(dated)),
    )


def _hash(
    *,
    request: IntervalReplayRequest | None = None,
    current_replay_result_hash: str = "b" * 64,
    reverse_tariffs: bool = False,
    tariff_count: int = 5,
    omit_last_component: bool = False,
) -> str:
    tariffs = load_all_admitted_tariffs(ROOT)[:tariff_count]
    if reverse_tariffs:
        tariffs = tuple(reversed(tariffs))
    required_components = load_required_component_keys(ROOT)
    if omit_last_component:
        required_components = required_components[:-1]
    return comparison_semantic_identity(
        tariffs=tariffs,
        comparison_request=request or _request(),
        current_replay_result_hash=current_replay_result_hash,
        required_component_keys=required_components,
        environment_lock_hash="e" * 64,
    ).sha256()


def test_comparison_identity_is_canonical_and_binds_rank_inputs() -> None:
    baseline = _hash()

    assert _hash(reverse_tariffs=True) == baseline
    assert (
        len(
            {
                baseline,
                _hash(request=_request(annual_usage_wh=None)),
                _hash(current_replay_result_hash="c" * 64),
                _hash(tariff_count=4),
                _hash(omit_last_component=True),
            }
        )
        == 5
    )
