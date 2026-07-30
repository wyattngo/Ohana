"""Ollama adapter for `LLMClient` — provider LOCAL qua wire OpenAI-compatible.

Ollama (>=0.x) phục vụ endpoint `/v1` tương thích OpenAI, nên toàn bộ phần khó — streaming,
tool-calls, message shaping — tái dùng `OpenAIClient` nguyên vẹn, cùng lý do với
`TogetherClient`: nhân bản 380 dòng để đổi một URL là mọi bug fix sau này phải sửa hai chỗ.

Khác `TogetherClient` ở HAI điểm có chủ đích:

1. `base_url` đọc từ `Settings` chứ không phải hằng số: vị trí server Ollama là cấu hình
   TRIỂN KHAI (localhost / máy LAN / container), không phải danh tính provider. Together
   chỉ có một endpoint chính chủ; Ollama thì mỗi máy một địa chỉ.
2. Không có API key thật: server local không xác thực. SDK `openai` đòi chuỗi khác rỗng
   nên điền placeholder `"ollama"` (convention chính thức của Ollama docs) — KHÔNG phải
   secret, không có gì để bảo vệ hay xoay vòng.

Chọn provider này bằng env `LLM_PROVIDER=ollama` (nhánh trong `default_llm_client` — I5b).
Dữ liệu KHÔNG rời máy: chuỗi bọc PII/tracing của factory vẫn nguyên (I16), nhưng payload
tới Ollama ở localhost thì "rời máy" vốn không xảy ra — lớp PII lúc này là phòng hờ đổi
`OLLAMA_BASE_URL` sang máy khác, không phải chi phí thừa.

⚠️ Tool-calling phụ thuộc MODEL: `codellama` (mặc định hiện tại — model đang cài) KHÔNG
gọi tool qua wire OpenAI-compat; đường drafter (bắt buộc `emit_reply`) cần model
tool-capable (llama3.1+, qwen2.5, mistral-nemo...). Đổi model là `ollama pull` + env,
không sửa code.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from openai import AsyncOpenAI

from agent.providers.openai_client import OpenAIClient
from app.config import DEFAULT_OLLAMA_MODEL, get_settings

# Placeholder key theo docs Ollama — server local bỏ qua giá trị này. Hằng số module để
# test khẳng định "không có secret nào bị đọc" bằng danh tính, không phải bằng chuỗi lặp.
OLLAMA_FAKE_API_KEY = "ollama"


class OllamaClient(OpenAIClient):
    """`OpenAIClient` trỏ server Ollama local. Base URL + model đọc từ `Settings` (env)."""

    def __init__(
        self,
        client: AsyncOpenAI | None = None,
        default_model: str | None = None,
        *,
        on_rate_limit: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        settings = get_settings()

        # Cùng ba-lớp chống-rỗng với TogetherClient (bài học G1: env set-nhưng-rỗng làm
        # client trỏ Ollama nhưng xin model của OpenAI — lỗi chỉ lộ khi gọi thật).
        resolved_model = (
            (default_model or "").strip()
            or (settings.ollama_model or "").strip()
            or DEFAULT_OLLAMA_MODEL
        )

        # Chuẩn hoá `/v1`: wire OpenAI-compat của Ollama sống Ở DƯỚI `/v1`, còn người viết
        # env thì nhớ mỗi host:port (đã cháy thật 2026-07-30: OLLAMA_BASE_URL=...:11434
        # trong .env ⇒ SDK gọi /chat/completions trần ⇒ 404 "page not found" khó lần —
        # cùng họ env-set-nhưng-sai với bài học G1). Chấp nhận cả hai dạng, tự thêm khi thiếu.
        base = (settings.ollama_base_url or "").strip() or "http://localhost:11434/v1"
        base = base.rstrip("/")
        if not base.endswith("/v1"):
            base = f"{base}/v1"

        super().__init__(
            client=client,
            default_model=resolved_model,
            base_url=base,
            api_key=OLLAMA_FAKE_API_KEY,
            on_rate_limit=on_rate_limit,
        )
