from __future__ import annotations

import json
from pathlib import Path

from ratereplay_persistence.replays import replay_semantic_identity
from ratereplay_tariffs.admission import load_admitted_e1
from ratereplay_tariffs.billing import ReconciliationPolicy, ReplayRequest

ROOT = Path(__file__).resolve().parents[3]


def _request(**updates: object) -> ReplayRequest:
    payload = json.loads((ROOT / "tariffs/examples/e1-replay-input.json").read_bytes())
    payload.update(updates)
    return ReplayRequest.model_validate_json(json.dumps(payload))


def _hash(
    request: ReplayRequest,
    *,
    policy: ReconciliationPolicy | None = None,
) -> str:
    return replay_semantic_identity(
        tariff=load_admitted_e1(ROOT),
        replay_request=request,
        environment_lock_hash="e" * 64,
        reconciliation_policy=policy,
    ).sha256()


def test_replay_total_unsupported_tuple_and_policy_change_semantic_identity() -> None:
    baseline_request = _request()
    baseline = _hash(baseline_request)
    changed_total = _hash(_request(current_bill_total_cents=11_001))
    changed_line = _hash(
        _request(
            user_unsupported_lines=[
                {
                    "line_item_key": "user_entered_local_tax",
                    "description": "Different result-visible label",
                    "amount_cents": 200,
                }
            ]
        )
    )
    changed_policy = _hash(
        baseline_request,
        policy=ReconciliationPolicy(review_tolerance_cents=101),
    )

    assert len({baseline, changed_total, changed_line, changed_policy}) == 4


def test_replay_unsupported_line_order_is_canonical() -> None:
    lines = [
        {"line_item_key": "b", "description": "second", "amount_cents": 2},
        {"line_item_key": "a", "description": "first", "amount_cents": 1},
    ]
    first = _request(user_unsupported_lines=lines)
    second = _request(user_unsupported_lines=list(reversed(lines)))

    assert first.user_unsupported_lines == second.user_unsupported_lines
    assert _hash(first) == _hash(second)
