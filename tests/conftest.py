from __future__ import annotations

import os
import tempfile

import pytest

# Point the app at a throwaway database before anything imports app.config.
_tmpdir = tempfile.mkdtemp(prefix="moltjournal-tests-")
os.environ["MOLT_DATABASE_URL"] = f"sqlite+pysqlite:///{_tmpdir}/test.db"
os.environ["MOLT_ADMIN_TOKEN"] = "test-admin-token"
# The gate and the limiter get their own tests; everything else predates them
# and would otherwise spend the whole suite being throttled.
os.environ.setdefault("MOLT_REGISTRATION", "open")
os.environ.setdefault("MOLT_RATE_LIMIT", "0")

from app.db import SessionLocal, engine  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture()
def session():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as s:
        yield s


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    # The web routes negotiate on Accept and User-Agent, and a bare TestClient
    # looks exactly like a program. Default to a browser; the negotiation tests
    # override these per request.
    with TestClient(
        app,
        headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/605.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
    ) as c:
        yield c


# --------------------------------------------------------------------------
# a real server, for the tests that need one (MCP adapter, dry-run harness)
# --------------------------------------------------------------------------


def _free_port() -> int:
    import socket as _socket

    with _socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture(scope="module")
def live_server(tmp_path_factory):
    yield from _spawn_server(tmp_path_factory)


@pytest.fixture
def fresh_server(tmp_path_factory):
    """A server of its own, for a test that a populated journal would disturb.

    Agents idling in a shared journal absorb review assignments and never act on
    them, which stalls whatever the next test is trying to drive.
    """
    yield from _spawn_server(tmp_path_factory)


def _spawn_server(tmp_path_factory):
    import subprocess
    import sys
    import time
    import urllib.error
    import urllib.request
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    port = _free_port()
    database = tmp_path_factory.mktemp("live") / "live.db"
    environment = {
        **os.environ,
        "MOLT_DATABASE_URL": f"sqlite+pysqlite:///{database}",
        "MOLT_AUTO_CREATE_SCHEMA": "1",
        "MOLT_REGISTRATION": "open",
        "MOLT_RATE_LIMIT": "0",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(120):
        if process.poll() is not None:
            pytest.fail(f"the server exited during startup:\n{process.stdout.read()}")
        try:
            urllib.request.urlopen(base + "/api/health", timeout=2)
            break
        except OSError:
            # URLError, ConnectionError and TimeoutError are all OSError; on
            # 3.14 a socket timeout is TimeoutError and is not a URLError.
            time.sleep(0.25)
    else:  # pragma: no cover
        process.terminate()
        pytest.fail("the server never came up")
    yield base
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover
        process.kill()
