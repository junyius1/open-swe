from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent.utils import thread_ops


class _FakeThreads:
    def __init__(self, thread: Any | Exception) -> None:
        self._thread = thread
        self.get = AsyncMock(
            side_effect=thread if isinstance(thread, Exception) else lambda *_a: thread
        )


class _FakeRuns:
    def __init__(self, runs_by_status: dict[str, list[dict[str, Any]]]) -> None:
        self._runs_by_status = runs_by_status

    async def list(self, thread_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        return self._runs_by_status.get(kwargs.get("status") or "", [])


class _FakeClient:
    def __init__(
        self, thread: Any | Exception, runs_by_status: dict[str, list[dict[str, Any]]]
    ) -> None:
        self.threads = _FakeThreads(thread)
        self.runs = _FakeRuns(runs_by_status)


def _patch(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr(thread_ops, "langgraph_client", lambda: client)


@pytest.mark.asyncio
async def test_idle_thread_is_inactive_without_listing_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient({"thread_id": "t1", "status": "idle"}, {})
    _patch(monkeypatch, client)

    assert await thread_ops.get_thread_active_status("t1") is False
    client.threads.get.assert_awaited_once_with("t1")


@pytest.mark.asyncio
async def test_busy_thread_with_running_run_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        {"thread_id": "t1", "status": "busy"},
        {"pending": [], "running": [{"run_id": "r1"}]},
    )
    _patch(monkeypatch, client)

    assert await thread_ops.get_thread_active_status("t1") is True


@pytest.mark.asyncio
async def test_busy_thread_with_pending_run_is_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(
        {"thread_id": "t1", "status": "busy"},
        {"pending": [{"run_id": "r1"}], "running": []},
    )
    _patch(monkeypatch, client)

    assert await thread_ops.get_thread_active_status("t1") is True


@pytest.mark.asyncio
async def test_zombie_busy_thread_without_live_runs_is_inactive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # status says busy but no pending/running run exists (stale flag, e.g. a
    # running run was cancelled/killed without the worker recomputing status).
    client = _FakeClient({"thread_id": "t1", "status": "busy"}, {})
    _patch(monkeypatch, client)

    assert await thread_ops.get_thread_active_status("t1") is False


@pytest.mark.asyncio
async def test_thread_get_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient(RuntimeError("threads.get exploded"), {})
    _patch(monkeypatch, client)

    assert await thread_ops.get_thread_active_status("t1") is None


@pytest.mark.asyncio
async def test_runs_list_failure_on_busy_thread_is_conservatively_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeClient({"thread_id": "t1", "status": "busy"}, {})
    client.runs.list = AsyncMock(side_effect=RuntimeError("runs.list exploded"))
    _patch(monkeypatch, client)

    assert await thread_ops.get_thread_active_status("t1") is True
