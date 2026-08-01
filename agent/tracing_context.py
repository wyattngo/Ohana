"""ContextVar propagation cho Langfuse trace metadata — user_id, session_id, trace_id.

Endpoint (assistant_chat/chat) set 3 giá trị đầu request; `LangfuseSink.record()` đọc
để pass lên `trace(...)`. Vì sao ContextVar chứ không argument passthrough:

1. **`GenerationRecord` bất khả tri với semantic Langfuse.** `user_id`/`session_id` là
   khái niệm CỦA Langfuse (tab Users/Sessions), không của trace protocol trung tính.
   Nhét vào record = leak abstraction ngược dòng — Sink khác (Sentry/Datadog) không có
   khái niệm này.
2. **Async-safe qua asyncio Task chain.** ContextVar copy per-Task, mutation trong
   endpoint không leak sang request khác — cùng cơ chế starlette dùng cho `request`
   context nội bộ.
3. **TracingClient không cần biết endpoint đang chạy gì.** Adapter/wrapper stack giữ
   nguyên chữ ký; chỉ 1 chỗ đọc (LangfuseSink) và N chỗ set (endpoint).

**Trace grouping (1 request = 1 trace, N generation con).** Langfuse SDK v2: `client.
trace(id=X)` upsert — gọi nhiều lần cùng `id`, các `.generation()` add vào cùng trace.
Endpoint set `trace_id = uuid4()` đầu request → mọi LLM call trong request (kể cả
multi-round drafter) group vào 1 trace UI.

**Không dùng token/reset.** FastAPI mỗi request = coroutine mới, asyncio tự copy context
từ root → mutation local task, không leak sang request kế. Không cần try/finally reset
(pattern quá overhead cho lợi ích 0 ở đây).
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from dataclasses import dataclass

_current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)
_current_session_id: ContextVar[str | None] = ContextVar("current_session_id", default=None)
_current_trace_id: ContextVar[str | None] = ContextVar("current_trace_id", default=None)


@dataclass(frozen=True)
class TracingContext:
    """Snapshot của 3 var tại 1 thời điểm. Frozen để consumer không thể mutate accidentally."""

    user_id: str | None
    session_id: str | None
    trace_id: str | None


def set_tracing_context(
    *,
    user_id: str | None,
    session_id: str | None,
    trace_id: str | None = None,
) -> str:
    """Set 3 var cho current async task. Trả `trace_id` (sinh mới UUID nếu caller không
    truyền — 99% case là auto-generate) để caller có thể log/trả về UI nếu muốn.

    Gọi 1 lần đầu request handler. Multiple lần trong cùng request ⇒ sau đè trước
    (grouping thay đổi). Không recommend nhưng không cấm.
    """
    _current_user_id.set(user_id)
    _current_session_id.set(session_id)
    resolved_trace_id = trace_id or str(uuid.uuid4())
    _current_trace_id.set(resolved_trace_id)
    return resolved_trace_id


def get_tracing_context() -> TracingContext:
    """Snapshot hiện tại. Trả `TracingContext(None, None, None)` khi chưa ai set — tracer
    fallback về hành vi cũ (tạo trace ẩn danh, không group)."""
    return TracingContext(
        user_id=_current_user_id.get(),
        session_id=_current_session_id.get(),
        trace_id=_current_trace_id.get(),
    )


def reset_tracing_context() -> None:
    """TEST-ONLY: reset về `None` cả 3 var. Fixture pytest dùng để test isolation
    (asyncio contextvar auto-isolate per-task, nhưng test sync có thể chia sẻ context)."""
    _current_user_id.set(None)
    _current_session_id.set(None)
    _current_trace_id.set(None)
