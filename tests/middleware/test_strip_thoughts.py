from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from langchain.agents.middleware.types import ExtendedModelResponse, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.middleware.strip_thoughts import StripThoughtsMiddleware


def _make_request() -> ModelRequest[None]:
    request = MagicMock()
    request.model = MagicMock()
    request.messages = []
    return cast(ModelRequest[None], request)


async def _call(middleware: StripThoughtsMiddleware, response: Any) -> Any:
    async def handler(req: ModelRequest[None]) -> ModelResponse[Any]:
        return cast(ModelResponse[Any], response)

    return await middleware.awrap_model_call(_make_request(), handler)


class TestStripThoughtsMiddleware:
    @pytest.mark.asyncio
    async def test_strips_reasoning_content_kwarg(self) -> None:
        message = AIMessage(
            content="answer",
            additional_kwargs={"reasoning_content": "long thinking"},
        )
        response = ModelResponse(result=[message])

        result = await _call(StripThoughtsMiddleware(), response)

        assert result is response
        assert message.additional_kwargs == {}
        assert message.content == "answer"

    @pytest.mark.asyncio
    async def test_strips_reasoning_kwarg(self) -> None:
        message = AIMessage(content="answer", additional_kwargs={"reasoning": "thinking"})

        await _call(StripThoughtsMiddleware(), ModelResponse(result=[message]))

        assert message.additional_kwargs == {}

    @pytest.mark.asyncio
    async def test_strips_reasoning_content_blocks(self) -> None:
        message = AIMessage(
            content=[
                {"type": "reasoning", "reasoning": "chain of thought"},
                {"type": "text", "text": "ok"},
            ]
        )

        await _call(StripThoughtsMiddleware(), ModelResponse(result=[message]))

        assert message.content == [{"type": "text", "text": "ok"}]

    @pytest.mark.asyncio
    async def test_strips_encrypted_reasoning_blocks(self) -> None:
        message = AIMessage(
            content=[
                {"type": "reasoning", "id": "rs_1", "encrypted_content": "gAAAAA"},
                {"type": "text", "text": "ok"},
            ]
        )

        await _call(StripThoughtsMiddleware(), ModelResponse(result=[message]))

        assert message.content == [{"type": "text", "text": "ok"}]

    @pytest.mark.asyncio
    async def test_strips_unsigned_thinking_blocks(self) -> None:
        message = AIMessage(
            content=[
                {"type": "thinking", "thinking": "unsigned reasoning"},
                {"type": "text", "text": "ok"},
            ]
        )

        await _call(StripThoughtsMiddleware(), ModelResponse(result=[message]))

        assert message.content == [{"type": "text", "text": "ok"}]

    @pytest.mark.asyncio
    async def test_preserves_signed_thinking_blocks(self) -> None:
        thinking_block = {"type": "thinking", "signature": "sig_abc", "thinking": "reasoning"}
        message = AIMessage(content=[thinking_block, {"type": "text", "text": "ok"}])

        await _call(StripThoughtsMiddleware(), ModelResponse(result=[message]))

        assert message.content == [thinking_block, {"type": "text", "text": "ok"}]

    @pytest.mark.asyncio
    async def test_ignores_string_content(self) -> None:
        message = AIMessage(content="just text")

        await _call(StripThoughtsMiddleware(), ModelResponse(result=[message]))

        assert message.content == "just text"
        assert message.additional_kwargs == {}

    @pytest.mark.asyncio
    async def test_ignores_non_ai_messages(self) -> None:
        human = HumanMessage(content="hi")
        tool = ToolMessage(content="tool out", tool_call_id="call_1")
        response = ModelResponse(result=[tool])

        await _call(StripThoughtsMiddleware(), response)

        assert human.content == "hi"
        assert tool.content == "tool out"
        assert tool.tool_call_id == "call_1"

    @pytest.mark.asyncio
    async def test_preserves_text_and_tool_calls(self) -> None:
        message = AIMessage(
            content=[
                {"type": "reasoning", "reasoning": "thinking"},
                {"type": "text", "text": "the answer"},
                {
                    "type": "tool_call",
                    "id": "call_1",
                    "name": "ls",
                    "args": {"path": "/tmp"},
                },
            ]
        )

        await _call(StripThoughtsMiddleware(), ModelResponse(result=[message]))

        assert message.content == [
            {"type": "text", "text": "the answer"},
            {
                "type": "tool_call",
                "id": "call_1",
                "name": "ls",
                "args": {"path": "/tmp"},
            },
        ]

    @pytest.mark.asyncio
    async def test_keeps_thought_only_message(self) -> None:
        message = AIMessage(
            content=[{"type": "reasoning", "reasoning": "only thinking here"}],
            additional_kwargs={"reasoning_content": "only thinking here"},
        )
        response = ModelResponse(result=[message])

        result = await _call(StripThoughtsMiddleware(), response)

        assert result is response
        assert response.result == [message]
        assert message.content == []
        assert message.additional_kwargs == {}

    @pytest.mark.asyncio
    async def test_handles_extended_model_response(self) -> None:
        message = AIMessage(
            content="ok",
            additional_kwargs={"reasoning_content": "thinking"},
        )
        inner = ModelResponse(result=[message])
        response = ExtendedModelResponse(model_response=inner)

        result = await _call(StripThoughtsMiddleware(), response)

        assert result is response
        assert message.additional_kwargs == {}

    @pytest.mark.asyncio
    async def test_mixed_messages(self) -> None:
        ai_message = AIMessage(
            content=[
                {"type": "thinking", "signature": "sig", "thinking": "signed"},
                {"type": "reasoning", "reasoning": "unsigned"},
                {"type": "text", "text": "final"},
            ],
            additional_kwargs={"reasoning": "dup thinking"},
        )
        human = HumanMessage(content="question")
        response = ModelResponse(result=[human, ai_message])

        await _call(StripThoughtsMiddleware(), response)

        assert response.result == [human, ai_message]
        assert human.content == "question"
        assert ai_message.additional_kwargs == {}
        assert ai_message.content == [
            {"type": "thinking", "signature": "sig", "thinking": "signed"},
            {"type": "text", "text": "final"},
        ]
