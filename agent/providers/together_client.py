"""Together AI adapter for `LLMClient` (spec 07 §7 Phase G0).

Together phục vụ model open-weight qua wire format tương thích OpenAI, nên toàn bộ phần khó —
streaming, tool-calls, reasoning split, message shaping — tái dùng `OpenAIClient` nguyên vẹn.
Class này chỉ làm đúng một việc: trỏ base_url + lấy key/model từ `Settings`.

Vì sao subclass chứ không copy: `OpenAIClient` đã implement đủ `LLMClient` và đã nhận
`base_url`/`api_key`. Nhân bản 380 dòng để đổi một URL nghĩa là mọi bug fix sau này phải sửa
hai chỗ — và chỗ thứ hai sẽ bị quên.

Vì sao đây là provider của General Chat: ADR `docs/adr/2026-07-18-hosting-region.md` chốt
open-weight để **provider tách khỏi region** — đổi nơi host không phải đổi model. ADR đó vẫn
PROPOSED (chờ Wyatt ký deployment-region + legal); G0 chỉ land client, KHÔNG kết luận hộ ADR.

Ranh giới cứng (Roadmap §3.0): General Chat **không bao giờ** chạm path gửi cho khách hàng.
Client này phục vụ seller hỏi-đáp; mọi thứ đi ra ngoài tới khách vẫn phải qua
`agent/policy_gate.py`. Ràng buộc đó được cưỡng chế bằng gate import-graph, không bằng đoạn
docstring này.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from openai import AsyncOpenAI

from agent.providers.openai_client import OpenAIClient
from app.config import DEFAULT_TOGETHER_MODEL, get_settings

# Endpoint OpenAI-compatible của Together. Hằng số ở đây (không phải env) vì nó là *danh tính
# của provider*, không phải cấu hình triển khai: trỏ class này sang host khác thì nó không còn
# là TogetherClient nữa. Muốn provider khác → thêm adapter khác.
TOGETHER_BASE_URL = "https://api.together.xyz/v1"


class TogetherClient(OpenAIClient):
    """`OpenAIClient` trỏ Together. Key + model đọc từ `Settings` (env), không hardcode."""

    def __init__(
        self,
        client: AsyncOpenAI | None = None,
        default_model: str | None = None,
        *,
        api_key: str | None = None,
        on_rate_limit: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        settings = get_settings()

        # Model được chốt HẲN ở đây, không để `OpenAIClient` quyết.
        #
        # Bản đầu truyền `default_model or settings.together_model` rồi phó mặc phần còn lại
        # cho `OpenAIClient.__init__`, vốn làm tiếp `default_model or settings.openai_model`.
        # Khi cả hai vế đầu falsy — `TOGETHER_MODEL=` rỗng trong `.env` — client trỏ Together
        # nhưng xin `gpt-4o-mini`, và Together trả 404 `model_not_available`. Lỗi chỉ lộ khi
        # gọi mạng thật; mọi test dùng fake client đều xanh.
        #
        # `.strip()` chứ không chỉ kiểm falsy: `"  "` cũng là model id vô nghĩa, và nó truthy.
        # Sau ba lớp này, `_default_model` KHÔNG THỂ rỗng và KHÔNG THỂ là model OpenAI —
        # không còn đường nào để `settings.openai_model` chạm tới client này.
        resolved_model = (
            (default_model or "").strip()
            or (settings.together_model or "").strip()
            or DEFAULT_TOGETHER_MODEL
        )

        # KHÔNG expose `base_url`: nhận override sẽ biến class này thành "client trỏ đâu cũng
        # được" và làm cái tên nói dối. Key/model thì có override — test cần tiêm, và cả hai
        # đều rơi về `Settings` khi không truyền.
        super().__init__(
            client=client,
            default_model=resolved_model,
            base_url=TOGETHER_BASE_URL,
            api_key=api_key or settings.together_api_key,
            on_rate_limit=on_rate_limit,
        )
