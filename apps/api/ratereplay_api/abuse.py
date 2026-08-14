"""Bounded, privacy-preserving request budgets for the single-host V1 service."""

from __future__ import annotations

import hashlib
import hmac
import math
import threading
from collections import deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address, ip_network
from typing import Final

from fastapi import Request

from ratereplay_api.problems import ApiProblem

SESSION_COOKIE: Final = "__Host-ratereplay_session"
EXEMPT_PATHS: Final = frozenset({"/healthz", "/metrics", "/readyz"})
READ_METHODS: Final = frozenset({"GET", "HEAD", "OPTIONS"})


class SlidingWindowRateLimiter:
    """Enforce a fixed request budget without retaining raw client identifiers."""

    def __init__(
        self,
        key: bytes,
        *,
        limit: int,
        window: timedelta,
        code: str,
        message: str,
        scope: str,
        maximum_identifiers: int = 4096,
        on_reject: Callable[[str], None] | None = None,
    ) -> None:
        if len(key) < 32:
            raise ValueError("Rate-limit key must contain at least 32 bytes")
        if limit < 1 or window <= timedelta(0) or maximum_identifiers < 1:
            raise ValueError("Rate-limit bounds must be positive")
        self._key = key
        self._limit = limit
        self._window = window
        self._code = code
        self._message = message
        self._scope = scope
        self._maximum_identifiers = maximum_identifiers
        self._on_reject = on_reject
        self._attempts: dict[str, deque[datetime]] = {}
        self._overflow_digest = self._digest("overflow")
        self._lock = threading.Lock()

    @property
    def retained_identifier_count(self) -> int:
        """Expose only the bounded cardinality for qualification tests."""

        with self._lock:
            return len(self._attempts)

    def check(self, identifier: str, *, now: datetime) -> None:
        digest = self._digest(identifier)
        cutoff = now - self._window
        rejected = False
        retry_after = 1
        with self._lock:
            self._discard_expired(cutoff)
            if digest not in self._attempts:
                overflow_reserved = self._overflow_digest not in self._attempts
                direct_capacity = self._maximum_identifiers - int(overflow_reserved)
                if len(self._attempts) >= direct_capacity:
                    digest = self._overflow_digest
            attempts = self._attempts.setdefault(digest, deque())
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if len(attempts) >= self._limit:
                rejected = True
                retry_after = max(
                    1,
                    math.ceil((attempts[0] + self._window - now).total_seconds()),
                )
            else:
                attempts.append(now)
        if not rejected:
            return
        if self._on_reject is not None:
            self._on_reject(self._scope)
        raise ApiProblem(
            status_code=429,
            code=self._code,
            message=self._message,
            headers={"Retry-After": str(retry_after)},
        )

    def _discard_expired(self, cutoff: datetime) -> None:
        expired: list[str] = []
        for digest, attempts in self._attempts.items():
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if not attempts:
                expired.append(digest)
        for digest in expired:
            del self._attempts[digest]

    def _digest(self, identifier: str) -> str:
        return hmac.new(self._key, identifier.encode("utf-8"), hashlib.sha256).hexdigest()


def enforce_request_budget(request: Request) -> None:
    """Apply a coarse per-session or per-connection budget to every public API call."""

    if request.url.path in EXEMPT_PATHS:
        return
    limiter_name = "read_limiter" if request.method in READ_METHODS else "mutation_limiter"
    limiter: SlidingWindowRateLimiter = getattr(request.app.state, limiter_name)
    limiter.check(_request_identity(request), now=datetime.now(UTC))


def _request_identity(request: Request) -> str:
    session_token = request.cookies.get(SESSION_COOKIE)
    if session_token is not None and 1 <= len(session_token) <= 256:
        return f"session:{session_token}"
    client_host = effective_client_host(request)
    if len(client_host) > 256:
        client_host = "oversized"
    return f"connection:{client_host}"


def effective_client_host(request: Request) -> str:
    """Resolve forwarding only when the immediate peer is explicitly trusted."""

    peer = request.client.host if request.client is not None else "unknown"
    try:
        peer_address = ip_address(peer)
    except ValueError:
        return peer
    trusted = tuple(
        ip_network(value, strict=False) for value in request.app.state.settings.trusted_proxy_cidrs
    )
    if not any(peer_address in network for network in trusted):
        return str(peer_address)
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded is None or len(forwarded) > 2048:
        return str(peer_address)
    try:
        chain = tuple(ip_address(value.strip()) for value in forwarded.split(","))
    except ValueError:
        return str(peer_address)
    for candidate in reversed(chain):
        if not any(candidate in network for network in trusted):
            return str(candidate)
    return str(peer_address)
