"""Regression test: inmem cron ``end_time`` must survive the API's ISO-string round trip.

Background: ``POST /runs/crons`` receives ``end_time`` as an ISO-8601 string and
``langgraph_api`` passes it through to the runtime unmodified. The gRPC/postgres
runtime parses it, but ``langgraph_runtime_inmem.Crons.put`` stored the raw
string. ``Crons.next`` then raises ``TypeError: '<' not supported between
instances of 'str' and 'datetime.datetime'`` on every scheduler tick, aborting
the whole ``cron_scheduler`` loop so no crons ever fire.

``agent.utils.cron_endtime_patch.install_cron_end_time_patch`` wraps the write
paths (``Crons.put``/``Crons.update``) to coerce strings to ``datetime`` and the
read path (``Crons.next``) to repair already-persisted string values.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from langgraph_runtime.ops import Crons
from langgraph_runtime_inmem.ops import Crons as InMemCrons

from agent.utils.cron_endtime_patch import _coerce_end_time, install_cron_end_time_patch

install_cron_end_time_patch()


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def test_patch_is_idempotent_and_targets_inmem_crons() -> None:
    assert Crons is InMemCrons
    install_cron_end_time_patch()
    install_cron_end_time_patch()
    assert getattr(Crons, "_open_swe_cron_end_time_patched", False) is True


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-10-13T10:08:30+00:00", datetime(2026, 10, 13, 10, 8, 30, tzinfo=UTC)),
        ("2026-10-13T12:08:30+02:00", datetime(2026, 10, 13, 10, 8, 30, tzinfo=UTC)),
        (None, None),
        ("not-a-date", None),
    ],
)
def test_coerce_end_time(value, expected) -> None:
    result = _coerce_end_time(value)
    assert (result == expected) or (result is expected is None)


async def test_put_coerces_string_end_time() -> None:
    """The API hands ``end_time`` as an ISO string; the store must hold a datetime."""
    assistant_id = str(uuid.uuid4())
    conn = SimpleNamespace(
        store={
            "assistants": [{"assistant_id": assistant_id}],
            "crons": [],
            "threads": [],
        },
    )
    end_time = datetime.now(UTC) + timedelta(hours=1)
    kwargs = {
        "payload": {"assistant_id": assistant_id},
        "schedule": "* * * * *",
        "enabled": True,
        "end_time": _iso(end_time),
    }

    created = [cron async for cron in Crons.put(conn, **kwargs)]
    assert len(created) == 1
    stored = next(c for c in conn.store["crons"] if c["cron_id"] == created[0]["cron_id"])
    assert stored["end_time"] == end_time


def _seeded_store() -> dict:
    cron_id = uuid.uuid4()
    return {
        "crons": [
            {
                "cron_id": cron_id,
                "assistant_id": uuid.uuid4(),
                "thread_id": None,
                "user_id": None,
                # Legacy value persisted before the patch existed.
                "end_time": _iso(datetime.now(UTC) + timedelta(hours=1)),
                "schedule": "* * * * *",
                "timezone": None,
                "payload": {"assistant_id": "test-assistant"},
                "next_run_date": datetime.now(UTC),
                "metadata": {},
                "on_run_completed": None,
                "enabled": True,
                "created_at": datetime.now(UTC),
                "updated_at": datetime.now(UTC),
            }
        ],
        "assistants": [],
        "threads": [],
    }


async def test_next_repairs_legacy_string_end_time() -> None:
    """A store containing a string ``end_time`` must not break the scheduler."""
    conn = SimpleNamespace(store=_seeded_store())

    next_runs = [cron async for cron in Crons.next(conn)]

    stored = conn.store["crons"][0]
    assert isinstance(stored["end_time"], datetime)
    # Not yet due (next_run_date == now), so it is a candidate for firing.
    assert [cron["cron_id"] for cron in next_runs] == [stored["cron_id"]]


async def test_next_skips_expired_string_end_time_without_typeerror() -> None:
    """An expired cron with a legacy string ``end_time`` must be skipped, not crash."""
    store = _seeded_store()
    cron = store["crons"][0]
    cron["end_time"] = _iso(datetime.now(UTC) - timedelta(hours=1))
    conn = SimpleNamespace(store=store)

    next_runs = [cron async for cron in Crons.next(conn)]

    assert next_runs == []
    assert isinstance(cron["end_time"], datetime)
