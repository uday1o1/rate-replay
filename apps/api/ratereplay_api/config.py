"""Validated API process configuration."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from ratereplay_persistence.database import DatabaseAtRestConfiguration
from ratereplay_persistence.object_store import ObjectStoreConfiguration


@dataclass(frozen=True, slots=True)
class AppSettings:
    environment: str
    database_url: str
    database_at_rest: DatabaseAtRestConfiguration
    allowed_origin: str
    session_key: bytes
    deletion_ledger_key: bytes
    restore_suppression_key: bytes
    restore_key_version: str
    object_store_root: Path
    object_store_configuration: ObjectStoreConfiguration
    deletion_ledger_root: Path
    espi_schema_path: Path
    repository_root: Path
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
        deletion_ledger_root = Path(
            os.getenv(
                "RATEREPLAY_DELETION_LEDGER_ROOT",
                "/private/tmp/rate-replay-deletion-ledger",
            )
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
        deletion_ledger_key = _load_control_key(
            environment=environment,
            variable="RATEREPLAY_DELETION_LEDGER_KEY_FILE",
        )
        restore_suppression_key = _load_control_key(
            environment=environment,
            variable="RATEREPLAY_RESTORE_KEY_FILE",
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
            deletion_ledger_key=deletion_ledger_key,
            restore_suppression_key=restore_suppression_key,
            restore_key_version=os.getenv("RATEREPLAY_RESTORE_KEY_VERSION", "restore-v1"),
            object_store_root=object_store_root,
            object_store_configuration=object_store_configuration,
            deletion_ledger_root=deletion_ledger_root,
            espi_schema_path=espi_schema_path,
            repository_root=repository_root,
            auto_create_schema=environment == "development",
        )

    @classmethod
    def for_test(
        cls,
        *,
        database_url: str = "sqlite+pysqlite:///:memory:",
        allowed_origin: str = "https://app.ratereplay.test",
        object_store_root: Path = Path("/private/tmp/rate-replay-test-objects"),
        deletion_ledger_root: Path = Path("/private/tmp/rate-replay-test-deletion-ledger"),
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
            deletion_ledger_key=b"rate-replay-test-ledger-key-v1!!",
            restore_suppression_key=b"rate-replay-test-restore-key-v1!",
            restore_key_version="restore-test-v1",
            object_store_root=object_store_root,
            object_store_configuration=ObjectStoreConfiguration.filesystem(object_store_root),
            deletion_ledger_root=deletion_ledger_root,
            espi_schema_path=espi_schema_path,
            repository_root=repository_root.resolve(),
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
    if len(value) < 32:
        raise RuntimeError(f"{variable} must reference at least 32 bytes")
    return value
