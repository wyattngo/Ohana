"""Provider-agnostic LLM interface.

Mọi truy cập model đi qua `LLMClient` (R6 pair: llm_client.py ↔ providers/*). Đổi nhà cung cấp
(OpenAI → Claude/khác) = thêm một adapter mới, KHÔNG sửa agent core (design §5.2).
"""

from __future__ import annotations

import base64
import json
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, TypedDict


class TextPart(TypedDict):
    type: Literal["text"]
    text: str


class ImagePart(TypedDict):
    """Ảnh trung tính (provider-agnostic). `data` = base64 thuần (không prefix `data:`)."""

    type: Literal["image"]
    mime: str
    data: str


ContentPart = TextPart | ImagePart


@dataclass
class ToolCall:
    """A model-requested tool invocation (native tool-calls, FR4). `arguments` is the PARSED JSON
    object (never a raw string); `id` correlates the later tool-result message (tool_call_id)."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class AssistantStep:
    """One model turn from `LLMClient.step()`: either it requests tools (`tool_calls` non-empty) or
    it produced an answer (`content`). Provider-agnostic; adapters build it from their raw reply.

    `usage` (spec 25 P1): provider-supplied token count dict `{prompt_tokens, completion_tokens,
    total_tokens, cached_tokens}` for this step (`cached_tokens` = provider prompt-cache hits,
    spec 44 P1; 0 when unreported). None when provider doesn't report (legacy adapter or model).
    Orchestrator aggregates this into `AgentRun.usage_total` for cost telemetry (spec 25 P3+)."""

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] | None = None


# ── Native streaming-with-tools events ──
# step_stream() emits a sequence of these, ending with exactly one StreamDone. Lets the
# orchestrator stream content to the user as soon as the first chunk arrives (no upfront
# non-stream step()), while still buffering tool_call deltas until the model decides to
# invoke tools.


@dataclass
class StreamTokenDelta:
    """An incremental piece of the model's `content`. Emit straight to the SSE TokenEvent."""

    delta: str


@dataclass
class StreamReasoningDelta:
    """An incremental piece of the model's chain-of-thought (`delta.reasoning`) — a SEPARATE channel
    from `content`. Only emitted when reasoning-mode was requested (extra_body) and the provider
    supports it. The orchestrator routes this to an ephemeral collapse channel; it NEVER enters
    the answer or the persisted message."""

    delta: str


@dataclass
class StreamToolCallDelta:
    """An incremental tool_call piece. OpenAI streams tool_call deltas keyed by `index` (slot)
    with `id`/`name` set on the FIRST delta and `arguments` arriving as JSON-string fragments;
    the adapter accumulates per slot and returns the fully-parsed tools in StreamDone."""

    index: int
    id: str | None
    name: str | None
    arguments_delta: str | None


@dataclass
class StreamDone:
    """Terminal event of a step_stream() iteration. `finish_reason` ∈ {"stop","tool_calls",...}.
    `accumulated_tool_calls` is the fully-buffered + JSON-parsed list (empty when content path);
    `usage` mirrors the final-chunk usage dict (spec 25 P1 contract, also visible on last_usage)."""

    finish_reason: str
    accumulated_tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] | None = None


StreamEvent = StreamTokenDelta | StreamReasoningDelta | StreamToolCallDelta | StreamDone


class ChatMessage(TypedDict):
    """Một lượt hội thoại. `role` ∈ {system, user, assistant, tool}. `content` = text thuần hoặc
    list part đa phương thức (vision). Adapter mỗi provider tự dịch part sang shape riêng (§5.2).

    Native tool-call fields (optional): an assistant turn that called tools carries `tool_calls`;
    a `tool`-role result carries `tool_call_id` (+ `name`) to satisfy the provider's correlation
    contract. Absent on plain text turns → M1–M3 messages are unchanged."""

    role: str
    content: str | list[ContentPart]
    tool_calls: NotRequired[list[ToolCall]]
    tool_call_id: NotRequired[str]
    name: NotRequired[str]


def text_part(text: str) -> TextPart:
    return {"type": "text", "text": text}


def image_part(data: bytes, mime: str) -> ImagePart:
    """Bytes ảnh → ImagePart neutral (base64). MIME đã được validate ở tầng upload (P2.1)."""
    return {"type": "image", "mime": mime, "data": base64.b64encode(data).decode("ascii")}


class LLMClient(ABC):
    """Interface mọi provider phải thỏa. M1: streaming chat; tool-calls/vision thêm ở M4/M2.

    USAGE CAPTURE (spec 25 P1): `last_usage` is a side-channel for `complete()`/`stream()` whose
    return signatures cannot naturally carry a usage dict. Provider implementations MUST set
    `self.last_usage = {prompt_tokens, completion_tokens, total_tokens, cached_tokens} | None` at
    the end of every call (`cached_tokens` = provider prompt-cache hits, spec 44 P1; 0 when
    unreported). It mirrors `AssistantStep.usage` for uniform reads on `step()`.
    Orchestrator reads after each call. Single-turn orderly access; not thread-safe across turns."""

    def __init__(self) -> None:
        # Side-channel for complete()/stream() usage; step() also mirrors here for uniform reads.
        self.last_usage: dict[str, int] | None = None

    @abstractmethod
    def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """Stream nội dung trả lời theo từng delta token. Implementations là async generator.

        `model=None` → adapter dùng model mặc định của nó. Lỗi provider → ném exception cụ thể
        để orchestrator (P1.3) bắt và emit SSE `error` (design §3.5), KHÔNG nuốt thành null.
        """
        ...

    @abstractmethod
    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        """Trả về toàn bộ nội dung 1 lượt (non-stream). Dùng cho mỗi bước ReAct rời rạc của
        orchestrator (design §3.6 `step = LLM(...)`); `stream()` lo câu trả lời cuối ra widget.

        Lỗi provider → propagate exception, KHÔNG nuốt thành chuỗi rỗng (design §3.5).
        """
        ...

    @abstractmethod
    async def step(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AssistantStep:
        """One reasoning turn that MAY call tools (native tool-calls, FR4). `tools` = neutral specs
        `[{"name","description","parameters"}]`; None/empty → no tools offered (model must answer).

        Returns an AssistantStep (content XOR tool_calls in practice). Provider errors propagate —
        KHÔNG nuốt thành step rỗng (design §3.5).
        """
        ...

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
        """One reasoning turn STREAMED. Combines the decision-and-emit of `step()` + `stream()`
        in a single provider call so the orchestrator can forward content tokens to the user as
        soon as they arrive (no upfront non-stream wait). Emits a mix of `StreamTokenDelta`
        (content) and `StreamToolCallDelta` (incremental tool_call fragments) followed by exactly
        one `StreamDone` carrying the final finish_reason, fully-accumulated tool_calls (parsed
        JSON args), and usage dict.

        Default implementation: graceful fallback for providers without native streaming-with-tools
        — calls `step()` and replays its AssistantStep as a single TokenDelta (if content) or a
        single batch of ToolCallDelta events (if tool_calls) followed by StreamDone.
        Functionally identical to step() + stream(), just no TTFT win until the provider overrides.
        Native providers (OpenAI) override this with stream=True+tools=[] for the real win.
        """
        result = await self.step(
            messages,
            tools=tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if result.tool_calls:
            for idx, tc in enumerate(result.tool_calls):
                yield StreamToolCallDelta(
                    index=idx,
                    id=tc.id,
                    name=tc.name,
                    arguments_delta=json.dumps(tc.arguments),
                )
            yield StreamDone(
                finish_reason="tool_calls",
                accumulated_tool_calls=list(result.tool_calls),
                usage=result.usage,
            )
        else:
            if result.content:
                yield StreamTokenDelta(delta=result.content)
            yield StreamDone(
                finish_reason="stop",
                accumulated_tool_calls=[],
                usage=result.usage,
            )
