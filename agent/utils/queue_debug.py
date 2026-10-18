"""Opt-in per-second probe of the single event loop that serves both agent
runs and HTTP requests.

The dashboard runtime runs runs and HTTP handlers on ONE pinned event loop
(``pin_single_event_loop`` + ``BG_JOB_ISOLATED_LOOPS=false``), so a
CPU-bound agent run starves every request that queues behind it. There is no
named ``asyncio.Queue`` holding those requests; the backlog lives in the loop's
ready queue plus the in-flight ASGI handlers blocked on it.

Enable per process with ``OPEN_SWE_QUEUE_DEBUG=1`` in the environment (e.g.
``.env``) and restart, or flip it at runtime on a specific process via
``POST /debug/queue-probe {"enabled": true}``.

Each second a monitor thread logs (it runs on its OWN thread, so it keeps
firing even while the loop is wedged by CPU work -- exactly the state to
inspect):
- ``lag_ms``       -- ms since the loop last ran our tick callback: the
  starvation gauge. Climb = the loop is busy with CPU work.
- ``loop_ready``   -- ``len(loop._ready)`` for the CPython loop (callbacks
  queued on the loop; ``-1`` on uvloop, whose ready queue is C-private).
- ``in_flight``    -- every HTTP request the process has accepted but not
  finished, with elapsed seconds and its request headers (credentials
  redacted); oldest first, so the head-of-line request is always on top.
"""

import asyncio
import logging
import os
import socket
import threading
import time
import uuid
from collections import deque as _ready_deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("open_swe.queue_debug")


def _ensure_logging() -> None:
    """Guarantee the probe's INFO records reach the console.

    The app runs with no central logging config, so uvicorn only configures
    its own loggers and the root stays bare. Without this, INFO records from
    this logger fall through to Python's last-resort handler (WARNING+) and
    silently vanish. Add a stderr handler only when none is already effective,
    so a real logging config elsewhere never double-logs.
    """
    if logger.handlers:
        logger.setLevel(logging.INFO)
        return
    if logger.propagate and _has_effective_handler(logger.parent):
        logger.setLevel(logging.INFO)
        return
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)


def _has_effective_handler(lg: logging.Logger) -> bool:
    cur: logging.Logger | None = lg
    while cur is not None:
        if cur.handlers:
            return True
        cur = cur.parent
    return False


# Request headers whose values are credentials; never dump these to logs.
_REDACTED_HEADERS = frozenset({"cookie", "authorization", "proxy-authorization", "x-api-key"})


def _safe_headers(headers: tuple[tuple[str, str], ...]) -> list[tuple[str, str]]:
    out = []
    for k, v in headers:
        out.append((k, "<redacted>" if k.lower() in _REDACTED_HEADERS else v))
    return out


PROBE_ENV = "OPEN_SWE_QUEUE_DEBUG"
_TRUTHY = {"1", "true", "yes", "on"}
_SAMPLE_SECONDS = 1.0
# The lag probe schedules a callback this far out and measures how late the
# loop actually ran it: a direct starvation gauge that works on both the
# CPython loop and uvloop.
_LAG_TARGET_SECONDS = 0.25
# Only dump per-request headers for the first N slowest entries; the rest are
# counted so a burst of retries does not turn the log into noise.
_HEADER_LIMIT = 5
# Requests shorter than this are noise for the probe's purpose.
_MIN_AGE_SECONDS = 0.5

_STATE: dict[str, Any] = {
    "enabled": False,
    "thread": None,
    "stop_event": None,
    "inflight": {},
    "sample_count": 0,
    "last_ready_peak": 0,
    "last_loop_tick": None,
}


@dataclass(frozen=True)
class InFlightRequest:
    rid: str
    method: str
    path: str
    headers: tuple[tuple[str, str], ...]
    started: float


def _enabled() -> bool:
    return bool(_STATE["enabled"])


def is_enabled() -> bool:
    return _enabled()


def set_enabled(enabled: bool) -> None:
    was = _STATE["enabled"]
    _STATE["enabled"] = bool(enabled)
    if _STATE["enabled"] and not was:
        _ensure_logging()
        _start_thread()
    if not _STATE["enabled"] and was:
        _stop_thread()


def snapshot(max_requests: int = 50) -> dict[str, Any]:
    """Current in-flight request table (for the /snapshot endpoint)."""
    now = time.monotonic()
    entries = []
    for req in list(_STATE["inflight"].values()):
        age = now - req.started
        if age < _MIN_AGE_SECONDS and len(_STATE["inflight"]) > 1:
            continue
        entries.append(
            {
                "id": req.rid,
                "method": req.method,
                "path": req.path,
                "age_seconds": round(age, 3),
                "headers": [{"name": k, "value": v} for k, v in _safe_headers(req.headers)],
            }
        )
    entries.sort(key=lambda e: e["age_seconds"], reverse=True)
    return {
        "enabled": _enabled(),
        "in_flight": len(_STATE["inflight"]),
        "sample_count": _STATE["sample_count"],
        "loop_ready_peak": _STATE["last_ready_peak"],
        "lag_ms": _tick_lag_ms(),
        "requests": entries[:max_requests],
    }


def _loop_ready_len(loop: asyncio.AbstractEventLoop) -> int:
    """``len(loop._ready)`` for the CPython loop -- readable from another
    thread: deque len() is an atomic GIL-protected op, so this stays live
    even while the loop is starved by CPU-bound work on the main thread.

    uvloop's ready queue is a C-private ``col_deque`` that is not readable
    from Python, so return ``-1`` (unknown) for any non-CPython loop; the
    in-flight request table then carries the signal."""
    ready = getattr(loop, "_ready", None)
    if not isinstance(ready, _ready_deque):
        return -1
    try:
        return len(ready)
    except TypeError:
        return -1


def _start_lag_probe(loop: asyncio.AbstractEventLoop) -> None:
    """Start a tick-stamp gauge on ``loop``: a periodic callback stamps
    ``_STATE["last_loop_tick"]`` every time the loop actually runs it.

    The monitor thread (a different thread) reads that stamp and computes
    ``lag_ms = now - last_tick``. While the loop is wedged by CPU work the
    callback cannot fire, so ``lag_ms`` climbs *live* -- the starvation
    signal. Loop-portable (CPython and uvloop alike)."""
    _STATE["last_loop_tick"] = time.monotonic()

    def _tick() -> None:
        _STATE["last_loop_tick"] = time.monotonic()
        loop.call_later(_LAG_TARGET_SECONDS, _tick)

    loop.call_later(_LAG_TARGET_SECONDS, _tick)


def _tick_lag_ms() -> int:
    """Milliseconds since the loop last ran our tick callback.

    ``-1`` means no probe is running. While the loop is wedged by CPU work
    the callback cannot fire, so this climbs live -- the starvation signal."""
    tick = _STATE.get("last_loop_tick")
    if tick is None:
        return -1
    return int(max(0.0, time.monotonic() - tick) * 1000.0)


def _report(now: float, ready: int, lag: int) -> None:
    inflight = _STATE["inflight"]
    _STATE["sample_count"] += 1
    _STATE["last_ready_peak"] = max(_STATE["last_ready_peak"], ready)
    ages = sorted(
        ((now - req.started, req) for req in list(inflight.values())),
        reverse=True,
    )
    n = len(ages)
    oldest = ages[0][0] if ages else 0.0
    logger.info(
        "queue-probe loop_ready=%d lag_ms=%d in_flight=%d oldest_age=%.1fs",
        ready,
        lag,
        n,
        oldest,
    )
    for i, (age, req) in enumerate(ages[:_HEADER_LIMIT]):
        header_txt = " ".join(f"{k}:{v}" for k, v in _safe_headers(req.headers))
        logger.info(
            "queue-probe   #%d age=%.1fs %s %s | %s",
            i + 1,
            age,
            req.method,
            req.path,
            header_txt,
        )
    if n > _HEADER_LIMIT:
        logger.info("queue-probe   ... %d more in flight (no headers)", n - _HEADER_LIMIT)


def _monitor_run(stop: threading.Event, loop: asyncio.AbstractEventLoop) -> None:
    while not stop.is_set():
        if stop.wait(_SAMPLE_SECONDS):
            return
        if not _enabled():
            continue
        if loop.is_closed():
            return
        _report(time.monotonic(), _loop_ready_len(loop), _tick_lag_ms())


def _start_thread() -> None:
    if _STATE["thread"] is not None and _STATE["thread"].is_alive():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("queue-probe: no running loop; cannot start monitor")
        return
    stop = threading.Event()
    t = threading.Thread(
        target=_monitor_run, args=(stop, loop), name="queue-debug-monitor", daemon=True
    )
    _STATE["stop_event"] = stop
    _STATE["thread"] = t
    _start_lag_probe(loop)
    t.start()


def _stop_thread() -> None:
    stop = _STATE["stop_event"]
    _STATE["thread"] = None
    _STATE["stop_event"] = None
    _STATE["last_loop_tick"] = None
    if stop is not None:
        stop.set()


def _ensure_thread_started() -> None:
    """Lazily (re)start the monitor once a running loop exists.

    ``enable_from_env()`` runs at import time, before any event loop exists,
    so the thread start deferred there must be retried from inside the loop.
    """
    if not _enabled():
        return
    if _STATE["thread"] is not None and _STATE["thread"].is_alive():
        return
    _start_thread()


def enable_from_env() -> None:
    value = os.environ.get(PROBE_ENV, "").strip().lower()
    if value in _TRUTHY:
        set_enabled(True)


class QueueDebugMiddleware:
    """ASGI middleware: register each accepted HTTP request in the in-flight
    table (with its request headers) and remove it on completion."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        _ensure_thread_started()
        rid = uuid.uuid4().hex[:8]
        method = str(scope.get("method") or "")
        path = str(scope.get("path") or "")
        host = ""
        headers: list[tuple[str, str]] = []
        for raw_k, raw_v in scope.get("headers") or []:
            try:
                k = raw_k.decode("latin-1")
                v = raw_v.decode("latin-1")
            except Exception:  # noqa: BLE001
                continue
            if k == "host":
                host = v
            headers.append((k, v))
        if not host:
            headers.append((":server", socket.gethostname()))
        req = InFlightRequest(rid, method, path, tuple(headers), time.monotonic())
        table = _STATE["inflight"]
        table[rid] = req
        try:
            await self.app(scope, receive, send)
        finally:
            table.pop(rid, None)
            if _enabled():
                elapsed = time.monotonic() - req.started
                if elapsed >= _MIN_AGE_SECONDS:
                    logger.info("queue-probe done age=%.1fs %s %s", elapsed, method, path)
