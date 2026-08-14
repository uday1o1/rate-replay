"""Validated API process configuration."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from ratereplay_persistence.database import DatabaseAtRestConfiguration
from ratereplay_persistence.keyrings import KeyringError, VersionedKeyring, load_keyring
from ratereplay_persistence.object_store import ObjectStoreConfiguration


@dataclass(frozen=True, slots=True)
class AppSettings:
    environment: str
    database_url: str
    database_at_rest: DatabaseAtRestConfiguration
    allowed_origin: str
    session_key: bytes
    deletion_ledger_keyring: VersionedKeyring
    restore_keyring: VersionedKeyring
    object_store_root: Path
    object_store_configuration: ObjectStoreConfiguration
    deletion_ledger_root: Path
    espi_schema_path: Path
    repository_root: Path
    trusted_proxy_cidrs: tuple[str, ...] = ()
    secure_cookies: bool = True
    auto_create_schema: bool = False

    @classmethod
    def from_environment(cls) -> AppSettings:
        environment = os.getenv("RATEREPLAY_ENV", "development")
        database_url = os.getenv(
            "RATEREPLAY_DATABASE_URL",
            "sqlite+pysqlite:///:memory:",
        )
        allowed_origin = os.getenv("RATEREPLAY_ALLOWED_ORIGIN", "https://localhost:5173")
        object_store_root = Path(
            os.getenv("RATEREPLAY_OBJECT_STORE_ROOT", "/private/tmp/rate-replay-objects")
        )
        configured_ledger_root = os.getenv("RATEREPLAY_DELETION_LEDGER_ROOT")
        deletion_ledger_root = Path(
            configured_ledger_root
            if configured_ledger_root is not None
            else f"/private/tmp/rate-replay-deletion-ledger-v2-{os.getpid()}-{secrets.token_hex(6)}"
        )
        espi_schema_path = Path(
            os.getenv(
                "RATEREPLAY_ESPI_SCHEMA_PATH",
                "third_party/espi-schema/espi-4.0.xsd",
            )
        )
        repository_root = Path(os.getenv("RATEREPLAY_REPOSITORY_ROOT", ".")).resolve()
        secret_path = os.getenv("RATEREPLAY_SESSION_SECRET_FILE")
        if secret_path is None:
            if environment in {"production", "staging"}:
                raise RuntimeError("RATEREPLAY_SESSION_SECRET_FILE is required")
            session_key = secrets.token_bytes(32)
        else:
            session_key = Path(secret_path).read_bytes().strip()
            if len(session_key) < 32:
                raise RuntimeError("Session secret must contain at least 32 bytes")
        deletion_ledger_keyring = _load_control_keyring(
            environment=environment,
            directory_variable="RATEREPLAY_DELETION_LEDGER_KEYS_DIR",
            current_version_variable="RATEREPLAY_DELETION_LEDGER_CURRENT_KEY_VERSION",
            legacy_file_variable="RATEREPLAY_DELETION_LEDGER_KEY_FILE",
            default_version="ledger-v1",
        )
        restore_keyring = _load_control_keyring(
            environment=environment,
            directory_variable="RATEREPLAY_RESTORE_KEYS_DIR",
            current_version_variable="RATEREPLAY_RESTORE_CURRENT_KEY_VERSION",
            legacy_file_variable="RATEREPLAY_RESTORE_KEY_FILE",
            default_version=os.getenv("RATEREPLAY_RESTORE_KEY_VERSION", "restore-v1"),
        )
        object_store_configuration = ObjectStoreConfiguration.from_environment(
            environment=environment,
            default_root=object_store_root,
        )
        database_at_rest = DatabaseAtRestConfiguration.from_environment(
            environment=environment,
        )
        return cls(
            environment=environment,
            database_url=database_url,
            database_at_rest=database_at_rest,
            allowed_origin=allowed_origin,
            session_key=session_key,
            deletion_ledger_keyring=deletion_ledger_keyring,
            restore_keyring=restore_keyring,
            object_store_root=object_store_root,
            object_store_configuration=object_store_configuration,
            deletion_ledger_root=deletion_ledger_root,
            espi_schema_path=espi_schema_path,
            repository_root=repository_root,
            trusted_proxy_cidrs=_trusted_proxy_cidrs(),
            auto_create_schema=environment == "development",
        )

    @classmethod
    def for_test(
        cls,
        *,
        database_url: str = "sqlite+pysqlite:///:memory:",
        allowed_origin: str = "https://app.ratereplay.test",
        object_store_root: Path = Path("/private/tmp/rate-replay-test-objects"),
        deletion_ledger_root: Path | None = None,
        espi_schema_path: Path = Path("third_party/espi-schema/espi-4.0.xsd"),
        repository_root: Path = Path("."),
    ) -> AppSettings:
        return cls(
            environment="test",
            database_url=database_url,
            database_at_rest=DatabaseAtRestConfiguration(
                mode="development-unencrypted",
            ),
            allowed_origin=allowed_origin,
            session_key=b"rate-replay-test-session-key-v1!",
            deletion_ledger_keyring=VersionedKeyring.single(
                "ledger-test-v1", b"rate-replay-test-ledger-key-v1!!"
            ),
            restore_keyring=VersionedKeyring.single(
                "restore-test-v1", b"rate-replay-test-restore-key-v1!"
            ),
            object_store_root=object_store_root,
            object_store_configuration=ObjectStoreConfiguration.filesystem(object_store_root),
            deletion_ledger_root=(
                deletion_ledger_root
                if deletion_ledger_root is not None
                else Path(
                    f"/private/tmp/rate-replay-test-deletion-ledger-v2-{os.getpid()}-"
                    f"{secrets.token_hex(6)}"
                )
            ),
            espi_schema_path=espi_schema_path,
            repository_root=repository_root.resolve(),
            trusted_proxy_cidrs=(),
            secure_cookies=True,
            auto_create_schema=True,
        )


def _load_control_key(*, environment: str, variable: str) -> bytes:
    path = os.getenv(variable)
    if path is None:
        if environment in {"production", "staging"}:
            raise RuntimeError(f"{variable} is required")
        return secrets.token_bytes(32)
    value = Path(path).read_bytes().strip()
    if len(value) != 32:
        raise RuntimeError(f"{variable} must reference exactly 32 bytes")
    return value


def _load_control_keyring(
    *,
    environment: str,
    directory_variable: str,
    current_version_variable: str,
    legacy_file_variable: str,
    default_version: str,
) -> VersionedKeyring:
    directory = os.getenv(directory_variable)
    current_version = os.getenv(current_version_variable, default_version)
    if directory is None:
        return VersionedKeyring.single(
            current_version,
            _load_control_key(environment=environment, variable=legacy_file_variable),
        )
    if os.getenv(legacy_file_variable) is not None:
        raise RuntimeError(f"Configure {directory_variable} or {legacy_file_variable}, not both")
    try:
        return load_keyring(Path(directory), current_version=current_version)
    except KeyringError as error:
        raise RuntimeError(f"{directory_variable} is invalid: {error.code}") from error


def _trusted_proxy_cidrs() -> tuple[str, ...]:
    from ipaddress import ip_network

    configured = os.getenv("RATEREPLAY_TRUSTED_PROXY_CIDRS", "")
    values = tuple(value.strip() for value in configured.split(",") if value.strip())
    try:
        return tuple(str(ip_network(value, strict=False)) for value in values)
    except ValueError as error:
        raise RuntimeError("RATEREPLAY_TRUSTED_PROXY_CIDRS is invalid") from error
