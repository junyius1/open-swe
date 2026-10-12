import asyncio
import os
from collections.abc import Mapping
from typing import Any, Literal, TypedDict, Unpack, cast

from langchain.chat_models import init_chat_model

from ..dashboard.options import DEFAULT_MODEL_ID, model_profile_with_context_override
from .gateway import gateway_env_default, gateway_overrides
from .openai_oauth import build_desktop_openai_oauth_model, desktop_openai_oauth_available

OPENAI_RESPONSES_WS_BASE_URL = "wss://api.openai.com/v1"

# Anthropic SDK default is 2; a 529 burst can outlive that. Bump to give the
# primary provider a fair chance before the fallback middleware kicks in.
DEFAULT_MAX_RETRIES = 6

# Per-request deadline. Without one a stalled provider connection can park a run
# for as long as the socket stays half-open (observed: a single call wedged for an
# hour). Every provider we ship accepts ``timeout``; keep it generous enough for
# max-effort reasoning on a long context, and let ``max_retries`` above turn a
# stall into a retry instead of a dead run.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 600.0
_TIMEOUT_PROVIDER_PREFIXES = ("openai:", "anthropic:", "google_genai:", "fireworks:")

_MODEL_CACHE: dict[
    tuple[str, bool | None, int | None, tuple[tuple[str, str], ...], int | None], Any
] = {}


def _loop_cache_key() -> int | None:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return None


def _freeze_model_kwargs(kwargs: dict[str, object]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, repr(value)) for key, value in kwargs.items()))


async def close_cached_models() -> None:
    models = list(_MODEL_CACHE.values())
    _MODEL_CACHE.clear()
    for model in models:
        close = getattr(model, "aclose", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result
            continue
        close = getattr(model, "close", None)
        if callable(close):
            result = close()
            if asyncio.iscoroutine(result):
                await result


OpenAIReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]
# OpenAI's Responses API only returns human-readable reasoning text when a
# summary is requested; without it, reasoning happens silently (billed in
# output tokens) and the reasoning content block arrives empty.
OpenAIReasoningSummary = Literal["auto", "concise", "detailed"]
AnthropicThinkingType = Literal["adaptive"]
AnthropicThinkingDisplay = Literal["summarized", "omitted"]
AnthropicEffort = Literal["low", "medium", "high", "xhigh", "max"]
GoogleThinkingLevel = Literal["minimal", "low", "medium", "high"]
FireworksReasoningEffort = Literal["none", "low", "medium", "high", "xhigh", "max"]


class OpenAIReasoning(TypedDict, total=False):
    effort: OpenAIReasoningEffort
    summary: OpenAIReasoningSummary


DEFAULT_LLM_REASONING: "OpenAIReasoning" = {"effort": "medium", "summary": "auto"}


class AnthropicThinking(TypedDict, total=False):
    type: AnthropicThinkingType
    display: AnthropicThinkingDisplay


class ModelKwargs(TypedDict, total=False):
    max_tokens: int | None
    reasoning: OpenAIReasoning | None
    reasoning_effort: OpenAIReasoningEffort | None
    thinking: AnthropicThinking | None
    effort: AnthropicEffort | None
    thinking_level: GoogleThinkingLevel | None
    temperature: float | None
    max_retries: int | None
    timeout: float | None
    store: bool | None
    include: list[str] | None
    output_version: Literal["responses/v1"] | None
    model_kwargs: dict[str, object] | None
    extra_body: dict[str, object] | None  # 👈 【新增这一行】：允许向底层透传请求体参数


_ANTHROPIC_EFFORTS: set[AnthropicEffort] = {"low", "medium", "high", "xhigh", "max"}


_LOCAL_OVERFLOW_PATTERNS = (
    "maximum context length",
    "exceeds the context length",
    "context length exceeded",
    "context length is exceeded",
    "context window exceeded",
    "context window limit exceeded",
    "exceed the model's context length",
)
_local_overflow_installed = False


def _is_context_overflow_error(e: BaseException) -> bool:
    """Detect OpenAI-compatible (vLLM/TGI/llama.cpp) context-overflow rejections."""
    body = getattr(e, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            body = err
    text = f"{getattr(e, 'message', None) or ''} {body}".lower()
    if any(pattern in text for pattern in _LOCAL_OVERFLOW_PATTERNS):
        return True
    return "input_tokens" in text and "context" in text


def install_context_overflow_error_patch() -> None:
    """Wrap langchain-openai's BadRequest handling so vLLM-style context overflows become ContextOverflowError.

    Without this, the deepagents summarization middleware's overflow-retry fallback never engages.
    """
    global _local_overflow_installed
    if _local_overflow_installed:
        return
    try:
        from langchain_openai.chat_models import base as lo_base
    except ImportError:
        return
    if getattr(lo_base, "_open_swe_overflow_patched", False):
        _local_overflow_installed = True
        return

    original = lo_base._handle_openai_bad_request

    def _patched_handle_openai_bad_request(e: BaseException) -> None:
        try:
            original(e)
            return
        except lo_base.ContextOverflowError:
            raise
        except BaseException as raised:
            # The original handler re-raises unmatched errors via a bare
            # `raise`; outside an exception context that surfaces as
            # RuntimeError, so fall back to the error passed in.
            source = (
                e
                if isinstance(raised, RuntimeError) and "No active exception" in str(raised)
                else raised
            )
            if _is_context_overflow_error(source):
                raise lo_base.OpenAIContextOverflowError(
                    message=str(getattr(source, "message", None) or source),
                    response=getattr(source, "response", None),
                    body=getattr(source, "body", None),
                ) from source
            raise source from None

    lo_base._handle_openai_bad_request = _patched_handle_openai_bad_request
    lo_base._open_swe_overflow_patched = True
    _local_overflow_installed = True


_local_reasoning_installed = False

# The base ``ChatOpenAI`` stamps ``model_provider="openai"`` on every
# chat-completions chunk, which routes ``content_blocks`` to the OpenAI
# translator — that translator (in both Python and the ``@langchain/core``
# JS) understands the Responses-API reasoning shape but drops the
# ``reasoning_content``/``reasoning`` fields that Qwen/vLLM/DashScope
# actually emit.
# ``"deepseek"`` is the one provider label that resolves to a
# reasoning-content-aware path on both sides: Python has no ``deepseek``
# translator (so ``content_blocks`` falls back to its built-in
# ``additional_kwargs["reasoning_content"]`` extraction) and the JS side
# registers a ``deepseek`` translator that reads ``reasoning_content``.
_LOCAL_REASONING_MODEL_PROVIDER = "deepseek"
_LOCAL_REASONING_MODEL_PROVIDER_KEY = "model_provider"
_LOCAL_REASONING_MARKER = "_open_swe_local_reasoning"
# OpenAI-compatible servers disagree on the thinking field name: Qwen /
# DashScope (and some vLLM chat templates) emit ``reasoning_content``,
# others emit plain ``reasoning``. Both carry the same thinking tokens.
_LOCAL_REASONING_FIELD_KEYS = ("reasoning_content", "reasoning")


def _delta_reasoning_text(chunk: Mapping[str, Any]) -> str | None:
    """Extract the thinking slice from a chat-completions stream chunk's delta."""
    choices = chunk.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    choice = choices[0]
    if not isinstance(choice, Mapping):
        return None
    delta = choice.get("delta")
    if not isinstance(delta, Mapping):
        return None
    return _first_reasoning_field(delta)


def _message_reasoning_text(message: Any) -> str | None:
    """Extract the thinking text from a non-streaming chat-completions message (dict or SDK object)."""
    if not message:
        return None
    if isinstance(message, Mapping):
        return _first_reasoning_field(message)
    for key in _LOCAL_REASONING_FIELD_KEYS:
        value = getattr(message, key, None)
        if isinstance(value, str) and value:
            return value
    return None


def _first_reasoning_field(mapping: Mapping[str, Any] | None) -> str | None:
    if not mapping:
        return None
    for key in _LOCAL_REASONING_FIELD_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def install_local_reasoning_patch() -> None:
    """Capture local-LLM ``reasoning_content`` so its thinking renders in the dashboard.

    The base ``ChatOpenAI`` deliberately drops provider-specific fields such as
    ``reasoning_content`` (documented at the top of its ``base.py``). For
    ``local:`` models — Qwen/vLLM/DashScope — the thinking tokens are therefore
    discarded before they reach ``AIMessageChunk.content_blocks``, so the v3
    streaming bridge never emits ``reasoning-delta`` events and the dashboard
    shows only tool calls plus the final answer.

    The patch does two things for local-model chunks only (see
    ``_LOCAL_REASONING_MARKER``):

    * store each chunk's thinking slice (``delta.reasoning_content`` or
      ``delta.reasoning``) in ``additional_kwargs["reasoning_content"]``; and
    * relabel ``response_metadata["model_provider"]`` so ``content_blocks``
      resolves to a reasoning-content-aware translator instead of the OpenAI one
      that silently drops the field.

    The same capture is applied to the non-streaming result path
    (``_create_chat_result``): the agent factory invokes models via
    ``ainvoke`` and the compat bridge reconstructs the ``message-start``
    event — including its provider label and the ``additional_kwargs``
    that carry the accumulated thinking — from the final message.

    Everything downstream — the v3 ``reasoning-delta`` events, the client
    ``MessageAssembler`` reasoning blocks, and the ``ReasoningBlock`` renderer —
    already handles standard reasoning blocks, so this is the single missing
    link. Mirrors what the DeepSeek partner does natively.
    """
    global _local_reasoning_installed
    if _local_reasoning_installed:
        return
    try:
        from langchain_openai.chat_models import base as lo_base
    except ImportError:
        return
    if getattr(lo_base, "_open_swe_reasoning_patched", False):
        _local_reasoning_installed = True
        return

    original_convert = lo_base.BaseChatOpenAI._convert_chunk_to_generation_chunk
    original_result = lo_base.BaseChatOpenAI._create_chat_result

    def _patched_convert(self: Any, chunk: Mapping[str, Any], *args: Any) -> Any:
        generation_chunk = original_convert(self, chunk, *args)
        if (
            generation_chunk is None
            or not isinstance(generation_chunk.message, lo_base.AIMessageChunk)
            or not getattr(self, _LOCAL_REASONING_MARKER, False)
        ):
            return generation_chunk
        message = generation_chunk.message
        message.response_metadata[_LOCAL_REASONING_MODEL_PROVIDER_KEY] = (
            _LOCAL_REASONING_MODEL_PROVIDER
        )
        if (reasoning := _delta_reasoning_text(chunk)) is not None:
            message.additional_kwargs["reasoning_content"] = reasoning
        return generation_chunk

    def _patched_create_chat_result(self: Any, *args: Any, **kwargs: Any) -> Any:
        result = original_result(self, *args, **kwargs)
        if not getattr(self, _LOCAL_REASONING_MARKER, False):
            return result
        response = args[0] if args else kwargs.get("response")
        reasoning: str | None = None
        model_name: str | None = None
        choices: Any = None
        if isinstance(response, Mapping):
            choices = response.get("choices")
            if (model := response.get("model")) is not None:
                model_name = str(model)
        else:
            choices = getattr(response, "choices", None)
            model_name = getattr(response, "model", None)
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, Mapping):
                reasoning = _message_reasoning_text(choice.get("message"))
            else:
                reasoning = _message_reasoning_text(getattr(choice, "message", None))
        for generation in result.generations:
            message = generation.message
            message.response_metadata[_LOCAL_REASONING_MODEL_PROVIDER_KEY] = (
                _LOCAL_REASONING_MODEL_PROVIDER
            )
            if isinstance(model_name, str):
                message.response_metadata.setdefault("model_name", model_name)
            if reasoning is not None:
                message.additional_kwargs["reasoning_content"] = reasoning
        return result

    lo_base.BaseChatOpenAI._convert_chunk_to_generation_chunk = _patched_convert  # type: ignore[method-assign]
    lo_base.BaseChatOpenAI._create_chat_result = _patched_create_chat_result  # type: ignore[method-assign]
    lo_base._open_swe_reasoning_patched = True
    _local_reasoning_installed = True


def _coerce_openai_chat_completions_kwargs(model_kwargs: dict[str, object]) -> None:
    if model_kwargs.get("use_responses_api") is not False:
        return
    reasoning = model_kwargs.pop("reasoning", None)
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        if isinstance(effort, str):
            model_kwargs.setdefault("reasoning_effort", effort)


def _configure_openai_responses_kwargs(model_kwargs: dict[str, object]) -> None:
    if model_kwargs.get("use_responses_api") is False:
        return
    model_kwargs.setdefault("store", False)
    model_kwargs.setdefault("output_version", "responses/v1")
    include = model_kwargs.get("include")
    if include is None:
        model_kwargs["include"] = ["reasoning.encrypted_content"]
    elif isinstance(include, list) and "reasoning.encrypted_content" not in include:
        include.append("reasoning.encrypted_content")


def make_model(model_id: str, *, use_gateway: bool | None = None, **kwargs: Unpack[ModelKwargs]):
    """Build a chat model, optionally routed through the LangSmith LLM Gateway.

    ``use_gateway`` resolves the deployment default (``LANGSMITH_GATEWAY_ENABLED``)
    when ``None``; async callers pass the team-settings-resolved value. When on,
    gateway ``base_url``/``api_key``/``use_responses_api`` override the direct
    provider defaults below (see :mod:`agent.utils.gateway`).
    """
    model_kwargs: dict[str, object] = dict(kwargs)
    model_kwargs.setdefault("max_retries", DEFAULT_MAX_RETRIES)
    if model_id.startswith(_TIMEOUT_PROVIDER_PREFIXES):
        model_kwargs.setdefault("timeout", DEFAULT_REQUEST_TIMEOUT_SECONDS)
    # The local: branch below returns early, so install the overflow patch here
    # rather than at the shared tail; it is idempotent and cheap.
    install_context_overflow_error_patch()

    if model_id.startswith("local:"):
        model_name = model_id.split(":", 1)[1]
        model_kwargs["model_provider"] = "openai"
        model_kwargs["base_url"] = os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:8080/v1")
        model_kwargs["api_key"] = os.environ.get("LOCAL_LLM_API_KEY", "dummy")
        model_kwargs.pop("use_responses_api", None)
        model_kwargs.pop("reasoning", None)
        model_kwargs.pop("thinking", None)
        model_kwargs.pop("effort", None)

        # 👈 【新增以下逻辑】：注入 Qwen/DashScope/vLLM 的思考开关
        extra_body = dict(model_kwargs.get("extra_body") or {})
        # 从环境变量或者默认开启思考模式
        enable_thinking = os.environ.get("LOCAL_LLM_ENABLE_THINKING", "true").lower() in (
            "true",
            "1",
        )
        if enable_thinking:
            extra_body["enable_thinking"] = True
            # 如果是本地 vLLM，部分框架使用 chat_template_kwargs
            extra_body.setdefault("chat_template_kwargs", {"enable_thinking": True})

        if extra_body:
            model_kwargs["extra_body"] = extra_body

        install_local_reasoning_patch()

        local_model = init_chat_model(model=model_name, **model_kwargs)
        # Mark the instance so the reasoning patch only relabels/captures
        # chunks from local models, leaving other chat-completions models
        # alone. Unsettable markers (test doubles, proxies) are harmless:
        # the patch no-ops on instances that carry no marker.
        try:
            local_model._open_swe_local_reasoning = True  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass
        return local_model

    if model_id.startswith("openai:"):
        model_kwargs["base_url"] = (
            os.environ.get("OPENAI_BASE_URL")
            or os.environ.get("OPENAI_API_BASE")
            or OPENAI_RESPONSES_WS_BASE_URL
        )
        model_kwargs["use_responses_api"] = True

    enabled = gateway_env_default() if use_gateway is None else use_gateway
    gateway_applied = False
    oauth_applied = False
    if enabled:
        overrides = gateway_overrides(model_id)
        if overrides is not None:
            model_kwargs.update(overrides)
            gateway_applied = True

    if (
        model_id.startswith("openai:")
        and not gateway_applied
        and not os.environ.get("OPENAI_API_KEY")
        and desktop_openai_oauth_available()
    ):
        model_kwargs.pop("base_url", None)
        oauth_applied = True

    if model_id.startswith("openai:"):
        if oauth_applied:
            model_kwargs.pop("max_tokens", None)
        _configure_openai_responses_kwargs(model_kwargs)
        _coerce_openai_chat_completions_kwargs(model_kwargs)

    profile_override = model_profile_with_context_override(model_id)
    if profile_override is not None:
        model_kwargs["profile"] = profile_override

    max_tokens = model_kwargs.get("max_tokens")
    max_tokens_key = max_tokens if type(max_tokens) is int else None
    key = (
        model_id,
        use_gateway,
        max_tokens_key,
        _freeze_model_kwargs(model_kwargs),
        _loop_cache_key(),
    )
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached
    if oauth_applied:
        model = build_desktop_openai_oauth_model(
            model_id.split(":", 1)[1], **cast(dict[str, Any], model_kwargs)
        )
    else:
        model = init_chat_model(model=model_id, **cast(dict[str, Any], model_kwargs))
    _MODEL_CACHE[key] = model
    return model


def fallback_model_id_for(primary_model_id: str) -> str | None:
    """Return the cross-provider fallback model id for a given primary, if any.

    Anthropic primaries fall back to OpenAI and vice versa. Returns ``None``
    when the provider has no configured cross-provider fallback (e.g. Google,
    local, or self-hosted providers we don't want to silently route off-host).
    """
    if primary_model_id.startswith("anthropic:"):
        return "openai:gpt-5.6-sol"
    if primary_model_id.startswith("openai:"):
        return "anthropic:claude-opus-5"
    return None


def is_gemini_3_family(model_id: str) -> bool:
    model_name = model_id.split(":", 1)[-1]
    return model_name.startswith("gemini-3")


def openai_reasoning_for(
    profile_effort: str | None,
    *,
    default_effort: OpenAIReasoningEffort | None = None,
) -> OpenAIReasoning | None:
    """Return an OpenAI reasoning kwarg from a profile effort string.

    Requests ``summary: "auto"`` for every reasoning effort so the Responses
    API emits visible reasoning text. ``effort: "none"`` disables reasoning
    entirely, so no summary is attached.
    """
    effort = profile_effort or default_effort or DEFAULT_LLM_REASONING.get("effort")
    if effort == "none":
        return {"effort": "none"}
    if effort == "low":
        return {"effort": "low", "summary": "auto"}
    if effort == "medium":
        return {"effort": "medium", "summary": "auto"}
    if effort == "high":
        return {"effort": "high", "summary": "auto"}
    if effort == "xhigh":
        return {"effort": "xhigh", "summary": "auto"}
    if effort == "max":
        return {"effort": "max", "summary": "auto"}
    return None


def anthropic_thinking_for(profile_effort: str | None) -> AnthropicThinking | None:
    if profile_effort in _ANTHROPIC_EFFORTS:
        # `display: "summarized"` makes Opus 4.7+ return the (summarized) reasoning
        # text in the response. The adaptive default is "omitted", which streams a
        # reasoning block carrying only a signature and no visible thinking — so the
        # dashboard never has any text to render.
        return {"type": "adaptive", "display": "summarized"}
    return None


def anthropic_effort_for(profile_effort: str | None) -> AnthropicEffort | None:
    if profile_effort in _ANTHROPIC_EFFORTS:
        return profile_effort
    return None


def fireworks_reasoning_effort_for(profile_effort: str | None) -> FireworksReasoningEffort | None:
    """Map profile effort to a Fireworks ``reasoning_effort`` value.

    Fireworks' OpenAI-compatible API accepts ``reasoning_effort`` on its reasoning
    models. ``none`` disables reasoning; ``xhigh``/``max`` are only honored by models
    that advertise them (e.g. DeepSeek V4 Pro). The per-model ``efforts`` lists in
    ``dashboard/options.py`` gate which values can actually reach this function.
    """
    if profile_effort == "none":
        return "none"
    if profile_effort == "low":
        return "low"
    if profile_effort == "medium":
        return "medium"
    if profile_effort == "high":
        return "high"
    if profile_effort == "xhigh":
        return "xhigh"
    if profile_effort == "max":
        return "max"
    return None


def google_thinking_level_for(profile_effort: str | None) -> GoogleThinkingLevel | None:
    """Map profile effort to Gemini 3+ ``thinking_level``."""
    if profile_effort in ("minimal", "none"):
        return "minimal"
    if profile_effort == "low":
        return "low"
    if profile_effort == "medium":
        return "medium"
    if profile_effort in ("high", "xhigh", "max"):
        return "high"
    return None


def provider_model_kwargs(
    model_id: str,
    profile_effort: str | None,
    *,
    max_tokens: int,
    openai_reasoning_default: OpenAIReasoning | None = None,
) -> ModelKwargs:
    """Build provider-specific kwargs for ``make_model`` from a model id and effort."""
    kwargs: ModelKwargs = {"max_tokens": max_tokens}
    if model_id.startswith("openai:"):
        reasoning = openai_reasoning_for(profile_effort)
        if reasoning is not None:
            kwargs["reasoning"] = reasoning
        elif openai_reasoning_default is not None:
            kwargs["reasoning"] = openai_reasoning_default
    elif model_id.startswith("anthropic:"):
        thinking = anthropic_thinking_for(profile_effort)
        if thinking is not None:
            kwargs["thinking"] = thinking
        effort = anthropic_effort_for(profile_effort)
        if effort is not None:
            kwargs["effort"] = effort
    elif model_id.startswith("google_genai:") and is_gemini_3_family(model_id):
        thinking_level = google_thinking_level_for(profile_effort)
        if thinking_level is not None:
            kwargs["thinking_level"] = thinking_level
    elif model_id.startswith("fireworks:"):
        effort = fireworks_reasoning_effort_for(profile_effort)
        if effort is not None:
            kwargs["model_kwargs"] = {"reasoning_effort": effort}
    # 👈 【新增此分支】：处理本地/Qwen 模型的思考开关
    elif model_id.startswith("local:"):
        # 只要 profile 没有显式设为 "none"，就打开思考
        if profile_effort != "none":
            kwargs["extra_body"] = {"enable_thinking": True}
    return kwargs


def validate_local_dev_llm_config() -> None:
    """Validate API keys for the locally configured default model.

    This check only runs in localhost development environments and is
    intended to catch missing credentials for the default model specified
    via LLM_MODEL_ID/DEFAULT_MODEL_ID. Runtime model selection may come
    from team, profile, or thread configuration and is not validated here.
    """
    dashboard_url = os.environ.get("DASHBOARD_BASE_URL", "")
    if not dashboard_url.startswith("http://localhost"):
        return

    model_id = os.environ.get("LLM_MODEL_ID", DEFAULT_MODEL_ID)

    if (
        model_id.startswith("openai:")
        and not os.environ.get("OPENAI_API_KEY")
        and not desktop_openai_oauth_available()
    ):
        raise ValueError(f"OPENAI_API_KEY is required for configured model {model_id}")
    elif model_id.startswith("anthropic:") and not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError(f"ANTHROPIC_API_KEY is required for configured model {model_id}")
    elif model_id.startswith("google_genai:") and not os.environ.get("GOOGLE_API_KEY"):
        raise ValueError(f"GOOGLE_API_KEY is required for configured model {model_id}")
    elif model_id.startswith("groq:") and not os.environ.get("GROQ_API_KEY"):
        raise ValueError(f"GROQ_API_KEY is required for configured model {model_id}")
    elif model_id.startswith("fireworks:") and not os.environ.get("FIREWORKS_API_KEY"):
        raise ValueError(f"FIREWORKS_API_KEY is required for configured model {model_id}")
