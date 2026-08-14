"""Local username, password, and server-side session contract."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from argon2 import PasswordHasher, Type
from argon2.exceptions import VerificationError
from ratereplay_persistence.audit import append_audit_event
from ratereplay_persistence.models import SessionRecord, UserRecord
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ratereplay_api.abuse import SlidingWindowRateLimiter
from ratereplay_api.problems import ApiProblem

SESSION_IDLE_LIFETIME = timedelta(minutes=30)
SESSION_ABSOLUTE_LIFETIME = timedelta(hours=24)
USERNAME_PATTERN = re.compile(r"[a-z0-9_]{3,64}\Z", re.ASCII)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def canonicalize_username(username: str) -> str:
    translated = username.translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )
    if USERNAME_PATTERN.fullmatch(translated) is None:
        raise ApiProblem(
            status_code=422,
            code="INVALID_USERNAME",
            message="Username must contain 3 to 64 ASCII letters, digits, or underscores.",
            field_paths=("username",),
        )
    return translated


def validate_password(password: str) -> None:
    if not 12 <= len(password) <= 128:
        raise ApiProblem(
            status_code=422,
            code="INVALID_PASSWORD_LENGTH",
            message="Password must contain 12 to 128 characters.",
            field_paths=("password",),
        )


@dataclass(frozen=True, slots=True)
class SessionGrant:
    user_id: str
    username: str
    session_token: str
    csrf_token: str
    idle_expires_at: datetime
    absolute_expires_at: datetime


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    user_id: str
    username: str
    session_id: str
    csrf_hash: str
    idle_expires_at: datetime
    absolute_expires_at: datetime


class LoginRateLimiter(SlidingWindowRateLimiter):
    """Backward-compatible named limiter for authentication and upload budgets."""

    def __init__(
        self,
        key: bytes,
        *,
        limit: int = 5,
        window: timedelta = timedelta(minutes=1),
        code: str = "AUTH_RATE_LIMITED",
        message: str = "Too many authentication attempts. Try again later.",
        scope: str = "AUTH",
        on_reject: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(
            key,
            limit=limit,
            window=window,
            code=code,
            message=message,
            scope=scope,
            on_reject=on_reject,
        )


class AuthService:
    def __init__(self, session_key: bytes, *, clock: Callable[[], datetime] = utc_now) -> None:
        if len(session_key) < 32:
            raise ValueError("Session key must contain at least 32 bytes")
        self._session_key = session_key
        self._clock = clock
        self._hasher = PasswordHasher(
            time_cost=3,
            memory_cost=65_536,
            parallelism=4,
            hash_len=32,
            salt_len=16,
            type=Type.ID,
        )
        self._dummy_hash = self._hasher.hash(secrets.token_urlsafe(32))

    @property
    def now(self) -> datetime:
        return self._clock().astimezone(UTC)

    def _digest(self, purpose: bytes, value: str) -> str:
        return hmac.new(
            self._session_key,
            purpose + b"\x00" + value.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def user_pseudonym(self, user_id: str) -> str:
        """Return a process-stable pseudonym safe for operational correlation."""

        return self._digest(b"telemetry-user", user_id)[:24]

    def _new_session(
        self,
        database: Session,
        user: UserRecord,
        *,
        event_type: Literal["AUTH_REGISTERED", "AUTH_LOGIN_SUCCEEDED"],
    ) -> SessionGrant:
        now = self.now
        session_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        idle_expires_at = now + SESSION_IDLE_LIFETIME
        absolute_expires_at = now + SESSION_ABSOLUTE_LIFETIME
        record = SessionRecord(
            id=secrets.token_hex(16),
            user_id=user.id,
            token_hash=self._digest(b"session", session_token),
            csrf_hash=self._digest(b"csrf", csrf_token),
            created_at=now,
            last_seen_at=now,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
        )
        database.add(record)
        append_audit_event(
            database,
            owner_user_id=user.id,
            event_type=event_type,
            subject_type="SESSION",
            subject_id=record.id,
            sequence=0,
            outcome="SUCCEEDED",
            now=now,
        )
        database.commit()
        return SessionGrant(
            user_id=user.id,
            username=user.username_canonical,
            session_token=session_token,
            csrf_token=csrf_token,
            idle_expires_at=idle_expires_at,
            absolute_expires_at=absolute_expires_at,
        )

    def register(self, database: Session, *, username: str, password: str) -> SessionGrant:
        canonical = canonicalize_username(username)
        validate_password(password)
        user = UserRecord(
            id=secrets.token_hex(16),
            username_canonical=canonical,
            password_hash=self._hasher.hash(password),
            created_at=self.now,
        )
        database.add(user)
        try:
            database.flush()
        except IntegrityError as error:
            database.rollback()
            raise ApiProblem(
                status_code=409,
                code="USERNAME_UNAVAILABLE",
                message="That username is unavailable.",
                field_paths=("username",),
            ) from error
        append_audit_event(
            database,
            owner_user_id=user.id,
            event_type="AUTH_REGISTERED",
            subject_type="ACCOUNT",
            subject_id=user.id,
            sequence=0,
            outcome="SUCCEEDED",
            now=self.now,
        )
        return self._new_session(database, user, event_type="AUTH_REGISTERED")

    def login(
        self,
        database: Session,
        *,
        username: str,
        password: str,
        prior_session_token: str | None,
    ) -> SessionGrant:
        canonical = canonicalize_username(username)
        validate_password(password)
        user = database.scalar(select(UserRecord).where(UserRecord.username_canonical == canonical))
        verifier = self._dummy_hash if user is None else user.password_hash
        try:
            verified: bool = self._hasher.verify(verifier, password)
        except VerificationError:
            verified = False
        if user is None or not verified:
            raise ApiProblem(
                status_code=401,
                code="INVALID_CREDENTIALS",
                message="Username or password is incorrect.",
            )
        if self._hasher.check_needs_rehash(user.password_hash):
            user.password_hash = self._hasher.hash(password)
        if prior_session_token is not None:
            prior = database.scalar(
                select(SessionRecord).where(
                    SessionRecord.token_hash == self._digest(b"session", prior_session_token)
                )
            )
            if prior is not None and prior.revoked_at is None:
                prior.revoked_at = self.now
        return self._new_session(database, user, event_type="AUTH_LOGIN_SUCCEEDED")

    def authenticate(self, database: Session, *, session_token: str | None) -> AuthenticatedSession:
        if session_token is None:
            raise self._authentication_required()
        row = database.execute(
            select(SessionRecord, UserRecord)
            .join(UserRecord, UserRecord.id == SessionRecord.user_id)
            .where(SessionRecord.token_hash == self._digest(b"session", session_token))
        ).one_or_none()
        if row is None:
            raise self._authentication_required()
        record, user = row
        if user.lifecycle_state != "ACTIVE":
            raise self._authentication_required()
        now = self.now
        idle_expires_at = _aware(record.idle_expires_at)
        absolute_expires_at = _aware(record.absolute_expires_at)
        if record.revoked_at is not None or now >= idle_expires_at or now >= absolute_expires_at:
            if record.revoked_at is None:
                record.revoked_at = now
                database.commit()
            raise self._authentication_required()
        record.last_seen_at = now
        record.idle_expires_at = min(now + SESSION_IDLE_LIFETIME, absolute_expires_at)
        database.commit()
        return AuthenticatedSession(
            user_id=user.id,
            username=user.username_canonical,
            session_id=record.id,
            csrf_hash=record.csrf_hash,
            idle_expires_at=_aware(record.idle_expires_at),
            absolute_expires_at=absolute_expires_at,
        )

    def verify_csrf(self, authenticated: AuthenticatedSession, csrf_token: str | None) -> None:
        if csrf_token is None or not hmac.compare_digest(
            authenticated.csrf_hash,
            self._digest(b"csrf", csrf_token),
        ):
            raise ApiProblem(
                status_code=403,
                code="CSRF_REJECTED",
                message="A valid CSRF token is required.",
            )

    def logout(self, database: Session, *, authenticated: AuthenticatedSession) -> None:
        record = database.get(SessionRecord, authenticated.session_id)
        if record is not None and record.revoked_at is None:
            now = self.now
            record.revoked_at = now
            append_audit_event(
                database,
                owner_user_id=authenticated.user_id,
                event_type="AUTH_LOGOUT",
                subject_type="SESSION",
                subject_id=authenticated.session_id,
                sequence=0,
                outcome="SUCCEEDED",
                now=now,
            )
            database.commit()

    @staticmethod
    def _authentication_required() -> ApiProblem:
        return ApiProblem(
            status_code=401,
            code="AUTHENTICATION_REQUIRED",
            message="A valid application session is required.",
        )
