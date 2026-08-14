from __future__ import annotations

import pytest

from scripts.m7_pg_restore_adapter import command

CONTAINER_ID = "a" * 64


def test_pg_restore_adapter_separates_listing_from_quarantine_restore() -> None:
    assert command(CONTAINER_ID, ("--list",)) == (
        "docker",
        "exec",
        "-i",
        CONTAINER_ID,
        "pg_restore",
        "--list",
    )
    restored = command(CONTAINER_ID, ("--clean", "--exit-on-error"))
    assert restored[:5] == ("docker", "exec", "-i", CONTAINER_ID, "pg_restore")
    assert restored[5:9] == ("-U", "ratereplay", "-d", "ratereplay")
    assert restored[9:] == ("--clean", "--exit-on-error")


def test_pg_restore_adapter_rejects_unresolved_container_identity() -> None:
    with pytest.raises(ValueError, match="container identity"):
        command("container-name;touch", ("--list",))
