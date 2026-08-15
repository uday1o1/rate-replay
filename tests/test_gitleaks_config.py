from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]


def test_gitleaks_allowlist_is_narrow_and_keeps_default_rules() -> None:
    configuration = cast(
        dict[str, Any],
        tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8")),
    )

    assert configuration["extend"] == {"useDefault": True}
    assert configuration["allowlists"] == [
        {
            "description": (
                "Tariff explanation identifiers and bounded object reads are not credentials"
            ),
            "targetRules": ["generic-api-key"],
            "regexTarget": "match",
            "regexes": [
                'explanation_key":"tariff\\.[a-z0-9_.]+"',
                "^key,\\s+maximum_bytes=64\\s$",
            ],
        }
    ]
