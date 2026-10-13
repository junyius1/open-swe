"""Monkeypatch for langgraph_runtime_inmem: coerce cron ``end_time`` to ``datetime``.

Background: the HTTP API receives cron ``end_time`` as an ISO-8601 string (the
OpenAPI schema declares ``format: date-time`` but jsonschema validation does not
coerce it), and ``langgraph_api`` passes that string straight through to
``Crons.put``/``Crons.update``. The in-memory runtime stores the value as-is
whereas the gRPC/postgres runtime parses it, so the inmem store ends up with a
string ``end_time``. ``Crons.next`` then compares ``cron["end_time"] < now`` on
every scheduler tick, raising ``TypeError: '<' not supported between instances
of 'str' and 'datetime.datetime'``. The exception aborts the whole
``cron_scheduler`` loop, so *no* crons ever fire while a poisoned entry exists.

``install_cron_end_time_patch`` wraps the inmem ``Crons.put``/``Crons.update``
write paths (root cause) and ``Crons.next`` (defensive read path, which also
repairs already-persisted string values on disk). It is a no-op on
gRPC/postgres runtimes. Upstream is still affected as of
``langgraph-runtime-inmem`` 0.36.0 (unpatched upstream at time of writing).
"""

import inspect
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

_installed = False


def _coerce_end_time(value: Any) -> Any:
    """Parse an ISO-8601 ``end_time`` into a ``datetime``; pass through otherwise."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            logger.warning(
                "Ignoring unparseable cron end_time %r (expected ISO-8601)",
                value,
            )
            return None
    return value


def _coerce_cron_store(store: Any) -> None:
    """Repair string ``end_time`` values already persisted in an inmem store."""
    if isinstance(store, dict):
        crons = store.get("crons")
    else:
        crons = getattr(store, "crons", None)
    if not isinstance(crons, list):
        return
    for cron in crons:
        if isinstance(cron, dict) and isinstance(cron.get("end_time"), str):
            cron["end_time"] = _coerce_end_time(cron["end_time"])


def install_cron_end_time_patch() -> None:
    """Coerce cron ``end_time`` to ``datetime`` on the inmem runtime write paths."""
    global _installed
    if _installed:
        return
    try:
        from langgraph_runtime.ops import Crons
        from langgraph_runtime_inmem.ops import Crons as InMemCrons
    except ImportError:
        # Non-inmem deployment (gRPC/postgres) already parses datetimes.
        return
    if InMemCrons is not Crons:
        return
    if getattr(Crons, "_open_swe_cron_end_time_patched", False):
        _installed = True
        return

    original_put = Crons.put
    original_update = Crons.update
    original_next = Crons.next

    async def _iter_original(result: Any) -> AsyncIterator[Any]:
        # Some import chains wrap the ops methods so the call returns a
        # coroutine that resolves to the iterator; tolerate both shapes.
        if inspect.iscoroutine(result):
            result = await result
        async for item in result:
            yield item

    async def _patched_put(conn: Any, /, **kwargs: Any) -> Any:
        kwargs["end_time"] = _coerce_end_time(kwargs.get("end_time"))
        async for item in _iter_original(original_put(conn, **kwargs)):
            yield item

    async def _patched_update(conn: Any, /, **kwargs: Any) -> Any:
        if kwargs.get("end_time") is not None:
            kwargs["end_time"] = _coerce_end_time(kwargs["end_time"])
        async for item in _iter_original(original_update(conn, **kwargs)):
            yield item

    async def _patched_next(conn: Any, *args: Any, **kwargs: Any) -> Any:
        _coerce_cron_store(getattr(conn, "store", None))
        async for item in _iter_original(original_next(conn, *args, **kwargs)):
            yield item

    Crons.put = staticmethod(_patched_put)
    Crons.update = staticmethod(_patched_update)
    Crons.next = staticmethod(_patched_next)
    Crons._open_swe_cron_end_time_patched = True  # type: ignore[attr-defined]
    _installed = True
    logger.info("Patched inmem Crons.put/update/next to coerce cron end_time to datetime")
