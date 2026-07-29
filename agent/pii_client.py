"""PII filter chokepoint — spec 16 B0.

`PIIFilteringClient` implement chính `LLMClient` ABC (không phải mixin, không phải
helper). Bọc `inner: LLMClient`, redact **mọi** content trong `messages` (kể cả
role=tool và list[ContentPart]) TRƯỚC khi uỷ nhiệm.

Vì sao decorator-over-ABC, không chèn ở call-site:
    Gate `GD0-STEP2` đòi "prompt build ⇒ không có đường bypass". Chèn filter tại
    3 call-site hôm nay thoả điều kiện đó cho hôm nay và hỏng ngày ai thêm call-site
    thứ 4. Bypass-proof nghĩa là "không có chỗ để quên" — điều đó đạt được khi filter
    NẰM CHÍNH TRÊN interface mà mọi call-site đã phải đi qua.

Fail-closed contract:
    Redactor raise ⇒ **KHÔNG** gọi `inner`. Exception bubble lên. Nuốt lỗi rồi vẫn
    forward = PII đã bay (không thu hồi được) với confidence sai (log sẽ báo "đã lọc"
    trong khi thực tế không). Vì thế redact chạy TRƯỚC `await inner`, không nằm trong
    try/except ôm cả block.

Contract KHÔNG cover ở lớp này:
    - Injection wrapping (`<customer_message>` tag) — phase C0, chỗ ráp prompt, khác
      mối quan tâm. Gộp vào wrapper sẽ không test riêng được.
    - Destination log — phase C0.
    - Đo FN-rate của regex — phase D0 (BLOCKED chờ tin khách thật).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from agent.llm_client import (
    AssistantStep,
    ChatMessage,
    ContentPart,
    LLMClient,
    StreamEvent,
    TextPart,
)
from agent.pii import redact


class PIIFilteringClient(LLMClient):
    """Decorator LLMClient — redact content messages trước khi delegate xuống `inner`.

    Cover cả 3 abstract method: `complete`, `step`, `step_stream` (ABC provide default
    `step_stream` gọi lại `step` — cover gián tiếp; giữ override để explicit hoá contract
    khi ABC đổi hoặc provider thêm native impl). `stream` cũng abstract nhưng chưa có
    consumer thật ở GĐ0 — override để không sập subclass check, không optimize gì thêm.
    """

    def __init__(self, inner: LLMClient) -> None:
        super().__init__()
        self._inner = inner
        # Spec 16 C0 destination log: cummulative PII hits của call GẦN NHẤT. Endpoint
        # đọc để log `hits=<dict>` (không log text). Reset đầu mỗi call. Cùng semantic
        # với `last_usage` ở ABC — side-channel single-turn, không thread-safe cross-turn.
        self.last_hits: dict[str, int] = {}

    # ── redact helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    def _redact_part(part: ContentPart) -> ContentPart:
        """Redact 1 ContentPart. Text → redact; ảnh giữ nguyên (image content không đi
        qua regex text)."""
        if part.get("type") == "text":
            text = part.get("text", "")
            redacted: TextPart = {"type": "text", "text": redact(str(text)).text}
            return redacted
        return part

    @classmethod
    def _redact_message(cls, msg: ChatMessage) -> ChatMessage:
        """Redact content của 1 message. Không đụng role / tool_calls / tool_call_id /
        name — chỉ payload text. Kết quả trả về là **message mới**, không mutate input.
        """
        content = msg.get("content")
        if isinstance(content, str):
            new_content: str | list[ContentPart] = redact(content).text
        elif isinstance(content, list):
            new_content = [cls._redact_part(p) for p in content]
        else:
            # Không phải str hay list[ContentPart] → không biết cách xử → fail-closed:
            # để redact() raise TypeError khi ai đó thêm shape mới mà chưa cập nhật wrapper.
            new_content = redact(content)

        new_msg: ChatMessage = {"role": msg["role"], "content": new_content}
        # Copy metadata giữ nguyên (tool_calls giữ, tool_call_id giữ, name giữ)
        if "tool_calls" in msg:
            new_msg["tool_calls"] = msg["tool_calls"]
        if "tool_call_id" in msg:
            new_msg["tool_call_id"] = msg["tool_call_id"]
        if "name" in msg:
            new_msg["name"] = msg["name"]
        return new_msg

    @classmethod
    def _redact_messages(cls, messages: list[ChatMessage]) -> list[ChatMessage]:
        """Redact toàn bộ danh sách messages. Chạy TRƯỚC bất kỳ `await inner.*` nào.

        Redactor raise → exception bubble ngay tại đây, `inner` KHÔNG bao giờ được gọi.
        Đây là contract fail-closed của B0.
        """
        return [cls._redact_message(m) for m in messages]

    def _redact_and_track(self, messages: list[ChatMessage]) -> list[ChatMessage]:
        """B0 fail-closed redact + C0 hits tracking. Reset `last_hits` đầu mỗi call, gộp
        `hits` từ mọi message text sau redact. Endpoint đọc `last_hits` để log destination
        (spec 16 C0: `hits=<dict>` KHÔNG log text).

        Dùng bare `redact` (top-level import) để test monkeypatch `pc.redact` áp cứng
        (fail-closed test của B0). Import local trong function tạo binding mới mỗi call,
        làm monkeypatch vô hiệu — bug đã dính khi ship C0.
        """
        total: dict[str, int] = {}

        def _track_str(s: str) -> str:
            r = redact(s)
            for k, v in r.hits.items():
                total[k] = total.get(k, 0) + v
            return r.text

        redacted: list[ChatMessage] = []
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, str):
                new_content: str | list[ContentPart] = _track_str(content)
            elif isinstance(content, list):
                new_parts: list[ContentPart] = []
                for part in content:
                    if part.get("type") == "text":
                        new_parts.append(
                            {"type": "text", "text": _track_str(str(part.get("text", "")))}
                        )
                    else:
                        new_parts.append(part)
                new_content = new_parts
            else:
                new_content = redact(content)  # fail-closed cho shape lạ

            new_msg: ChatMessage = {"role": msg["role"], "content": new_content}
            if "tool_calls" in msg:
                new_msg["tool_calls"] = msg["tool_calls"]
            if "tool_call_id" in msg:
                new_msg["tool_call_id"] = msg["tool_call_id"]
            if "name" in msg:
                new_msg["name"] = msg["name"]
            redacted.append(new_msg)

        self.last_hits = total
        return redacted

    # ── delegate ────────────────────────────────────────────────────────────────────

    async def complete(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> str:
        redacted = self._redact_and_track(messages)
        result = await self._inner.complete(
            redacted, model=model, temperature=temperature, max_tokens=max_tokens
        )
        self.last_usage = self._inner.last_usage
        return result

    async def step(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AssistantStep:
        redacted = self._redact_and_track(messages)
        result = await self._inner.step(
            redacted,
            tools=tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        self.last_usage = self._inner.last_usage
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
        """Override tường minh dù ABC provide default fallback (default gọi lại `step`
        nên vẫn qua wrapper). Explicit hoá để: (1) nếu inner có native step_stream, dùng
        native đó với messages đã redact; (2) người đọc thấy ngay 3 method đều cover
        thay vì phải đọc ABC để suy ra.
        """
        redacted = self._redact_and_track(messages)
        async for event in self._inner.step_stream(
            redacted,
            tools=tools,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning=reasoning,
        ):
            yield event
        self.last_usage = self._inner.last_usage

    def stream(
        self,
        messages: list[ChatMessage],
        *,
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
    ) -> AsyncIterator[str]:
        """4th abstract method — Ohana GĐ0 chưa có consumer thật của `stream` (chat
        endpoint dùng `step`; drafter cũng `step`). Nhưng ABC đòi override, và bỏ qua
        nghĩa là subclass check sẽ TypeError. Giữ nguyên semantic redact-then-delegate;
        nếu ai thêm consumer sau này, wrapper đã sẵn.
        """
        redacted = self._redact_and_track(messages)
        return self._inner.stream(
            redacted, model=model, temperature=temperature, max_tokens=max_tokens
        )
