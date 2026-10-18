"""Tests for the opt-in single-event-loop queue probe."""

import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agent.api.app import create_app
from agent.dashboard import oauth
from agent.utils import queue_debug


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setitem(queue_debug._STATE, "inflight", {})
    queue_debug.set_enabled(False)

    app = create_app()
    app.dependency_overrides[oauth.require_session] = lambda: {"sub": "octocat"}
    return TestClient(app)


def test_probe_disabled_by_default_and_toggles(client: TestClient) -> None:
    resp = client.get("/debug/queue-probe")
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False

    resp = client.post("/debug/queue-probe", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert queue_debug.is_enabled() is True

    resp = client.post("/debug/queue-probe", json={"enabled": False})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False


def test_probe_rejects_bad_body(client: TestClient) -> None:
    assert client.post("/debug/queue-probe", json={"enabled": "yes"}).status_code == 400
    assert client.post("/debug/queue-probe", content=b"[]").status_code == 400


def test_probe_requires_session(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    client = TestClient(app)
    assert client.get("/debug/queue-probe").status_code == 401
    assert client.post("/debug/queue-probe", json={"enabled": True}).status_code == 401


def test_app_wiring_middleware_present() -> None:
    app = create_app()
    assert any(m.cls.__name__ == "QueueDebugMiddleware" for m in app.user_middleware)


def test_middleware_tracks_in_flight_request(client: TestClient) -> None:
    # While a request is executing, the probe must already list it (that is
    # the whole point: see what is queued behind the starved loop).
    @client.app.post("/track-check")
    async def track_check():
        paths = [r.path for r in queue_debug._STATE["inflight"].values()]
        return {"inflight_paths": paths}

    resp = client.post("/track-check", headers={"X-Probe-Tag": "yes"})
    assert resp.status_code == 200
    assert resp.json()["inflight_paths"] == ["/track-check"]

    # After completion the request must be gone again.
    assert client.post("/track-check").json()["inflight_paths"] == ["/track-check"]
    assert queue_debug._STATE["inflight"] == {}


def test_snapshot_redacts_credentials(client: TestClient) -> None:
    req = queue_debug.InFlightRequest(
        rid="abc",
        method="POST",
        path="/dashboard/api/threads/x/history",
        headers=(
            ("host", "localhost:8001"),
            ("cookie", "osw_session=SECRET"),
            ("authorization", "Bearer SECRET"),
            ("x-custom", "keepme"),
        ),
        started=0.0,
    )
    queue_debug._STATE["inflight"]["abc"] = req
    try:
        snap = queue_debug.snapshot()
        assert snap["in_flight"] == 1
        hdrs = {h["name"]: h["value"] for h in snap["requests"][0]["headers"]}
        assert hdrs["cookie"] == "<redacted>"
        assert hdrs["authorization"] == "<redacted>"
        assert hdrs["x-custom"] == "keepme"
    finally:
        queue_debug._STATE["inflight"].pop("abc", None)


def test_report_reads_loop_ready_queue(caplog: pytest.LogCaptureFixture) -> None:
    import time

    req = queue_debug.InFlightRequest(
        rid="abc",
        method="POST",
        path="/threads/x/history",
        headers=(("host", "localhost:8001"), ("cookie", "osw_session=SECRETVAL")),
        started=time.monotonic() - 2.0,
    )
    queue_debug._STATE["inflight"]["abc"] = req
    from collections import deque

    fake_loop = SimpleNamespace(_ready=deque(range(7)))
    try:
        ready = queue_debug._loop_ready_len(fake_loop)
        assert ready == 7
        with caplog.at_level("INFO", logger="open_swe.queue_debug"):
            queue_debug._report(time.monotonic(), ready, lag=12)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "loop_ready=7" in text
        assert "lag_ms=12" in text
        assert "/threads/x/history" in text
        # Cookie value must never appear in logs.
        assert "SECRETVAL" not in text
        assert "cookie:<redacted>" in text
    finally:
        queue_debug._STATE["inflight"].pop("abc", None)


def test_enable_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    queue_debug.set_enabled(False)
    monkeypatch.setenv(queue_debug.PROBE_ENV, "1")
    queue_debug.enable_from_env()
    assert queue_debug.is_enabled() is True
    queue_debug.set_enabled(False)


def test_set_enabled_without_running_loop_is_safe() -> None:
    # Outside an event loop the monitor thread cannot start; enabling must
    # still flip the flag and not raise.
    queue_debug.set_enabled(True)
    try:
        assert queue_debug.is_enabled() is True
    finally:
        queue_debug.set_enabled(False)


def test_ensure_logging_attaches_handler_when_root_bare() -> None:
    """uvicorn leaves the root logger unconfigured, so the probe must attach
    its own handler or INFO records vanish into the last-resort handler."""
    probe_logger = logging.getLogger(queue_debug.logger.name)
    # Simulate uvicorn's bare configuration.
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    probe_logger.handlers.clear()
    try:
        queue_debug._ensure_logging()
        assert probe_logger.handlers, "probe logger must get its own handler"
        assert probe_logger.level == logging.INFO
        # Idempotent: calling again must not stack handlers.
        n = len(probe_logger.handlers)
        queue_debug._ensure_logging()
        assert len(probe_logger.handlers) == n
    finally:
        probe_logger.handlers.clear()


def test_env_enable_starts_monitor_on_first_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The env switch flips the flag at import time (no loop yet); the
    monitor thread must start lazily on the first handled request."""
    import time

    monkeypatch.setenv(queue_debug.PROBE_ENV, "1")
    queue_debug.enable_from_env()
    assert queue_debug.is_enabled() is True
    # No loop is running on this thread yet, so nothing has started.
    thread = queue_debug._STATE["thread"]
    assert thread is None or not thread.is_alive()

    # A context-managed TestClient keeps its portal loop open between
    # requests, so the monitor thread (bound to that loop) keeps sampling.
    app = create_app()
    app.dependency_overrides[oauth.require_session] = lambda: {"sub": "octocat"}
    with TestClient(app) as c:
        resp = c.get("/health")
        assert resp.status_code == 200
        # First request ran inside a loop -> monitor now alive.
        thread = queue_debug._STATE["thread"]
        assert thread is not None and thread.is_alive()
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline and queue_debug.snapshot()["sample_count"] < 2:
            time.sleep(0.1)
        assert queue_debug.snapshot()["sample_count"] >= 2

    queue_debug.set_enabled(False)
