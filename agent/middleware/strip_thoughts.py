"""Middleware that strips model thinking from responses before they are checkpointed.

Thinking/reasoning content is the dominant payload in oversized threads: local
models (Qwen via vLLM/DashScope) persist thinking as
``AIMessage.additional_kwargs["reasoning_content"]`` and as ``reasoning``/
``thinking`` content blocks, which then land in the ``messages`` channel,
checkpoints, and Postgres ``checkpoint_blobs``. This bloat makes ``/state``
and ``/history`` slow or hang on long-running threads.

Stripping runs after the model call returns and before the factory writes the
response into state. Live streaming is unaffected: v3 reasoning-delta events
are emitted while the model invocation is in flight, so the dashboard still
renders thinking during the run. The thinking is only lost from persisted
history and from the context sent to subsequent turns.

Anthropic ``thinking`` blocks that carry a non-empty ``signature`` are
preserved verbatim: Anthropic requires signed thinking blocks to round-trip
unmodified for multi-turn correctness.
"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import AIMessage

_REASONING_KWARG_KEYS: tuple[str, ...] = ("reasoning_content", "reasoning")


def _strip_message(message: Any) -> None:
    if not isinstance(message, AIMessage):
        return
    kwargs = message.additional_kwargs
    if isinstance(kwargs, dict):
        for key in _REASONING_KWARG_KEYS:
            kwargs.pop(key, None)
    content = message.content
    if not isinstance(content, list):
        return
    kept = [
        block
        for block in content
        if not (
            isinstance(block, dict)
            and (
                block.get("type") == "reasoning"
                or (block.get("type") == "thinking" and not block.get("signature"))
            )
        )
    ]
    if len(kept) != len(content):
        message.content = kept


def _result_messages(response: Any) -> list[Any] | None:
    inner = getattr(response, "model_response", None)
    if inner is not None:
        response = inner
    result = getattr(response, "result", None)
    return result if isinstance(result, list) else None


class StripThoughtsMiddleware(AgentMiddleware):
    """Remove thinking from model responses so it never reaches the checkpoint."""

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> Any:
        response = await handler(request)
        messages = _result_messages(response)
        if messages is not None:
            for message in messages:
                _strip_message(message)
        return response
