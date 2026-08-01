"""Langfuse sink — NƠI DUY NHẤT import `langfuse` (I5, cùng luật với SDK provider).

Self-host localhost:3000 (docker-compose), không dữ liệu nào rời hạ tầng — đó là điều
kiện để golden set C4 (PII thật) ở lại trong nước. SDK v2 khớp server `langfuse/langfuse:2`.

I16 không cưỡng chế ở ĐÂY mà ở vị trí stack trong `agent/llm_client.py::default_llm_client`
(trace nằm dưới PII filter). File này chỉ cần đúng một điều: đừng tự đi lấy dữ liệu ở đâu
khác ngoài `GenerationRecord` được đưa cho nó.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent.llm_client import GenerationRecord, TraceSink

logger = logging.getLogger(__name__)


class LangfuseSink:
    """`TraceSink` ghi vào Langfuse self-host.

    SDK v2 tự batch + gửi nền (thread riêng) — `record()` không block đường trả lời.
    Không gọi `flush()` mỗi record: đó là việc của batcher; process ngắn hạn (test,
    script) cần chắc chắn đã gửi thì gọi `flush()` tường minh.
    """

    def __init__(self, *, public_key: str, secret_key: str, host: str) -> None:
        from langfuse import Langfuse

        self._client = Langfuse(public_key=public_key, secret_key=secret_key, host=host)

    def record(self, gen: GenerationRecord) -> None:
        # Trace + generation cùng start_time để UI grouping timeline chuẩn. Nếu để SDK
        # tự stamp (không pass), trace = t0, generation = t0+ε → span 0-duration →
        # cột End Time trống + Latency null trên UI.
        #
        # Đọc TracingContext (contextvar set bởi endpoint) để pass user_id + session_id
        # + trace_id lên Langfuse — populate tab Users/Sessions + group multi-step trong
        # 1 request thành 1 trace (SDK v2 upsert theo `id`). Context rỗng ⇒ fallback về
        # hành vi cũ (trace ẩn danh, không group) — non-breaking cho call-site chưa wire.
        from agent.tracing_context import get_tracing_context

        ctx = get_tracing_context()
        trace_kwargs: dict[str, object] = {
            "name": f"llm.{gen.op}",
            "start_time": gen.started_at,
        }
        if ctx.trace_id is not None:
            trace_kwargs["id"] = ctx.trace_id
        if ctx.user_id is not None:
            trace_kwargs["user_id"] = ctx.user_id
        if ctx.session_id is not None:
            trace_kwargs["session_id"] = ctx.session_id
        trace = self._client.trace(**trace_kwargs)
        trace.generation(
            name=gen.op,
            model=gen.model,
            start_time=gen.started_at,
            end_time=gen.ended_at,
            input=gen.input_messages,
            output=gen.output
            if not gen.tool_calls
            else {
                "content": gen.output,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in gen.tool_calls
                ],
            },
            usage={
                "promptTokens": gen.usage.get("prompt_tokens"),
                "completionTokens": gen.usage.get("completion_tokens"),
                "totalTokens": gen.usage.get("total_tokens"),
            }
            if gen.usage
            else None,
            level="ERROR" if gen.error else None,
            status_message=gen.error,
        )

    def flush(self) -> None:
        self._client.flush()


def default_trace_sink() -> TraceSink | None:
    """Factory — None khi thiếu key (tracing TẮT, app chạy bình thường).

    Trace là phụ trợ: thiếu config không được sập app, không warning ồn ào mỗi call —
    log đúng MỘT dòng info lúc wiring để người vận hành biết vì sao dashboard trống.
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        logger.info("langfuse: thiếu LANGFUSE_PUBLIC_KEY/SECRET_KEY — tracing tắt")
        return None
    return LangfuseSink(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key,
        host=settings.langfuse_host,
    )
