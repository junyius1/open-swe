"""Lifecycle tests for the dashboard /stream/events proxy.

Covers the upstream-subscription recycling added to
``_stream_thread_events``: client-disconnect detection, idle read timeout,
and max total lifetime -- the three guards that stop orphaned LangGraph
subscriptions from piling up when a dashboard client reconnects without
closing its old stream. Also covers the per-thread stream/events
aggregation in the queue probe.
"""

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from agent.dashboard import thread_api
from agent.utils import queue_debug


class _FakeResponse:
    def __init__(self, aiter: AsyncIterator[bytes], status_code: int = 200) -> None:
        self._aiter = aiter
        self.status_code = status_code
        self.reason_phrase = ""

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        return self._aiter

    async def aread(self) -> bytes:
        return b""


class _FakeStreamContext:
    def __init__(self, response: _FakeResponse) -> None:
        self._response = response
        self.closed = False

    async def __aenter__(self) -> _FakeResponse:
        return self._response

    async def __aexit__(self, *exc: object) -> None:
        self.closed = True


class _FakeUpstream:
    def __init__(self, aiter: AsyncIterator[bytes], status_code: int = 200) -> None:
        self.ctx = _FakeStreamContext(_FakeResponse(aiter, status_code))

    @property
    def closed(self) -> bool:
        return self.ctx.closed


class _FakeAsyncClient:
    def __init__(self, upstream: _FakeUpstream, *args: object, **kwargs: object) -> None:
        self._upstream = upstream

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def stream(self, method: str, url: str, **kwargs: object) -> _FakeStreamContext:
        return self._upstream.ctx


@pytest.fixture
def fake_client(monkeypatch: pytest.MonkeyPatch):
    def _install(aiter: AsyncIterator[bytes], status_code: int = 200) -> _FakeUpstream:
        upstream = _FakeUpstream(aiter, status_code)
        monkeypatch.setattr(
            thread_api.httpx,
            "AsyncClient",
            lambda *a, **k: _FakeAsyncClient(upstream, *a, **k),
        )
        return upstream

    return _install


def _drain(gen: AsyncIterator[bytes], limit: float = 5.0) -> list[bytes]:
    async def _run() -> list[bytes]:
        out: list[bytes] = []
        async for chunk in gen:
            out.append(chunk)
        return out

    async def _main() -> list[bytes]:
        return await asyncio.wait_for(_run(), timeout=limit)

    return asyncio.run(_main())


def _blocking_aiter(
    first: bytes | None = None, block_seconds: float = 3600.0
) -> AsyncIterator[bytes]:
    async def _gen() -> AsyncIterator[bytes]:
        if first is not None:
            yield first
        await asyncio.sleep(block_seconds)

    return _gen()


async def _disconnect_receive() -> dict[str, str]:
    return {"type": "http.disconnect"}


def test_pass_through_yields_all_chunks_and_closes(fake_client) -> None:
    async def _gen() -> AsyncIterator[bytes]:
        yield b"a"
        yield b"b"

    fake_client(_gen())
    chunks = _drain(thread_api._stream_thread_events("tid", b"{}", "application/json"))
    assert b"".join(chunks) == b"ab"


def test_idle_timeout_recycles_silent_upstream(
    fake_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(thread_api._STREAM_IDLE_TIMEOUT_ENV, "0.2")
    monkeypatch.setenv(thread_api._STREAM_MAX_LIFETIME_ENV, "0")
    upstream = fake_client(_blocking_aiter(first=b"one"))
    started = time.monotonic()
    chunks = _drain(thread_api._stream_thread_events("tid", b"{}", "application/json"))
    elapsed = time.monotonic() - started
    assert chunks == [b"one"]
    assert upstream.closed
    assert elapsed < 5.0


def test_client_disconnect_recycles_upstream(fake_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(thread_api._STREAM_IDLE_TIMEOUT_ENV, "0")
    monkeypatch.setenv(thread_api._STREAM_MAX_LIFETIME_ENV, "0")
    upstream = fake_client(_blocking_aiter())
    started = time.monotonic()
    chunks = _drain(
        thread_api._stream_thread_events(
            "tid", b"{}", "application/json", receive=_disconnect_receive
        )
    )
    elapsed = time.monotonic() - started
    assert chunks == []
    assert upstream.closed
    assert elapsed < 5.0


def test_max_lifetime_recycles_live_upstream(fake_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(thread_api._STREAM_IDLE_TIMEOUT_ENV, "0")
    monkeypatch.setenv(thread_api._STREAM_MAX_LIFETIME_ENV, "0.2")

    async def _gen() -> AsyncIterator[bytes]:
        for _ in range(1000):
            yield b"x"
            await asyncio.sleep(0.05)

    upstream = fake_client(_gen())
    started = time.monotonic()
    chunks = _drain(thread_api._stream_thread_events("tid", b"{}", "application/json"))
    elapsed = time.monotonic() - started
    assert len(chunks) >= 1
    assert upstream.closed
    assert 0.15 < elapsed < 5.0


async def test_proxy_passes_receive_through(fake_client, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(thread_api._STREAM_IDLE_TIMEOUT_ENV, "0")
    monkeypatch.setenv(thread_api._STREAM_MAX_LIFETIME_ENV, "0")

    async def _fake_metadata(*args: object, **kwargs: object) -> dict[str, object]:
        return {"id": "tid"}

    monkeypatch.setattr(thread_api, "_readable_thread_metadata", _fake_metadata)
    fake_client(_blocking_aiter())
    gen = await thread_api.proxy_dashboard_thread_stream_events(
        "tid", "octocat", b"{}", receive=_disconnect_receive
    )
    collected: list[bytes] = []
    async for chunk in gen:
        collected.append(chunk)
    assert collected == []


def test_stream_timeout_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(thread_api._STREAM_IDLE_TIMEOUT_ENV, raising=False)
    assert thread_api._stream_idle_timeout() == thread_api._STREAM_IDLE_TIMEOUT_DEFAULT
    monkeypatch.setenv(thread_api._STREAM_IDLE_TIMEOUT_ENV, "0")
    assert thread_api._stream_idle_timeout() == 0.0
    monkeypatch.setenv(thread_api._STREAM_IDLE_TIMEOUT_ENV, "12.5")
    assert thread_api._stream_idle_timeout() == 12.5
    monkeypatch.setenv(thread_api._STREAM_IDLE_TIMEOUT_ENV, "-3")
    assert thread_api._stream_idle_timeout() == thread_api._STREAM_IDLE_TIMEOUT_DEFAULT
    monkeypatch.setenv(thread_api._STREAM_IDLE_TIMEOUT_ENV, "bogus")
    assert thread_api._stream_idle_timeout() == thread_api._STREAM_IDLE_TIMEOUT_DEFAULT


def _probe_entry(
    rid: str, path: str, age: float, method: str = "POST"
) -> queue_debug.InFlightRequest:
    return queue_debug.InFlightRequest(
        rid=rid,
        method=method,
        path=path,
        headers=(("host", "localhost"),),
        started=time.monotonic() - age,
    )


@pytest.fixture
def probe_state():
    inflight: dict[str, object] = {}
    peaks: dict[str, int] = {}
    queue_debug._STATE["inflight"] = inflight
    queue_debug._STATE["stream_events_peak"] = peaks
    yield inflight, peaks
    queue_debug._STATE["inflight"] = {}
    queue_debug._STATE["stream_events_peak"] = {}


def test_stream_events_aggregation_counts_per_thread(probe_state) -> None:
    inflight, _peaks = probe_state
    now = time.monotonic()
    inflight["a"] = _probe_entry("a", "/threads/tid1/stream/events", 3.0)
    inflight["b"] = _probe_entry("b", "/threads/tid1/stream/events", 1.0)
    inflight["c"] = _probe_entry("c", "/threads/tid2/stream/events", 0.4)
    inflight["d"] = _probe_entry("d", "/threads/tid1/history", 9.0)
    inflight["e"] = _probe_entry("e", "/threads/tid1/stream/events", 2.0, method="GET")
    by_thread = queue_debug._stream_events_by_thread(now)
    assert by_thread == {
        "tid1": {"count": 2, "oldest_seconds": pytest.approx(3.0, abs=0.5)},
        "tid2": {"count": 1, "oldest_seconds": pytest.approx(0.4, abs=0.5)},
    }


def test_report_logs_stream_events_line(probe_state, caplog: pytest.LogCaptureFixture) -> None:
    inflight, peaks = probe_state
    inflight["a"] = _probe_entry("a", "/threads/tid1/stream/events", 3.0)
    inflight["b"] = _probe_entry("b", "/threads/tid1/stream/events", 1.0)
    inflight["c"] = _probe_entry("c", "/threads/tid2/stream/events", 0.6)
    with caplog.at_level("INFO", logger=queue_debug.logger.name):
        queue_debug._report(time.monotonic(), ready=0, lag=0)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "queue-probe stream/events: 3 sub(s) across 2 thread(s)" in text
    assert "tid1" in text and "peak 2" in text
    assert "tid2" in text and "peak 1" in text
    assert peaks == {"tid1": 2, "tid2": 1}
    # Second report with the count back down still remembers the peak.
    del queue_debug._STATE["inflight"]["b"]
    del queue_debug._STATE["inflight"]["c"]
    with caplog.at_level("INFO", logger=queue_debug.logger.name):
        queue_debug._report(time.monotonic(), ready=0, lag=0)
    text2 = "\n".join(r.getMessage() for r in caplog.records)
    assert "1 sub(s) across 1 thread(s)" in text2
    assert "peak 2" in text2
    assert peaks["tid1"] == 2


def test_snapshot_includes_stream_events(probe_state) -> None:
    inflight, _peaks = probe_state
    inflight["a"] = _probe_entry("a", "/threads/tid9/stream/events", 2.0)
    snap = queue_debug.snapshot()
    assert "stream_events" in snap
    assert snap["stream_events"]["tid9"]["count"] == 1
