"""Validated API process configuration."""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppSettings:
    environment: str
    database_url: str
    allowed_origin: str
    session_key: bytes
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
        secret_path = os.getenv("RATEREPLAY_SESSION_SECRET_FILE")
        if secret_path is None:
            if environment in {"production", "staging"}:
                raise RuntimeError("RATEREPLAY_SESSION_SECRET_FILE is required")
            session_key = secrets.token_bytes(32)
        else:
            session_key = Path(secret_path).read_bytes().strip()
            if len(session_key) < 32:
                raise RuntimeError("Session secret must contain at least 32 bytes")
        return cls(
            environment=environment,
            database_url=database_url,
            allowed_origin=allowed_origin,
            session_key=session_key,
            auto_create_schema=environment == "development",
        )

    @classmethod
    def for_test(
        cls,
        *,
        database_url: str = "sqlite+pysqlite:///:memory:",
        allowed_origin: str = "https://app.ratereplay.test",
    ) -> AppSettings:
        return cls(
            environment="test",
            database_url=database_url,
            allowed_origin=allowed_origin,
            session_key=b"rate-replay-test-session-key-v1!",
            secure_cookies=True,
            auto_create_schema=True,
        )
