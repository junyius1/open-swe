"""Persistence tests for the per-user local-LLM reasoning-stream toggle."""

from unittest.mock import AsyncMock, patch

import pytest

from agent.dashboard.agent_overrides import profile_reasoning_stream
from agent.dashboard.profiles import ProfileUpdate, upsert_profile


def _update(**overrides: object) -> ProfileUpdate:
    base: dict[object] = {"default_model": "openai:gpt-5.6-sol", "reasoning_effort": "medium"}
    base.update(overrides)
    return ProfileUpdate(**base)  # type: ignore[arg-type]


def test_profile_reasoning_stream_reads_tristate() -> None:
    assert profile_reasoning_stream({"reasoning_stream": True}) is True
    assert profile_reasoning_stream({"reasoning_stream": False}) is False
    # unset (legacy profiles) -> None -> inherits the env deployment default
    assert profile_reasoning_stream({}) is None
    assert profile_reasoning_stream(None) is None
    assert profile_reasoning_stream({"reasoning_stream": "on"}) is None


@pytest.mark.asyncio
async def test_explicit_reasoning_stream_is_persisted() -> None:
    put_item = AsyncMock()
    with (
        patch(
            "agent.dashboard.profiles.get_profile",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("agent.dashboard.profiles._client") as client,
    ):
        client.return_value.store.put_item = put_item
        profile = await upsert_profile(
            "octocat", "octocat@example.com", _update(reasoning_stream=False)
        )

    assert profile["reasoning_stream"] is False
    assert put_item.await_args is not None
    assert put_item.await_args.args[2]["reasoning_stream"] is False


@pytest.mark.asyncio
async def test_unset_reasoning_stream_persists_none() -> None:
    put_item = AsyncMock()
    with (
        patch(
            "agent.dashboard.profiles.get_profile",
            new_callable=AsyncMock,
            return_value={"reasoning_stream": False},
        ),
        patch("agent.dashboard.profiles._client") as client,
    ):
        client.return_value.store.put_item = put_item
        profile = await upsert_profile("octocat", "octocat@example.com", _update())

    # omitting the field means "back to inheriting the deployment default"
    assert profile["reasoning_stream"] is None
    assert put_item.await_args is not None
    assert put_item.await_args.args[2]["reasoning_stream"] is None
