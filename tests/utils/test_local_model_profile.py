"""Tests for the local: model profile injection in ``make_model``.

``local:`` models (vLLM behind the OpenAI-compatible endpoint) ship no
models.dev profile, so ``model.profile`` is ``None`` and the deepagents
summarization middleware's fraction-based trigger (0.85 of
``profile["max_input_tokens"]``) is inert — runs only survive the reactive
overflow-retry fallback after vLLM rejects the call. ``make_model`` now
derives a profile from the serving stack's ``max_model_len`` (or the
``LOCAL_LLM_MAX_INPUT_TOKENS`` override) so the proactive trigger works.
"""

import contextlib
import json
from unittest.mock import Mock

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


@contextlib.contextmanager
def _fake_response(payload: bytes):
    response = Mock()
    response.read.return_value = payload
    yield response


def _fake_urlopen(payload: bytes | Exception):
    def _fake(request, timeout=None):
        if isinstance(payload, Exception):
            raise payload
        return _fake_response(payload)

    return _fake


def _fake_init(captured: dict):
    def _init(model, **kwargs):
        captured["model"] = model
        captured.update(kwargs)
        return "LOCAL_MODEL"

    return _init


def _run_make_model(monkeypatch, payload: bytes | Exception) -> dict:
    captured: dict = {}
    monkeypatch.setattr(model_utils.urllib.request, "urlopen", _fake_urlopen(payload))
    monkeypatch.setattr(model_utils, "init_chat_model", _fake_init(captured))
    try:
        model_utils.make_model(
            "local:/mnt/D/models/Qwen3.8-27B",
            use_gateway=False,
            max_tokens=64_000,
        )
    finally:
        model_utils._MODEL_CACHE.clear()
        model_utils._LOCAL_LLM_WINDOW_CACHE.clear()
    return captured


def test_profile_from_vllm_models_endpoint(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_LLM_MAX_INPUT_TOKENS", raising=False)
    # max_model_len spans input+output: 262144 - 64000 output budget = 198144.
    captured = _run_make_model(monkeypatch, _vllm_models_payload(262_144))
    profile = captured.get("profile")
    assert profile is not None, "local model must carry a profile"
    assert profile["max_input_tokens"] == 198_144
    assert profile["name"] == "/mnt/D/models/Qwen3.8-27B"


def test_profile_env_override_wins_and_skips_request(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_MAX_INPUT_TOKENS", "128000")

    def _no_urlopen(request, timeout=None):
        raise AssertionError("env override must not hit the models endpoint")

    captured: dict = {}
    monkeypatch.setattr(model_utils.urllib.request, "urlopen", _no_urlopen)
    monkeypatch.setattr(model_utils, "init_chat_model", _fake_init(captured))
    model_utils._MODEL_CACHE.clear()
    try:
        model_utils.make_model(
            "local:/mnt/D/models/Qwen3.8-27B", use_gateway=False, max_tokens=64_000
        )
    finally:
        model_utils._MODEL_CACHE.clear()
        model_utils._LOCAL_LLM_WINDOW_CACHE.clear()
    assert captured["profile"]["max_input_tokens"] == 128_000


def test_profile_absent_when_endpoint_unreachable(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_LLM_MAX_INPUT_TOKENS", raising=False)
    captured = _run_make_model(monkeypatch, ConnectionRefusedError("no vLLM"))
    assert "profile" not in captured, "unknown window must leave the model profile-less"


def test_profile_absent_when_model_not_listed(monkeypatch) -> None:
    monkeypatch.delenv("LOCAL_LLM_MAX_INPUT_TOKENS", raising=False)
    payload = json.dumps(
        {"object": "list", "data": [{"id": "other-model", "max_model_len": 128_000}]}
    ).encode()
    captured = _run_make_model(monkeypatch, payload)
    assert "profile" not in captured


def test_invalid_env_override_falls_back_to_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("LOCAL_LLM_MAX_INPUT_TOKENS", "not-a-number")
    captured = _run_make_model(monkeypatch, _vllm_models_payload(262_144))
    assert captured.get("profile", {}).get("max_input_tokens") == 198_144


def test_profile_lands_on_the_built_model(monkeypatch) -> None:
    """End-to-end shape: the real ChatOpenAI instance carries the injected profile."""
    from langchain_openai import ChatOpenAI

    monkeypatch.delenv("LOCAL_LLM_MAX_INPUT_TOKENS", raising=False)
    monkeypatch.setattr(
        model_utils.urllib.request, "urlopen", _fake_urlopen(_vllm_models_payload(262_144))
    )
    monkeypatch.setenv("LOCAL_LLM_API_KEY", "dummy")
    model_utils._MODEL_CACHE.clear()
    try:
        model = model_utils.make_model(
            "local:/mnt/D/models/Qwen3.8-27B", use_gateway=False, max_tokens=64_000
        )
    finally:
        model_utils._MODEL_CACHE.clear()
        model_utils._LOCAL_LLM_WINDOW_CACHE.clear()
    assert isinstance(model, ChatOpenAI)
    assert model.profile is not None
    assert model.profile["max_input_tokens"] == 198_144
