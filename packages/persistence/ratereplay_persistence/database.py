"""SQLAlchemy engine and unit-of-work construction."""

from __future__ import annotations

import os
from dataclasses import dataclass

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    """Declarative registry shared by all persistence modules."""


@dataclass(frozen=True, slots=True)
class DatabaseAtRestConfiguration:
    """Fail-closed declaration for the deployment-managed database volume."""

    mode: str

    @classmethod
    def from_environment(cls, *, environment: str) -> DatabaseAtRestConfiguration:
        variable = "RATEREPLAY_DATABASE_AT_REST_ENCRYPTION"
        mode = os.getenv(variable, "development-unencrypted")
        if mode not in {"development-unencrypted", "managed-volume"}:
            raise RuntimeError(f"{variable} must be development-unencrypted or managed-volume")
        if environment in {"production", "staging"} and mode != "managed-volume":
            raise RuntimeError("Production and staging require managed database volume encryption")
        return cls(mode=mode)


def make_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Build a production engine or a deterministic in-memory test engine."""

    options: dict[str, object] = {
        "echo": echo,
        "pool_pre_ping": True,
    }
    if database_url == "sqlite+pysqlite:///:memory:":
        options.update(
            {
                "connect_args": {"check_same_thread": False},
                "poolclass": StaticPool,
            }
        )
    return create_engine(database_url, **options)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    """Create short-lived sessions with explicit commit boundaries."""

    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
