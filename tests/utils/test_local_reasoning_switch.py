"""Tests for the local-LLM reasoning-stream switch in ``make_model``.

One switch (``reasoning_stream``, the per-user profile setting) gates both
halves of the local-LLM reasoning pipeline:

* the ``enable_thinking`` request field sent to the LLM (whether it generates
  thinking at all — the "pull"), and
* the ``_open_swe_local_reasoning`` instance marker (whether thinking is
  captured and streamed to the dashboard as reasoning events — the
  "display/push").

``None`` inherits the ``LOCAL_LLM_ENABLE_THINKING`` deployment default (on),
mirroring the ``use_gateway`` tri-state resolution.
"""

from agent.utils import model as model_utils


class _FakeLocalModel:
    """Stand-in for the ChatOpenAI instance: accepts the marker attribute."""


def _fake_init(captured: dict):
    def _init(model, **kwargs):
        captured["model"] = model
        captured.update(kwargs)
        instance = _FakeLocalModel()
        captured["instance"] = instance
        return instance

    return _init


async def _run_make_model(monkeypatch, *, reasoning_stream: bool | None) -> dict:
    captured: dict = {}
    monkeypatch.setattr(model_utils, "init_chat_model", _fake_init(captured))
    # Fixed window override avoids the async GET /v1/models read entirely.
    monkeypatch.setenv("LOCAL_LLM_MAX_INPUT_TOKENS", "100000")
    try:
        await model_utils.make_model(
            "local:/mnt/D/models/Qwen3.8-27B",
            use_gateway=False,
            reasoning_stream=reasoning_stream,
            max_tokens=64_000,
        )
    finally:
        model_utils._MODEL_CACHE.clear()
        model_utils._LOCAL_LLM_WINDOW_CACHE.clear()
    return captured


def test_resolver_none_inherits_env_default() -> None:
    assert model_utils.resolve_local_reasoning_stream(None) is True
    assert model_utils.resolve_local_reasoning_stream(True) is True
    assert model_utils.resolve_local_reasoning_stream(False) is False


def test_resolver_env_false_inherited_when_unset(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_ENABLE_THINKING", "false")
    assert model_utils.resolve_local_reasoning_stream(None) is False
    # explicit setting still wins over the env default
    assert model_utils.resolve_local_reasoning_stream(True) is True


async def test_switch_on_keeps_thinking_and_marker(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_LLM_ENABLE_THINKING", raising=False)
    captured = await _run_make_model(monkeypatch, reasoning_stream=True)
    extra_body = captured["extra_body"]
    assert extra_body["enable_thinking"] is True
    assert extra_body["chat_template_kwargs"]["enable_thinking"] is True
    # the instance carries the marker so the reasoning patch captures its chunks
    assert captured["instance"]._open_swe_local_reasoning is True


async def test_switch_off_disables_thinking_and_marker(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_LLM_ENABLE_THINKING", raising=False)
    captured = await _run_make_model(monkeypatch, reasoning_stream=False)
    extra_body = captured["extra_body"]
    assert extra_body["enable_thinking"] is False
    assert extra_body["chat_template_kwargs"]["enable_thinking"] is False
    # no marker: the reasoning patch no-ops, so no reasoning events are emitted
    assert not hasattr(captured["instance"], "_open_swe_local_reasoning")


async def test_switch_off_wins_over_env_true(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_ENABLE_THINKING", "true")
    captured = await _run_make_model(monkeypatch, reasoning_stream=False)
    assert captured["extra_body"]["enable_thinking"] is False


async def test_unset_switch_inherits_env_true(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_ENABLE_THINKING", "true")
    captured = await _run_make_model(monkeypatch, reasoning_stream=None)
    assert captured["extra_body"]["enable_thinking"] is True


async def test_unset_switch_inherits_env_false(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_ENABLE_THINKING", "false")
    captured = await _run_make_model(monkeypatch, reasoning_stream=None)
    assert captured["extra_body"]["enable_thinking"] is False
