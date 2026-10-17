"""Tests for the local: model profile injection in ``make_model``.

``local:`` models (vLLM behind the OpenAI-compatible endpoint) ship no
models.dev profile, so ``model.profile`` is ``None`` and the deepagents
summarization middleware's fraction-based trigger (0.85 of
``profile["max_input_tokens"]``) is inert — runs only survive the reactive
overflow-retry fallback after vLLM rejects the call. ``make_model`` now
derives a profile from the serving stack's ``max_model_len`` (or the
``LOCAL_LLM_MAX_INPUT_TOKENS`` override) so the proactive trigger works.
The ``/models`` read is async httpx: model construction happens inside the
run's event loop, where a blocking socket call is rejected by langgraph's
blockbuster guard.
"""

import contextlib
import json
import socket
import threading
from typing import Any
from unittest.mock import patch as mock_patch

import httpx
import pytest

from agent.utils import model as model_utils


@pytest.fixture(autouse=True)
def _clear_caches():
    model_utils._MODEL_CACHE.clear()
    model_utils._LOCAL_LLM_WINDOW_CACHE.clear()
    yield
    model_utils._MODEL_CACHE.clear()
    model_utils._LOCAL_LLM_WINDOW_CACHE.clear()


def _vllm_models_payload(max_model_len: int) -> bytes:
    return json.dumps(
        {
            "object": "list",
            "data": [
                {
                    "id": "/mnt/D/models/Qwen3.8-27B",
                    "object": "model",
                    "owned_by": "vllm",
                    "max_model_len": max_model_len,
                }
            ],
        }
    ).encode()


def _mock_transport(handler):
    """httpx.MockTransport built from a sync handler (transport runs it per request)."""
    return httpx.MockTransport(handler)


@contextlib.contextmanager
def _patch_async_client(handler):
    """Route ``httpx.AsyncClient`` through a MockTransport for the duration of the test."""
    real_async_client = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        kwargs["transport"] = _mock_transport(handler)
        return real_async_client(*args, **kwargs)

    with mock_patch.object(httpx, "AsyncClient", _factory):
        yield


class _ModelsServer:
    """Minimal real localhost /v1/models server (avoids patching httpx internals)."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(8)
        self._server = server
        self.base_url = f"http://127.0.0.1:{server.getsockname()[1]}/v1"
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            conn, _ = self._server.accept()
            try:
                conn.recv(65536)
                body = self._payload
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(body)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + body
                )
            except OSError:
                pass
            finally:
                conn.close()

    def close(self) -> None:
        self._server.close()


def _fake_init(captured: dict):
    def _init(model, **kwargs):
        captured["model"] = model
        captured.update(kwargs)
        return "LOCAL_MODEL"

    return _init


async def _run_make_model(handler, monkeypatch) -> dict:
    captured: dict = {}
    monkeypatch.setattr(model_utils, "init_chat_model", _fake_init(captured))
    try:
        with _patch_async_client(handler):
            await model_utils.make_model(
                "local:/mnt/D/models/Qwen3.8-27B",
                use_gateway=False,
                max_tokens=64_000,
            )
    finally:
        model_utils._MODEL_CACHE.clear()
        model_utils._LOCAL_LLM_WINDOW_CACHE.clear()
    return captured


async def test_profile_from_vllm_models_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_LLM_MAX_INPUT_TOKENS", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_vllm_models_payload(262_144))

    # max_model_len spans input+output: 262144 - 64000 output budget = 198144.
    captured = await _run_make_model(handler, monkeypatch)
    profile = captured.get("profile")
    assert profile is not None, "local model must carry a profile"
    assert profile["max_input_tokens"] == 198_144
    assert profile["name"] == "/mnt/D/models/Qwen3.8-27B"


async def test_profile_env_override_wins_and_skips_request(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_MAX_INPUT_TOKENS", "128000")

    def _no_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("env override must not hit the models endpoint")

    captured: dict = {}
    monkeypatch.setattr(model_utils, "init_chat_model", _fake_init(captured))
    model_utils._MODEL_CACHE.clear()
    try:
        with _patch_async_client(_no_handler):
            await model_utils.make_model(
                "local:/mnt/D/models/Qwen3.8-27B", use_gateway=False, max_tokens=64_000
            )
    finally:
        model_utils._MODEL_CACHE.clear()
        model_utils._LOCAL_LLM_WINDOW_CACHE.clear()
    assert captured["profile"]["max_input_tokens"] == 128_000


async def test_profile_absent_when_endpoint_unreachable(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_LLM_MAX_INPUT_TOKENS", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no vLLM")

    captured = await _run_make_model(handler, monkeypatch)
    assert "profile" not in captured, "unknown window must leave the model profile-less"


async def test_profile_absent_when_model_not_listed(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_LLM_MAX_INPUT_TOKENS", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {"object": "list", "data": [{"id": "other-model", "max_model_len": 128_000}]}
            ).encode(),
        )

    captured = await _run_make_model(handler, monkeypatch)
    assert "profile" not in captured


async def test_invalid_env_override_falls_back_to_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_MAX_INPUT_TOKENS", "not-a-number")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_vllm_models_payload(262_144))

    captured = await _run_make_model(handler, monkeypatch)
    assert captured.get("profile", {}).get("max_input_tokens") == 198_144


async def test_profile_lands_on_the_built_model(monkeypatch) -> None:
    """End-to-end shape: the real ChatOpenAI instance carries the injected profile."""
    from langchain_openai import ChatOpenAI

    monkeypatch.delenv("LOCAL_LLM_MAX_INPUT_TOKENS", raising=False)
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "dummy")
    server = _ModelsServer(_vllm_models_payload(262_144))
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", server.base_url)
    model_utils._MODEL_CACHE.clear()
    try:
        model = await model_utils.make_model(
            "local:/mnt/D/models/Qwen3.8-27B", use_gateway=False, max_tokens=64_000
        )
    finally:
        model_utils._MODEL_CACHE.clear()
        model_utils._LOCAL_LLM_WINDOW_CACHE.clear()
        server.close()
    assert isinstance(model, ChatOpenAI)
    assert model.profile is not None
    assert model.profile["max_input_tokens"] == 198_144
