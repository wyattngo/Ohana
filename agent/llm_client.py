"""Provider-agnostic LLM interface.

Mọi truy cập model đi qua `LLMClient` (R6 pair: llm_client.py ↔ providers/*). Đổi nhà cung cấp
(OpenAI → Claude/khác) = thêm một adapter mới, KHÔNG sửa agent core (design §5.2).
"""

from __future__ import annotations

import base64
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal, NotRequired, Protocol, TypedDict

logger = logging.getLogger(__name__)


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
    Orchestrator reads after each call. Single-turn orderly access; not thread-safe across turns.

    MODEL CAPTURE: `last_model` side-channel — cùng bài `last_usage` — expose model đã RESOLVE
    (sau khi adapter apply `model or self._default_model`). Vì sao cần: `TracingClient` bọc
    ngoài chỉ thấy tham số `model=` mà caller truyền vào — mà mọi call-site production
    (api.chat, api.assistant_chat, agent.drafter) gọi `.step(messages)` KHÔNG truyền model,
    nên `TracingClient` không có model để đưa lên `GenerationRecord.model` → sink Langfuse
    nhận `model=None` → "Model costs" / "Model Usage" report "No data" (Langfuse group theo
    tên model). Adapter set `last_model` cuối mỗi call, tracer đọc từ `_inner.last_model`
    thay cho tham số. Single-turn contract giống `last_usage`."""

    def __init__(self) -> None:
        # Side-channel for complete()/stream() usage; step() also mirrors here for uniform reads.
        self.last_usage: dict[str, int] | None = None
        # Side-channel for resolved model name — set bởi adapter (`model or self._default_model`)
        # để TracingClient/PIIFilteringClient đọc mà không phải re-derive default logic.
        self.last_model: str | None = None

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


# ── Tracing hook (I16) ──────────────────────────────────────────────────────────────
#
# Vị trí trong stack là toàn bộ cơ chế cưỡng chế I16 ("Langfuse chỉ nhận Scrubbed"):
#
#     PIIFilteringClient( TracingClient( TogetherClient ) )
#                        ^^^^^^^^^^^^^^
#
# `TracingClient` nằm DƯỚI PII filter, nên messages nó thấy — và mọi thứ nó đưa cho
# sink — đã đi qua `redact()` rồi. Không có đường code nào đưa text thô tới sink mà
# không sửa thứ tự bọc trong `default_llm_client()`. Gate: tests/test_langfuse_wiring.py
# ::test_sink_never_sees_raw_pii — đừng đổi thứ tự bọc mà không đọc test đó trước.
#
# SDK Langfuse KHÔNG import ở đây (I5): sink cụ thể sống ở
# `agent/providers/langfuse_tracer.py`; module này chỉ khai Protocol.


@dataclass
class GenerationRecord:
    """Một lượt gọi model đã hoàn tất, shape trung tính để sink ghi đi đâu tùy nó.

    `input_messages` là messages ĐÃ REDACT (xem comment vị trí stack ở trên). `error`
    non-None khi provider ném — record vẫn được ghi để trace nhìn thấy cả lượt hỏng."""

    op: str  # "complete" | "stream" | "step" | "step_stream"
    model: str | None
    input_messages: list[ChatMessage]
    output: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, int] | None = None
    error: str | None = None


class TraceSink(Protocol):
    """Đích nhận GenerationRecord. Impl KHÔNG được ném từ `record()` — nhưng
    `TracingClient` vẫn nuốt + log mọi exception: trace là phụ trợ, một sink hỏng
    không được phép làm hỏng đường trả lời khách."""

    def record(self, gen: GenerationRecord) -> None: ...


class TracingClient(LLMClient):
    """Decorator LLMClient — ghi mỗi lượt gọi model vào `sink`, delegate mọi thứ xuống
    `inner`. Đặt DƯỚI PIIFilteringClient trong stack (xem comment I16 ở trên).

    Sink lỗi ⇒ log warning, KHÔNG propagate. Provider lỗi ⇒ ghi record kèm `error` rồi
    re-raise nguyên vẹn (design §3.5 — không nuốt exception của đường chính)."""

    def __init__(self, inner: LLMClient, sink: TraceSink) -> None:
        super().__init__()
        self._inner = inner
        self._sink = sink

    def _emit(self, gen: GenerationRecord) -> None:
        try:
            self._sink.record(gen)
        except Exception:  # noqa: BLE001 — mọi lỗi sink đều không được chạm đường chính
            logger.warning("trace sink lỗi — bỏ qua record op=%s", gen.op, exc_info=True)

    async def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        chunks: list[str] = []
        try:
            async for delta in self._inner.stream(
                messages, model=model, temperature=temperature, max_tokens=max_tokens
            ):
                chunks.append(delta)
                yield delta
        except Exception as exc:
            self.last_usage = self._inner.last_usage
            self.last_model = self._inner.last_model
            self._emit(
                GenerationRecord(
                    op="stream",
                    model=self._inner.last_model,
                    input_messages=messages,
                    output="".join(chunks) or None,
                    usage=self._inner.last_usage,
                    error=repr(exc),
                )
            )
            raise
        self.last_usage = self._inner.last_usage
        self.last_model = self._inner.last_model
        self._emit(
            GenerationRecord(
                op="stream",
                model=self._inner.last_model,
                input_messages=messages,
                output="".join(chunks),
                usage=self._inner.last_usage,
            )
        )

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        try:
            out = await self._inner.complete(
                messages, model=model, temperature=temperature, max_tokens=max_tokens
            )
        except Exception as exc:
            self.last_usage = self._inner.last_usage
            self.last_model = self._inner.last_model
            self._emit(
                GenerationRecord(
                    op="complete",
                    model=self._inner.last_model,
                    input_messages=messages,
                    output=None,
                    usage=self._inner.last_usage,
                    error=repr(exc),
                )
            )
            raise
        self.last_usage = self._inner.last_usage
        self.last_model = self._inner.last_model
        self._emit(
            GenerationRecord(
                op="complete",
                model=self._inner.last_model,
                input_messages=messages,
                output=out,
                usage=self._inner.last_usage,
            )
        )
        return out

    async def step(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AssistantStep:
        try:
            result = await self._inner.step(
                messages, tools=tools, model=model, temperature=temperature, max_tokens=max_tokens
            )
        except Exception as exc:
            self.last_usage = self._inner.last_usage
            self.last_model = self._inner.last_model
            self._emit(
                GenerationRecord(
                    op="step",
                    model=self._inner.last_model,
                    input_messages=messages,
                    output=None,
                    usage=self._inner.last_usage,
                    error=repr(exc),
                )
            )
            raise
        self.last_usage = self._inner.last_usage
        self.last_model = self._inner.last_model
        self._emit(
            GenerationRecord(
                op="step",
                model=self._inner.last_model,
                input_messages=messages,
                output=result.content,
                tool_calls=list(result.tool_calls),
                usage=result.usage,
            )
        )
        return result

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
        # Delegate THẲNG xuống inner (không dùng fallback của ABC): nếu inner có native
        # streaming-with-tools thì đi đường đó; record ghi tại StreamDone.
        chunks: list[str] = []
        done: StreamDone | None = None
        try:
            async for event in self._inner.step_stream(
                messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                reasoning=reasoning,
            ):
                if isinstance(event, StreamTokenDelta):
                    chunks.append(event.delta)
                elif isinstance(event, StreamDone):
                    done = event
                yield event
        except Exception as exc:
            self.last_usage = self._inner.last_usage
            self.last_model = self._inner.last_model
            self._emit(
                GenerationRecord(
                    op="step_stream",
                    model=self._inner.last_model,
                    input_messages=messages,
                    output="".join(chunks) or None,
                    usage=self._inner.last_usage,
                    error=repr(exc),
                )
            )
            raise
        self.last_usage = self._inner.last_usage
        self.last_model = self._inner.last_model
        self._emit(
            GenerationRecord(
                op="step_stream",
                model=self._inner.last_model,
                input_messages=messages,
                output="".join(chunks) or None,
                tool_calls=list(done.accumulated_tool_calls) if done else [],
                usage=done.usage if done else self._inner.last_usage,
            )
        )


def default_llm_client() -> LLMClient:
    """Factory — chỗ DUY NHẤT call-site lấy LLM client (I5b: đổi provider chỉ sửa ở đây).
    Chuyển từ `api/chat.py::get_llm_client` sang seam ở fix I5b; call-site giữ phần cache.

    Import TRONG hàm, cả ba:
    - `TogetherClient`: SDK `openai` validate credentials ngay trong `AsyncOpenAI.__init__`
      (kiểm ở G0), thiếu `TOGETHER_API_KEY` lúc import sẽ sập cả app — dựng lười ⇒ hỏng
      đúng phạm vi route chat.
    - `alert_service`: module-level `from app import alert_service` tái lập coupling
      provider→app mà G0 đã gỡ (spec 12 W0, ISSUE-010). Hook đếm 429 fire-and-forget,
      re-raise `RateLimitError` nguyên vẹn.
    - `PIIFilteringClient` (spec 16 B0): bọc để MỌI call-site đi qua LLMClient tự động
      redact PII trước khi payload rời máy — chokepoint nằm trên interface, call-site
      thứ N thêm sau vẫn an toàn.

    Thứ tự bọc là I16 (xem block comment TracingClient): PII NGOÀI, trace TRONG —
    Langfuse chỉ có thể thấy text đã redact. Không có key Langfuse ⇒ sink None ⇒
    không có tầng TracingClient, đường gọi y như trước khi wire.
    """
    from agent.pii_client import PIIFilteringClient
    from agent.providers.langfuse_tracer import default_trace_sink
    from app import alert_service
    from app.config import get_settings

    # Nhánh provider theo env LLM_PROVIDER (I5b: đổi provider = đổi env, call-site không
    # đổi). Giá trị lạ ⇒ raise, KHÔNG rơi âm thầm về Together: typo trong env mà vẫn chạy
    # provider trả phí là hỏng kiểu đắt tiền, và lỗi lúc dựng client thì lộ ngay khi boot.
    provider = (get_settings().llm_provider or "together").strip().lower()
    inner: LLMClient
    if provider == "ollama":
        from agent.providers.ollama_client import OllamaClient

        inner = OllamaClient(on_rate_limit=alert_service.record_provider_429)
    elif provider == "together":
        from agent.providers.together_client import TogetherClient

        inner = TogetherClient(on_rate_limit=alert_service.record_provider_429)
    else:
        raise ValueError(f"LLM_PROVIDER={provider!r} không hỗ trợ — chọn 'together' hoặc 'ollama'")
    sink = default_trace_sink()
    if sink is not None:
        inner = TracingClient(inner, sink)
    return PIIFilteringClient(inner)
