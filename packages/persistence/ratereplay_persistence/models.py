"""Relational records shared by API and worker processes."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ratereplay_persistence.database import Base


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    username_canonical: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    sessions: Mapped[list[SessionRecord]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SessionRecord(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_active", "user_id", "revoked_at"),
        Index("ix_sessions_idle_expiry", "idle_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[UserRecord] = relationship(back_populates="sessions")
