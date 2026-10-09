"""Regression test: local-provider (vLLM) context-overflow errors must trigger the
deepagents summarization middleware's overflow-retry fallback instead of killing the
run with a raw openai.BadRequestError.

Background: langchain-openai only classifies a 400 as overflow when the provider
message contains one of a few exact phrases (``context_length_exceeded``,
``prompt is too long``, ``Input tokens exceed the configured limit``). vLLM reports
the failure differently, so the raw ``BadRequestError`` escaped the
``except ContextOverflowError`` handler in
``_DeepAgentsSummarizationMiddleware.awrap_model_call`` and the run died.

``agent.utils.model.install_context_overflow_error_patch`` wraps
``langchain_openai.chat_models.base._handle_openai_bad_request`` so that
OpenAI-compatible overflow wordings are re-raised as ``ContextOverflowError``.
"""

import asyncio
import json
import warnings
from unittest.mock import Mock

import pytest
from langchain_core.exceptions import ContextOverflowError
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_openai import ChatOpenAI
from openai import BadRequestError

from agent.utils import model as model_utils

# The exact message vLLM emits for this thread's failure.
VLLM_OVERFLOW_MESSAGE = (
    "This model's maximum context length is 262144 tokens. However, you requested 64000 output tokens and your "
    "prompt contains at least 198145 input tokens, for a total of at least 262145 tokens. Please reduce the length "
    "of the input prompt or the number of requested output tokens. (parameter=input_tokens, value=198145)"
)

SUMMARY_TEXT = "SUMMARY OF PRIOR CONVERSATION"
FINAL_TEXT = "FINAL ANSWER OK"


def _make_bad_request(message: str) -> BadRequestError:
    response = Mock()
    response.status_code = 400
    response.request = Mock()
    response.headers = {}
    return BadRequestError(
        message=message, response=response, body={"error": {"message": message, "code": 400}}
    )


def test_vllm_overflow_message_is_classified_as_context_overflow() -> None:
    model_utils.install_context_overflow_error_patch()
    from langchain_openai.chat_models import base as lo_base

    with pytest.raises(ContextOverflowError):
        lo_base._handle_openai_bad_request(_make_bad_request(VLLM_OVERFLOW_MESSAGE))


def test_unrelated_bad_request_is_not_reclassified() -> None:
    model_utils.install_context_overflow_error_patch()
    from langchain_openai.chat_models import base as lo_base

    with pytest.raises(BadRequestError):
        lo_base._handle_openai_bad_request(
            _make_bad_request("Invalid value for parameter temperature: -3")
        )


def test_local_model_build_installs_overflow_patch() -> None:
    """The local: branch of make_model returns early, before the shared tail.

    This is the exact regression from the reported bug: the install was placed at the
    shared tail, so local models never got the patch and their vLLM overflow escaped as
    a raw BadRequestError. Installing at the top of make_model covers every branch.
    """
    from unittest.mock import patch as _patch

    from langchain_openai.chat_models import base as lo_base

    # Simulate a fresh process: drop any prior install so the local: path must do it.
    model_utils._local_overflow_installed = False
    lo_base.__dict__.pop("_open_swe_overflow_patched", None)
    # Reset the wrapped handler to the pristine original if a prior test wrapped it, so
    # we observe a clean install from the local path.
    import importlib

    importlib.reload(lo_base)

    captured: dict[str, object] = {}

    def _fake_init(model: str, **kwargs: object) -> str:
        captured["model"] = model
        return "LOCAL_MODEL"

    with _patch.object(model_utils, "init_chat_model", _fake_init):
        model_utils.make_model(
            "local:/mnt/D/models/Qwen3.8-27B", use_gateway=False, max_tokens=64_000
        )

    assert captured.get("model") == "/mnt/D/models/Qwen3.8-27B"
    assert getattr(lo_base, "_open_swe_overflow_patched", False), (
        "local: model build must install the overflow patch (early-return path)"
    )
    assert model_utils._local_overflow_installed


def _build_overflowing_model(calls: list[str]) -> ChatOpenAI:
    """A profile-less ChatOpenAI that overflows on the first user-request call.

    Mirrors the production shape: no ``profile`` (so the fallback 170K trigger and the
    ``_aclip_overflow_tail`` None-budget path are exercised). On the first call it runs
    the raw vLLM overflow error through the real (patched) ``_handle_openai_bad_request``
    exactly as the real ``_agenerate`` does, so the test genuinely exercises the patch
    inside the full middleware flow.
    """

    class _OverflowingChatOpenAI(ChatOpenAI):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            first = messages[0] if messages else None
            calls.append(json.dumps(first.content, ensure_ascii=False) if first is not None else "")
            from langchain_core.outputs import ChatGeneration, ChatResult

            if len(calls) == 1:
                # The HTTP layer raised a raw BadRequestError; the real _agenerate routes
                # it through _handle_openai_bad_request, which the patch turns into a
                # ContextOverflowError.
                from langchain_openai.chat_models import base as lo_base

                lo_base._handle_openai_bad_request(_make_bad_request(VLLM_OVERFLOW_MESSAGE))
            if len(calls) == 2:
                return ChatResult(
                    generations=[ChatGeneration(message=AIMessage(content=SUMMARY_TEXT))]
                )
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=FINAL_TEXT))])

    return _OverflowingChatOpenAI(
        model="local-model", base_url="http://localhost:1/v1", api_key="dummy", max_tokens=64_000
    )


def _load_state_messages() -> list:
    """Deterministic dense message set whose approximate token count sits just under the
    170K fallback trigger: enough real tokens to overflow the provider, but low enough that
    the pre-emptive summarization does NOT fire and only the overflow-retry path can save
    the run (the exact production situation)."""
    filler = " ".join(f"exec-line-{j} data-payload-{j}" for j in range(60))
    raw: list[dict] = []
    for i in range(315):
        raw.append(
            {
                "type": "human",
                "content": f'<input-message sender="github:u" surface="web" kind="human">Task message {i}</input-message>',
            }
        )
        raw.append(
            {
                "type": "ai",
                "content": "",
                "tool_calls": [
                    {
                        "id": f"c{i}",
                        "name": "execute",
                        "args": {"command": f"make test-{i} && cat build-{i}.log | head -50"},
                    }
                ],
            }
        )
        raw.append({"type": "tool", "content": filler, "tool_call_id": f"c{i}"})
    return raw


def _to_langchain(raw: list[dict]):
    from langchain_core.messages import AIMessage as AI

    out = []
    for m in raw:
        t = m.get("type")
        if t == "human":
            out.append(HumanMessage(content=m.get("content")))
        elif t == "ai":
            out.append(
                AI(
                    content=m.get("content") or "",
                    tool_calls=m.get("tool_calls") or [],
                    additional_kwargs=m.get("additional_kwargs") or {},
                )
            )
        elif t == "tool":
            out.append(
                ToolMessage(content=m.get("content") or "", tool_call_id=m.get("tool_call_id", ""))
            )
    return out


def test_summarization_middleware_retries_after_local_overflow() -> None:
    """The overflow must be recovered by summarize-then-retry, not surface as a raw
    BadRequestError. Without the patch, ``awrap_model_call`` re-raises the
    BadRequestError and the run dies on the first model call."""
    model_utils.install_context_overflow_error_patch()

    from deepagents.backends import StateBackend
    from deepagents.middleware.summarization import create_summarization_middleware
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    messages = _to_langchain(_load_state_messages())
    calls: list[str] = []
    m = _build_overflowing_model(calls)

    # Build the middleware exactly as create_deep_agent does, so the production
    # trigger/keep for a profile-less model (tokens=170000, keep=6 messages) are used.
    mw = create_summarization_middleware(m, StateBackend())
    request = ModelRequest(model=m, messages=messages, state={"messages": list(messages)})

    async def handler(req: ModelRequest):
        # Mirror the production _execute_model_async: actually invoke the model, which is
        # what raises the overflow error on the first attempt.
        response = await req.model.ainvoke(req.messages)
        return ModelResponse(result=[response])

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        result = asyncio.run(mw.awrap_model_call(request, handler))

    # 3 model calls total: (1) the overflowing user request, (2) the summary-generation
    # call, (3) the summarized user-request retry. A raw BadRequestError would have left
    # this at 1 call and raised.
    assert len(calls) == 3, f"expected overflow + summary + retry, got {len(calls)} calls"

    # The retry (call 3) must have started from the summary, not the original giant history.
    assert SUMMARY_TEXT in calls[2], "retry must start from the summarization message"
    assert "Task message 0" not in calls[2], "retry must not re-send the original history"

    # The final model response is the successful retry.
    final = result.model_response.result[0]
    assert final.content == FINAL_TEXT, "final response must be the successful summarized retry"

    # The summarization event is persisted so later turns keep the compacted history.
    update = result.command.update
    assert "_summarization_event" in update, "summarization event must be persisted"
    event = update["_summarization_event"]
    assert event["cutoff_index"] > 0, "summarization must evict at least some messages"
    assert SUMMARY_TEXT in event["summary_message"].content
