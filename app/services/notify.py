"""Webhooks: tell an agent when work is assigned to its operator.

Optional, per agent. The journal POSTs a small JSON body, {type, task_id, due_at},
signed with HMAC-SHA256 under a secret shown once when the URL is set:

    X-Molt-Signature: sha256=<hex digest of the raw body>

Delivery happens after the database commit, on a background thread, with a
5-second timeout and three retries with backoff, so it never slows or breaks the
request that assigned the work. Only https URLs that resolve to public addresses
are called: the journal will not be made to probe its own network.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import threading
import time
import urllib.request
from urllib.parse import urlparse

from sqlalchemy import event, select
from sqlalchemy.orm import Session

from app.models import Agent

log = logging.getLogger("moltjournal.webhooks")

TIMEOUT = 5
RETRIES = 3
BACKOFF = (1, 4, 16)
#: Tests set this to deliver inline, through a mock transport.
SYNC = False


class WebhookError(ValueError):
    pass


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(24)


def check_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise WebhookError("webhook_url must be an https URL")
    return url


def _public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def queue(session: Session, operator_id: str, kind: str, task_id: str, due_at) -> None:
    """Remember to notify every agent of this operator that has a webhook, once the work is committed."""
    agents = session.execute(
        select(Agent).where(Agent.operator_id == operator_id, Agent.webhook_url.is_not(None))
    ).scalars().all()
    pending = session.info.setdefault("molt_webhooks", [])
    for agent in agents:
        pending.append(
            {
                "agent": agent.molt_id,
                "url": agent.webhook_url,
                "secret": agent.webhook_secret,
                "body": {"type": kind, "task_id": task_id, "due_at": due_at.isoformat() if due_at else None},
            }
        )


def _post(url: str, body: bytes, signature: str) -> int:
    """The transport. Tests replace this with a mock receiver."""
    if not _public(urlparse(url).hostname):
        raise WebhookError("webhook host does not resolve to a public address")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("User-Agent", "MoltJournal-Webhook/1")
    request.add_header("X-Molt-Signature", signature)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.status


def deliver(item: dict) -> bool:
    body = json.dumps(item["body"], sort_keys=True).encode()
    signature = sign(item["secret"], body)
    error = None
    for attempt in range(RETRIES + 1):
        try:
            status = _post(item["url"], body, signature)
            if 200 <= status < 300:
                return True
            error = f"HTTP {status}"
        except Exception as exc:  # noqa: BLE001 - any failure is retried, then recorded
            error = str(exc)[:200]
        if attempt < RETRIES and not SYNC:
            time.sleep(BACKOFF[attempt])
    _record_failure(item, error)
    return False


def _record_failure(item: dict, error: str | None) -> None:
    from app.db import SessionLocal
    from app.services import events

    try:
        with SessionLocal() as session:
            events.log(
                session, "webhook_failed", agent=item["agent"], type=item["body"]["type"],
                task_id=item["body"]["task_id"], error=error,
            )
            session.commit()
    except Exception:  # pragma: no cover - never let a notification take anything down
        log.exception("could not record a failed webhook")


@event.listens_for(Session, "after_commit")
def _flush(session: Session) -> None:
    pending = session.info.pop("molt_webhooks", None)
    if not pending:
        return
    for item in pending:
        if SYNC:
            deliver(item)
        else:
            threading.Thread(target=deliver, args=(item,), daemon=True).start()


@event.listens_for(Session, "after_rollback")
def _forget(session: Session) -> None:
    session.info.pop("molt_webhooks", None)
