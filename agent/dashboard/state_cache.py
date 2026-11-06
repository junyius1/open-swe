"""In-process cache of the latest thread state snapshot.

Keyed by (thread_id, state version). The version is the runtime's
``state_updated_at`` (Postgres) or ``updated_at`` (inmem dev runtime) taken
from the fast ``GET /threads/{id}`` record -- state on an idle thread only
changes when a run writes a checkpoint, which always moves that timestamp.
That makes a hit structurally identical to a live ``/state`` read, and no
hit serves state to a thread with a run in flight: callers gate the read on
idle status and a run start / thread delete invalidates the entry.

A hit never queues work on the runtime's single-worker loop (which is what
stalls ``/state`` behind the active run's checkpoint I/O), so idle refreshes
serve from this process's memory instead.
"""

import json
import logging
import os
import time
from collections import OrderedDict
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_CACHE: OrderedDict[str, tuple[dict[str, Any], str, float]] = OrderedDict()
_MAX_ENTRIES = int(os.environ.get("DASHBOARD_STATE_CACHE_MAX_ENTRIES", "256"))
_MAX_ENTRY_BYTES = int(
    os.environ.get("DASHBOARD_STATE_CACHE_MAX_ENTRY_BYTES", str(32 * 1024 * 1024))
)
# Tombstones expire after one heartbeat. A run start already invalidates
# synchronously, so the only staleness window a tombstone covers is an
# out-of-band state write (Slack/Linear/webhook) that this process missed.
_INVALIDATE_AGE_SECONDS = 60.0
_TOMBSTONE_VERSION = "__invalidated__"
_stats: dict[str, int] = {
    "hits": 0,
    "misses": 0,
    "invalidations": 0,
    "evictions": 0,
    "oversize_skips": 0,
}


def _now() -> float:
    return time.monotonic()


def _prune_stale_invalidation(now: float) -> None:
    for thread_id, (_, _, age) in list(_CACHE.items()):
        if now - age > _INVALIDATE_AGE_SECONDS:
            _CACHE.pop(thread_id, None)


def get(thread_id: str, version: str | None) -> dict[str, Any] | None:
    """Return the cached snapshot for ``thread_id`` if it matches ``version``."""
    if not version or version == _TOMBSTONE_VERSION:
        _stats["misses"] += 1
        return None
    entry = _CACHE.get(thread_id)
    if entry is None or entry[1] != version:
        _stats["misses"] += 1
        return None
    _CACHE.move_to_end(thread_id)
    _stats["hits"] += 1
    return entry[0]


def _put(thread_id: str, version: str, state: dict[str, Any]) -> None:
    try:
        size = len(json.dumps(state, default=str).encode())
    except Exception:  # noqa: BLE001
        size = 0
    if size > 0 and 0 < _MAX_ENTRY_BYTES <= size:
        _stats["oversize_skips"] += 1
        return
    _CACHE[thread_id] = (state, version, _now())
    _CACHE.move_to_end(thread_id)
    while len(_CACHE) > _MAX_ENTRIES:
        _CACHE.popitem(last=False)
        _stats["evictions"] += 1


def put(thread_id: str, version: str | None, state: dict[str, Any]) -> None:
    if not version:
        return
    _put(thread_id, version, state)


def invalidate(thread_id: str) -> None:
    """Drop the entry if fresh; a short-lived tombstone marks the thread dirty."""
    _prune_stale_invalidation(_now())
    if thread_id in _CACHE:
        _CACHE.pop(thread_id, None)
        _stats["invalidations"] += 1
    else:
        _put(thread_id, _TOMBSTONE_VERSION, {})


def stats() -> dict[str, int]:
    return {**_stats, "entries": len(_CACHE)}


def clear() -> None:
    _CACHE.clear()
    for key in _stats:
        _stats[key] = 0


def parse_state_updated_at(value: Any) -> str | None:
    """Normalize a runtime ``state_updated_at``/``updated_at`` to a version string."""
    if isinstance(value, str) and value:
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return datetime.fromtimestamp(value, tz=UTC).isoformat()
    return None
