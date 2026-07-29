"""OpenAI GPT-4o adapter for `LLMClient`.

Vision (FR2) + tool-calls (FR4) bám vào cùng adapter này ở M2/M4; M1 chỉ cần text streaming.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast

import openai
from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import ChatCompletion, ChatCompletionChunk

from agent.llm_client import (
    AssistantStep,
    ChatMessage,
    LLMClient,
    StreamDone,
    StreamEvent,
    StreamReasoningDelta,
    StreamTokenDelta,
    StreamToolCallDelta,
    ToolCall,
)
from app.config import get_settings

logger = logging.getLogger(__name__)


def _to_openai_content(content: str | list[Any]) -> Any:
    """Neutral content → OpenAI shape. str passthrough; ImagePart → image_url data-URL (§5.2)."""
    if isinstance(content, str):
        return content
    parts: list[dict[str, Any]] = []
    for p in content:
        if p["type"] == "text":
            parts.append({"type": "text", "text": p["text"]})
        else:  # image
            parts.append(
                {"type": "image_url", "image_url": {"url": f"data:{p['mime']};base64,{p['data']}"}}
            )
    return parts


def _to_openai_messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """Neutral messages → OpenAI shape, including native tool-call turns (FR4):
    - role `tool` → {role, tool_call_id, content} (the result, content is the obs JSON string).
    - assistant with `tool_calls` → {role, content|null, tool_calls:[function-call]}.
    - everything else → plain {role, content} (M1–M3 path, unchanged).
    """
    out: list[dict[str, Any]] = []
    for m in messages:
        if m["role"] == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m["tool_call_id"],
                    "content": m["content"],
                }
            )
        elif m.get("tool_calls"):
            out.append(
                {
                    "role": "assistant",
                    "content": m["content"] or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                        }
                        for tc in m["tool_calls"]
                    ],
                }
            )
        else:
            out.append({"role": m["role"], "content": _to_openai_content(m["content"])})
    return out


def _to_openai_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Neutral tool specs `{name,description,parameters}` → OpenAI `tools=` function shape."""
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("parameters", {}),
            },
        }
        for t in tools
    ]


class OpenAIClient(LLMClient):
    """Bọc `AsyncOpenAI`. API key + model mặc định lấy từ settings (env)."""

    def __init__(
        self,
        client: AsyncOpenAI | None = None,
        default_model: str | None = None,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        on_rate_limit: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__()
        settings = get_settings()
        # `client` injectable để test (mock) — không tạo network khi unit test.
        # base_url/api_key None → AsyncOpenAI uses api.openai.com + OPENAI_API_KEY (behavior-
        # preserving); non-None values route through a gateway/proxy without changing call path.
        self._client = client or AsyncOpenAI(
            api_key=api_key or settings.openai_api_key, base_url=base_url
        )
        self._default_model = default_model or settings.openai_model
        # ISSUE-010 (spec 07 G0): the 429 counter used to be a module-level import of
        # `app.alert_service`, a module never ported from drnickv4 — so that one dead import
        # made this ENTIRE module unimportable, which is why Together (OpenAI-compatible,
        # otherwise a free ride on this class) was blocked.
        # Telemetry is now injected: absent hook → no counter, same exception. ISSUE-010 stays
        # OPEN for real alerting; G0 only removed the coupling, it did not port the service.
        self._on_rate_limit = on_rate_limit

    async def _create(self, **kwargs: Any) -> Any:
        """Single funnel for `create` that reports provider 429s to the injected hook, then
        RE-RAISES unchanged. Fire-and-forget: the RateLimitError propagates in EVERY path — no
        hook, hook succeeds, or hook itself blows up. No swallow, no retry. Returns what
        `create` returns (ChatCompletion or AsyncStream); callers cast as before.
        """
        try:
            return await self._client.chat.completions.create(**kwargs)
        except openai.RateLimitError:
            if self._on_rate_limit is not None:
                try:
                    await self._on_rate_limit()
                except Exception as hook_exc:
                    # Telemetry NEVER gets to mask the real error. The hook is caller-supplied
                    # code (a counter, a metrics push, a DB write) — if it times out or throws,
                    # letting that escape would replace RateLimitError with something unrelated,
                    # and every backoff/retry branch upstream keys on the exception TYPE. The
                    # caller would stop seeing "rate limited" during exactly the incident where
                    # telemetry is most likely to be failing too.
                    #
                    # `except Exception` (not BaseException) on purpose: KeyboardInterrupt and
                    # asyncio.CancelledError must still propagate — a cancelled request should
                    # die, not get downgraded into a logged warning.
                    #
                    # Log the exception TYPE only — not the message, not the traceback. A
                    # traceback is actually safe (CPython prints source lines, never local
                    # values — verified, not assumed), but the hook's exception *message* is
                    # attacker-adjacent: a DB/HTTP client that echoes the row or body it choked
                    # on would put customer message text into our logs, which under PDPL is
                    # data we never consented to store there. The hook's author owns logging
                    # their own detail; our only job is to not lose the signal or the 429.
                    logger.warning(
                        "on_rate_limit hook raised %s; original 429 re-raised intact",
                        type(hook_exc).__name__,
                    )
            raise

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        # Reset side-channel; populated from the final usage chunk below (spec 25 P1).
        self.last_usage = None
        raw = await self._create(
            model=model or self._default_model,
            messages=_to_openai_messages(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            # OpenAI emits a final usage chunk when this option is set; the chunk has empty choices
            # so the existing `if not chunk.choices: continue` guard wouldn't surface it — we read
            # `chunk.usage` explicitly before the continue.
            stream_options={"include_usage": True},
        )
        # stream=True → AsyncStream; overload không narrow được nên cast tường minh.
        stream = cast(AsyncStream[ChatCompletionChunk], raw)
        async for chunk in stream:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                self.last_usage = _extract_usage(chunk_usage)
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        self.last_usage = None
        resp = await self._create(
            model=model or self._default_model,
            messages=_to_openai_messages(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
        )
        # stream=False → ChatCompletion; overload không narrow được nên cast tường minh.
        completion = cast(ChatCompletion, resp)
        self.last_usage = _extract_usage(getattr(completion, "usage", None))
        return completion.choices[0].message.content or ""

    async def step(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AssistantStep:
        self.last_usage = None
        openai_tools = _to_openai_tools(tools)
        kwargs: dict[str, Any] = {}
        if openai_tools is not None:
            kwargs["tools"] = openai_tools
        resp = await self._create(
            model=model or self._default_model,
            messages=_to_openai_messages(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            stream=False,
            **kwargs,
        )
        completion = cast(ChatCompletion, resp)
        msg = completion.choices[0].message
        # Capture usage to BOTH AssistantStep.usage (inline) AND self.last_usage (uniform contract).
        usage = _extract_usage(getattr(completion, "usage", None))
        self.last_usage = usage
        calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            if tc.type != "function":  # narrow union; we don't use custom tool-calls
                continue
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):  # tolerant: non-object args → {}
                args = {}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        return AssistantStep(content=msg.content, tool_calls=calls, usage=usage)

    async def step_stream(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        reasoning: bool = False,
    ) -> AsyncIterator[StreamEvent]:
        """Native streaming-with-tools (spec 27 P2). One `chat.completions.create(stream=True,
        tools=...)` call delivers interleaved content + tool_call deltas. The adapter:
        - forwards content fragments as `StreamTokenDelta` (live → user via TokenEvent),
        - accumulates tool_call slot args by `index` for the final parse,
        - mirrors each incoming tool_call delta as `StreamToolCallDelta` (orchestrator may ignore
          per-delta — the load-bearing data is on StreamDone),
        - emits exactly one `StreamDone` at end with finish_reason, parsed tool_calls, usage.
        """
        self.last_usage = None
        openai_tools = _to_openai_tools(tools)
        kwargs: dict[str, Any] = {}
        if openai_tools is not None:
            kwargs["tools"] = openai_tools
        # Reasoning-mode (CoT on a separate `delta.reasoning` channel). Only request extra_body for
        # a model known to support it — else gpt-4o/other could reject the call. The tier gate is
        # upstream (orchestrator); `reasoning` already encodes "mode==pro_only and tier==pro".
        if reasoning and (model or self._default_model) in get_settings().reasoning_models:
            kwargs["extra_body"] = {
                "reasoning": {"enabled": True},
                "chat_template_kwargs": {"thinking": True},
            }
        raw = await self._create(
            model=model or self._default_model,
            messages=_to_openai_messages(messages),
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
            **kwargs,
        )
        stream = cast(AsyncStream[ChatCompletionChunk], raw)

        # Slot accumulator: index → {id, name, args_str}. Parsed at StreamDone time.
        tool_call_accum: dict[int, dict[str, Any]] = {}
        finish_reason = "stop"  # defensive default if provider never sends one
        async for chunk in stream:
            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                self.last_usage = _extract_usage(chunk_usage)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            fr = getattr(choice, "finish_reason", None)
            if fr:
                finish_reason = fr
            delta = choice.delta
            content_piece = getattr(delta, "content", None)
            if content_piece:
                yield StreamTokenDelta(delta=content_piece)
            # CoT on `delta.reasoning` → distinct event (never mashed into content). Some models
            # emit delta.reasoning by default, so gate the FORWARD on the caller's `reasoning`
            # flag (not just extra_body) — else a non-reasoning turn would still leak CoT. Model
            # reasons regardless; tier gates DISPLAY only.
            reasoning_piece = getattr(delta, "reasoning", None)
            if reasoning_piece and reasoning:
                yield StreamReasoningDelta(delta=reasoning_piece)
            tc_deltas = getattr(delta, "tool_calls", None) or []
            for tc_delta in tc_deltas:
                idx = getattr(tc_delta, "index", 0)
                slot = tool_call_accum.setdefault(idx, {"id": None, "name": None, "args_str": ""})
                tc_id = getattr(tc_delta, "id", None)
                if tc_id:
                    slot["id"] = tc_id
                fn = getattr(tc_delta, "function", None)
                tc_name = getattr(fn, "name", None) if fn is not None else None
                if tc_name:
                    slot["name"] = tc_name
                tc_args = getattr(fn, "arguments", None) if fn is not None else None
                if tc_args:
                    slot["args_str"] += tc_args
                yield StreamToolCallDelta(
                    index=idx, id=tc_id, name=tc_name, arguments_delta=tc_args
                )

        # Parse accumulated tool_calls (sorted by index for deterministic dispatch order).
        accumulated: list[ToolCall] = []
        for idx in sorted(tool_call_accum):
            slot = tool_call_accum[idx]
            if not slot["id"] or not slot["name"]:
                # Defensive: an incomplete slot (no id/name) is dropped — never dispatch a
                # malformed tool call (never invent identifiers).
                continue
            try:
                args = json.loads(slot["args_str"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if not isinstance(args, dict):  # mirror step(): tools take objects only
                args = {}
            accumulated.append(ToolCall(id=slot["id"], name=slot["name"], arguments=args))
        yield StreamDone(
            finish_reason=finish_reason,
            accumulated_tool_calls=accumulated,
            usage=self.last_usage,
        )


def _coerce_int(value: Any) -> int:
    """Best-effort int; non-numeric/None → 0 (never raises — usage fields are advisory)."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _extract_cache_hit_tokens(usage: Any) -> int:
    """Provider prompt-cache HIT token count, defensive multi-provider (spec 44 P1, Q1).
    First-present field wins (even at 0 — a reported cache miss is 0, not "unknown"); no field
    present → 0 (provider doesn't report caching). Never raises.
      - OpenAI / Together / DeepInfra: usage.prompt_tokens_details.cached_tokens
      - DeepSeek native shape:         usage.prompt_cache_hit_tokens
      - fallback (other compatible):   usage.cached_tokens
    """
    details = getattr(usage, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", None)
        if cached is not None:
            return _coerce_int(cached)
    for field in ("prompt_cache_hit_tokens", "cached_tokens"):
        cached = getattr(usage, field, None)
        if cached is not None:
            return _coerce_int(cached)
    return 0


def _extract_usage(usage: Any | None) -> dict[str, int] | None:
    """Normalize OpenAI usage object → flat dict[str, int]. Returns None if missing or malformed
    (graceful — orchestrator handles None by skipping that call in aggregation). Carries
    `cached_tokens` (provider prompt-cache hit; 0 when unreported) alongside prompt/completion/
    total tokens."""
    if usage is None:
        return None
    try:
        return {
            "prompt_tokens": int(getattr(usage, "prompt_tokens", 0) or 0),
            "completion_tokens": int(getattr(usage, "completion_tokens", 0) or 0),
            "total_tokens": int(getattr(usage, "total_tokens", 0) or 0),
            "cached_tokens": _extract_cache_hit_tokens(usage),
        }
    except (TypeError, ValueError):
        return None
