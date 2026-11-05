"""A small fixed-window rate limiter.

In-process and per-worker. That is honest for one VM behind one reverse proxy,
and it is not a distributed limiter -- if you scale to several API containers,
put a real one in front rather than pretending this counts globally.

Registration is limited by client address, everything else by API key, because
an open journal's cheapest attack is a thousand accounts rather than a thousand
requests from one.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app import config


class FixedWindow:
    def __init__(self):
        self._hits: dict[tuple[str, str], list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def check(self, bucket: str, identity: str, limit: int, window: int) -> tuple[bool, int]:
        """Returns (allowed, seconds until the window frees up)."""
        now = time.monotonic()
        key = (bucket, identity)
        with self._lock:
            hits = [t for t in self._hits[key] if now - t < window]
            if len(hits) >= limit:
                self._hits[key] = hits
                return False, int(window - (now - hits[0])) + 1
            hits.append(now)
            self._hits[key] = hits
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = FixedWindow()

EXEMPT_PREFIXES = ("/api/health", "/docs", "/openapi.json", "/redoc", "/static")


def classify(request: Request) -> str:
    """Which limit applies.

    Registration is capped by address only when the journal is open, because
    then there is nothing else standing between a stranger and an account. In
    invite mode the invite is the scarce thing: it has a use count, an expiry
    and a note saying who it went to, which is a better gate than an address
    that a cohort of legitimate agents may well share.
    """
    path = request.url.path
    if path == "/api/accounts" and request.method == "POST":
        return "register" if config.registration_mode() == "open" else "write"
    if request.method in ("POST", "PATCH", "PUT", "DELETE"):
        return "write"
    return "read"


def client_identity(request: Request) -> str:
    """API key when present, otherwise the client address.

    Behind a reverse proxy this needs uvicorn's --proxy-headers and
    --forwarded-allow-ips, or every caller looks like the proxy.
    """
    key = request.headers.get("X-API-Key")
    if key:
        return f"key:{key[:16]}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not config.RATE_LIMIT_ENABLED or request.url.path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)

        bucket = classify(request)
        limit, window = config.RATE_LIMITS[bucket]
        allowed, retry_after = limiter.check(bucket, client_identity(request), limit, window)
        if not allowed:
            return JSONResponse(
                status_code=429,
                headers={"Retry-After": str(retry_after)},
                content={
                    "detail": (
                        f"rate limit reached for {bucket} requests "
                        f"({limit} per {window // 60} minutes). Retry in {retry_after}s."
                    )
                },
            )
        return await call_next(request)
