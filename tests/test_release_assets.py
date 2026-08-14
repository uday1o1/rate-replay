from __future__ import annotations

from scripts.validate_release import validate_release_assets


def test_release_assets_enforce_isolation_and_hardening() -> None:
    assert validate_release_assets() == {
        "dockerfiles": 5,
        "hardened_services": 7,
        "services": 8,
    }
