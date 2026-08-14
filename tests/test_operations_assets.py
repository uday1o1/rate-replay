from __future__ import annotations

from scripts.validate_operations import validate_operations_assets


def test_versioned_operations_assets_are_complete() -> None:
    assert validate_operations_assets() == {
        "alerts": 5,
        "indicators": 9,
        "panels": 7,
    }
