"""Immutable versioned keyrings shared by encrypted persistence adapters."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

KEY_VERSION = re.compile(r"[A-Za-z0-9._-]{1,64}\Z", re.ASCII)


class KeyringError(RuntimeError):
    """A configured versioned keyring is unavailable or invalid."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class VersionedKeyring:
    """An immutable exact-32-byte key mapping with an explicit write version."""

    current_version: str
    keys: Mapping[str, bytes]

    def __post_init__(self) -> None:
        if KEY_VERSION.fullmatch(self.current_version) is None:
            raise KeyringError("KEY_VERSION_INVALID", "Current key version is invalid")
        copied: dict[str, bytes] = {}
        for version, key in self.keys.items():
            if KEY_VERSION.fullmatch(version) is None:
                raise KeyringError("KEY_VERSION_INVALID", "Key version is invalid")
            if not isinstance(key, bytes) or len(key) != 32:
                raise KeyringError("KEY_LENGTH_INVALID", "Every key must contain exactly 32 bytes")
            copied[version] = bytes(key)
        if not copied:
            raise KeyringError("KEYRING_EMPTY", "Keyring must contain at least one key")
        if self.current_version not in copied:
            raise KeyringError("CURRENT_KEY_UNAVAILABLE", "Current key version is unavailable")
        object.__setattr__(self, "keys", MappingProxyType(copied))

    @classmethod
    def single(cls, version: str, key: bytes) -> VersionedKeyring:
        return cls(current_version=version, keys={version: key})

    def current_key(self) -> bytes:
        return self.keys[self.current_version]

    def require(self, version: str) -> bytes:
        try:
            return self.keys[version]
        except KeyError as error:
            raise KeyringError(
                "KEY_VERSION_UNAVAILABLE",
                "Required historical key version is unavailable",
            ) from error


def load_keyring(directory: Path, *, current_version: str) -> VersionedKeyring:
    """Load version-named raw or lowercase/uppercase hexadecimal key files."""

    try:
        paths = tuple(sorted(path for path in directory.iterdir() if path.is_file()))
    except OSError as error:
        raise KeyringError("KEYRING_UNREADABLE", "Key directory cannot be read") from error
    if not paths:
        raise KeyringError("KEYRING_EMPTY", "Key directory is empty")
    keys: dict[str, bytes] = {}
    for path in paths:
        version = path.name
        if KEY_VERSION.fullmatch(version) is None:
            raise KeyringError("KEY_VERSION_INVALID", "Key filename is invalid")
        try:
            encoded = path.read_bytes()
        except OSError as error:
            raise KeyringError("KEYRING_UNREADABLE", "Key file cannot be read") from error
        key = encoded
        hexadecimal = encoded.strip()
        if len(hexadecimal) == 64:
            try:
                key = bytes.fromhex(hexadecimal.decode("ascii"))
            except (UnicodeError, ValueError) as error:
                raise KeyringError(
                    "KEY_ENCODING_INVALID",
                    "Key must be raw bytes or hexadecimal",
                ) from error
        if len(key) != 32:
            raise KeyringError("KEY_LENGTH_INVALID", "Every key must contain exactly 32 bytes")
        keys[version] = key
    return VersionedKeyring(current_version=current_version, keys=keys)
