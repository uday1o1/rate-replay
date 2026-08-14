from __future__ import annotations

from pathlib import Path

import pytest
from ratereplay_persistence.keyrings import KeyringError, VersionedKeyring, load_keyring


def test_versioned_keyring_is_immutable_and_requires_exact_versions() -> None:
    source = {"ledger-v1": b"a" * 32, "ledger-v2": b"b" * 32}
    keyring = VersionedKeyring(current_version="ledger-v2", keys=source)
    source["ledger-v2"] = b"c" * 32

    assert keyring.current_key() == b"b" * 32
    assert keyring.require("ledger-v1") == b"a" * 32
    with pytest.raises(TypeError):
        keyring.keys["ledger-v3"] = b"d" * 32  # type: ignore[index]
    with pytest.raises(KeyringError) as missing:
        keyring.require("ledger-v0")
    assert missing.value.code == "KEY_VERSION_UNAVAILABLE"


@pytest.mark.parametrize(
    ("version", "key", "code"),
    [
        ("", b"a" * 32, "KEY_VERSION_INVALID"),
        ("unsafe/path", b"a" * 32, "KEY_VERSION_INVALID"),
        ("ledger-v1", b"short", "KEY_LENGTH_INVALID"),
    ],
)
def test_keyring_rejects_invalid_versions_and_lengths(version: str, key: bytes, code: str) -> None:
    with pytest.raises(KeyringError) as invalid:
        VersionedKeyring(current_version=version, keys={version: key})
    assert invalid.value.code == code


def test_load_keyring_accepts_raw_and_hexadecimal_files(tmp_path: Path) -> None:
    directory = tmp_path / "keys"
    directory.mkdir()
    (directory / "ledger-v1").write_bytes(b"a" * 32)
    (directory / "ledger-v2").write_text("62" * 32, encoding="ascii")

    keyring = load_keyring(directory, current_version="ledger-v2")

    assert keyring.current_key() == b"b" * 32
    assert keyring.require("ledger-v1") == b"a" * 32


def test_load_keyring_rejects_empty_unreadable_or_bad_key_files(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(KeyringError) as no_keys:
        load_keyring(empty, current_version="ledger-v1")
    assert no_keys.value.code == "KEYRING_EMPTY"

    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "ledger-v1").write_text("not-a-key", encoding="ascii")
    with pytest.raises(KeyringError) as bad_key:
        load_keyring(invalid, current_version="ledger-v1")
    assert bad_key.value.code == "KEY_LENGTH_INVALID"
