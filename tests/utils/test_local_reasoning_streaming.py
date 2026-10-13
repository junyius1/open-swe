"""Regression test: local-LLM thinking tokens must reach the dashboard as reasoning blocks.

Background: the base ``ChatOpenAI`` drops provider-specific fields such as
``reasoning_content`` (documented at the top of its ``base.py``). OpenAI-compatible
servers (Qwen on vLLM, DashScope, …) carry the model's thinking in a field that is
``reasoning_content`` on some and plain ``reasoning`` on others, in both the
streaming delta and the non-streaming message. Without
``agent.utils.model.install_local_reasoning_patch`` those tokens are discarded
before they reach ``AIMessageChunk.content_blocks``, so the v3 streaming bridge
never emits ``reasoning-delta`` events and the dashboard shows only tool calls
plus the final answer.

The patch (streaming and non-streaming) stores the thinking text in
``additional_kwargs["reasoning_content"]`` and relabels
``response_metadata["model_provider"]`` to ``"deepseek"`` — the one label that
resolves to a reasoning-content-aware translator on both the Python and the
``@langchain/core`` JS sides.
"""

from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessageChunk

from agent.utils import model as model_utils


def _install_fresh() -> Any:
    """Force a clean install of the reasoning patch (mirrors a fresh process)."""
    import importlib

    from langchain_openai.chat_models import base as lo_base

    model_utils._local_reasoning_installed = False
    lo_base.__dict__.pop("_open_swe_reasoning_patched", None)
    importlib.reload(lo_base)
    model_utils.install_local_reasoning_patch()
    return lo_base


def _make_model() -> Any:
    from langchain_openai.chat_models import base as lo_base

    model = MagicMock(spec=lo_base.BaseChatOpenAI)
    model._open_swe_local_reasoning = True
    model.output_version = None
    model.model_name = "/mnt/D/models/Qwen3.8-27B"
    return model


def _stream_chunk(delta: dict, extra: dict | None = None) -> dict:
    chunk: dict[str, Any] = {
        "id": "chatcmpl-test",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "/mnt/D/models/Qwen3.8-27B",
        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
    }
    if extra:
        chunk.update(extra)
    return chunk


def test_delta_reasoning_text_reads_both_field_names() -> None:
    assert model_utils._delta_reasoning_text(_stream_chunk({"reasoning": "thinking"})) == "thinking"
    assert (
        model_utils._delta_reasoning_text(_stream_chunk({"reasoning_content": "thinking"}))
        == "thinking"
    )
    assert model_utils._delta_reasoning_text(_stream_chunk({"content": "answer"})) is None
    assert model_utils._delta_reasoning_text(_stream_chunk({"reasoning": ""})) is None
    assert model_utils._delta_reasoning_text({"choices": []}) is None


def test_streaming_chunk_carries_reasoning_block() -> None:
    _install_fresh()
    from langchain_openai.chat_models import base as lo_base

    model = _make_model()

    generation = lo_base.BaseChatOpenAI._convert_chunk_to_generation_chunk(
        model, _stream_chunk({"reasoning": "思考中"}), AIMessageChunk, None
    )
    assert generation is not None
    message = generation.message
    assert message.response_metadata["model_provider"] == "deepseek"
    assert message.additional_kwargs["reasoning_content"] == "思考中"
    blocks = cast("list[dict[str, Any]]", message.content_blocks)
    assert [block["type"] for block in blocks] == ["reasoning"]
    assert blocks[0]["reasoning"] == "思考中"

    # reasoning_content field name must work identically
    generation = lo_base.BaseChatOpenAI._convert_chunk_to_generation_chunk(
        model, _stream_chunk({"reasoning_content": "继续思考"}), AIMessageChunk, None
    )
    assert generation is not None
    assert generation.message.additional_kwargs["reasoning_content"] == "继续思考"

    # text chunks are left alone (no reasoning key, provider still relabeled)
    generation = lo_base.BaseChatOpenAI._convert_chunk_to_generation_chunk(
        model, _stream_chunk({"content": "答案"}), AIMessageChunk, None
    )
    assert generation is not None
    assert "reasoning_content" not in generation.message.additional_kwargs
    assert generation.message.response_metadata["model_provider"] == "deepseek"

    # unmarked models (non-local) must not be touched
    plain = _make_model()
    plain._open_swe_local_reasoning = False
    generation = lo_base.BaseChatOpenAI._convert_chunk_to_generation_chunk(
        plain, _stream_chunk({"reasoning": "别动我"}), AIMessageChunk, None
    )
    assert generation is not None
    assert generation.message.response_metadata["model_provider"] == "openai"
    assert "reasoning_content" not in generation.message.additional_kwargs


def test_streaming_reasoning_chunks_accumulate() -> None:
    _install_fresh()
    from langchain_openai.chat_models import base as lo_base

    model = _make_model()
    convert = lo_base.BaseChatOpenAI._convert_chunk_to_generation_chunk

    aggregated: AIMessageChunk | None = None
    for delta in ({"reasoning": "用"}, {"reasoning": "中文"}, {"content": "8"}):
        generated = convert(model, _stream_chunk(delta), AIMessageChunk, None)
        assert generated is not None
        chunk = cast("AIMessageChunk", generated.message)
        aggregated = chunk if aggregated is None else aggregated + chunk
    assert aggregated is not None
    blocks = cast("list[dict[str, Any]]", aggregated.content_blocks)
    reasoning = [block for block in blocks if block["type"] == "reasoning"]
    assert [block["reasoning"] for block in reasoning] == ["用中文"]
    assert aggregated.text == "8"


def _mock_response(message: dict, model: str = "/mnt/D/models/Qwen3.8-27B") -> Any:
    """An SDK-style (non-dict) ChatCompletion response with typed choices."""
    raw = {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "model": model,
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
    }
    choice = MagicMock()
    choice.message = MagicMock(**message)
    choice.finish_reason = "stop"
    response = MagicMock()
    response.model_dump.return_value = raw
    response.choices = [choice]
    response.model = model
    response.id = "chatcmpl-test"
    return response


def test_non_streaming_result_carries_reasoning_block() -> None:
    _install_fresh()
    from langchain_openai.chat_models import base as lo_base

    model = _make_model()

    response = _mock_response({"content": "8", "role": "assistant", "reasoning": "需要计算 4+4。"})
    result = lo_base.BaseChatOpenAI._create_chat_result(model, response, None)
    message = result.generations[0].message
    assert message.response_metadata["model_provider"] == "deepseek"
    assert message.response_metadata["model_name"] == "/mnt/D/models/Qwen3.8-27B"
    assert message.additional_kwargs["reasoning_content"] == "需要计算 4+4。"
    blocks = message.content_blocks
    assert [block["type"] for block in blocks] == ["reasoning", "text"]

    # dict-form responses (some code paths pass raw dicts) and the
    # reasoning_content field name must behave identically
    result = lo_base.BaseChatOpenAI._create_chat_result(
        model,
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": "/mnt/D/models/Qwen3.8-27B",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "8",
                        "reasoning_content": "dict 路径",
                    },
                }
            ],
        },
        None,
    )
    message = result.generations[0].message
    assert message.additional_kwargs["reasoning_content"] == "dict 路径"
    assert message.response_metadata["model_provider"] == "deepseek"

    # no thinking in the response -> nothing captured
    result = lo_base.BaseChatOpenAI._create_chat_result(
        model, _mock_response({"content": "8", "role": "assistant"}), None
    )
    assert "reasoning_content" not in result.generations[0].message.additional_kwargs


def test_make_model_local_model_is_marked() -> None:
    _install_fresh()
    from langchain_openai.chat_models import base as lo_base

    marker_seen: dict[str, Any] = {}

    def _spy_init(model: str, **kwargs: Any) -> Any:
        instance = MagicMock()
        marker_seen["model"] = model
        marker_seen["marked"] = getattr(instance, model_utils._LOCAL_REASONING_MARKER, None)
        return instance

    original = model_utils.init_chat_model
    model_utils.init_chat_model = _spy_init
    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.delenv("LOCAL_LLM_ENABLE_THINKING", raising=False)
            model_utils.make_model("local:/mnt/D/models/Qwen3.8-27B", use_gateway=False)
    finally:
        model_utils.init_chat_model = original

    assert marker_seen.get("model") == "/mnt/D/models/Qwen3.8-27B"
    assert getattr(lo_base, "_open_swe_reasoning_patched", False)
    assert model_utils._local_reasoning_installed
