"""Together provider gate — spec 07 Phase G0.

Viết TRƯỚC khi `agent/providers/together_client.py` tồn tại — kỳ vọng RED.

Phase này mua 2 thứ:

  1. **Together chạy được mà KHÔNG nhân bản 380 dòng.** `OpenAIClient` đã implement đủ
     `LLMClient` và đã nhận `base_url`; Together là OpenAI-compatible. Thứ chặn nó là đúng
     một import module-level (`from app import alert_service`, ISSUE-010) khiến cả module
     không import nổi. G0 đổi coupling đó thành hook **injected** — telemetry là thứ nên
     tiêm vào, không phải thứ để module chết vì thiếu nó.

  2. **Hành vi 429 KHÔNG được đổi.** Đây là chỗ dễ "sửa cho xanh" nhất: bỏ import rồi tiện
     tay nuốt luôn exception hoặc thêm retry. Test dưới ép re-raise nguyên vẹn.

KHÔNG gọi mạng: mọi test inject fake client. Smoke thật với Together là việc của §10.
"""

from __future__ import annotations

from typing import Any

import pytest


def _rate_limit_error() -> Any:
    """Dựng `openai.RateLimitError` THẬT.

    Không dùng exception giả: nếu test bắt một class tự chế, nó sẽ pass ngay cả khi code đổi
    sang bắt/nuốt đúng loại lỗi thật của SDK. `RateLimitError.__init__` đọc `response.request`,
    nên phải có httpx.Response gắn Request thật — đó là lý do có hàm này thay vì gọi trực tiếp.
    """
    import httpx
    import openai

    request = httpx.Request("POST", "https://api.together.xyz/v1/chat/completions")
    response = httpx.Response(429, request=request)
    return openai.RateLimitError("429", response=response, body=None)


class _FakeCompletions:
    """Đứng thay `client.chat.completions`. Ghi lại kwargs, hoặc ném lỗi đã cấu hình."""

    def __init__(self, raises: BaseException | None = None) -> None:
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        return {"ok": True}


class _FakeAsyncOpenAI:
    def __init__(self, raises: BaseException | None = None) -> None:
        self.completions = _FakeCompletions(raises)
        self.chat = self

    # Ghi lại để test xác nhận base_url/api_key được truyền đúng khi KHÔNG inject client.
    base_url: str | None = None
    api_key: str | None = None


def test_together_client_is_an_llm_client() -> None:
    """`TogetherClient` phải là `LLMClient` thật — không phải một class rời mang tên giống."""
    from agent.llm_client import LLMClient
    from agent.providers.together_client import TogetherClient

    assert issubclass(TogetherClient, LLMClient)


def test_together_client_targets_together_endpoint_and_settings() -> None:
    """base_url trỏ Together, model + key lấy từ Settings — KHÔNG hardcode key/model trong code.

    Nếu ai đó hardcode model id vào class, đổi model sẽ phải sửa code thay vì sửa env — đúng
    cái debt §8.2 Roadmap nói (`cấm hardcode model id`).
    """
    import inspect

    from agent.providers import together_client as mod

    src = inspect.getsource(mod)
    assert "api.together.xyz" in src, "TogetherClient phải trỏ endpoint Together"
    # Key tuyệt đối không được nằm trong source.
    assert "sk-" not in src and "Bearer " not in src, "nghi ngờ key/hardcode auth trong source"


def test_together_client_reads_model_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Đổi `TOGETHER_MODEL` trong env → client dùng model mới, không cần sửa code."""
    monkeypatch.setenv("TOGETHER_API_KEY", "test-key-not-real")
    monkeypatch.setenv("TOGETHER_MODEL", "vendor/some-other-model")

    from app.config import Settings

    s = Settings()
    assert s.together_model == "vendor/some-other-model"
    assert s.together_api_key == "test-key-not-real"


def test_together_model_default_is_the_signed_choice(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default = model Wyatt ký ở §14 (PRE-G02). Không có key/model trong env vẫn phải có
    default hợp lệ, để app khởi động được thay vì raise lúc import.

    `delenv` là bắt buộc, không phải cho gọn: bản đầu của test này KHÔNG xoá env và chỉ xanh
    vì môi trường test tình cờ không có `TOGETHER_MODEL`. Production đọc `.env` thì khác —
    xem `test_empty_env_var_does_not_defeat_the_default` ngay dưới.
    """
    monkeypatch.delenv("TOGETHER_MODEL", raising=False)
    from app.config import Settings

    assert Settings().together_model == "meta-llama/Llama-3.3-70B-Instruct-Turbo"


def test_empty_env_var_does_not_defeat_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """`TOGETHER_MODEL=` (khai báo nhưng RỖNG) phải rơi về default, KHÔNG phải về chuỗi rỗng.

    Đây là trạng thái THẬT trong `.env` của Wyatt 2026-07-19, sinh ra từ chính `.env.example`
    tôi viết ở G0 (để trống mọi giá trị). pydantic-settings coi biến-tồn-tại-nhưng-rỗng là một
    giá trị hợp lệ và ghi đè default — nên `together_model` thành `""`.

    Chuỗi rỗng là falsy, nên nó trượt tiếp qua mọi `or` phía dưới và kết thúc ở model của
    provider KHÁC. Lỗi không ồn ào ở tầng config; nó nổ ở tầng HTTP với 404 model_not_available.
    """
    monkeypatch.setenv("TOGETHER_MODEL", "")
    from app.config import Settings

    assert Settings().together_model == "meta-llama/Llama-3.3-70B-Instruct-Turbo", (
        "env rỗng đã ghi đè default — cái bẫy này áp cho MỌI field str có default"
    )


def test_together_client_never_falls_back_to_the_openai_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bất biến cấu trúc: `TogetherClient` KHÔNG có đường nào kết thúc trên `openai_model`.

    Bug thật (G1 smoke, 2026-07-19): `TOGETHER_MODEL` rỗng ⇒ `default_model or
    settings.together_model` = `""` ⇒ `OpenAIClient.__init__` chạy tiếp `"" or
    settings.openai_model` ⇒ client trỏ Together nhưng xin `gpt-4o-mini` ⇒ 404.

    Test dựng đúng cái bẫy đó: `OPENAI_MODEL` để một giá trị nhận ra được, `TOGETHER_MODEL`
    rỗng. Nếu tên OpenAI xuất hiện trong `_default_model` thì fallback vẫn rò.
    """
    from agent.providers.together_client import TogetherClient
    from app.config import get_settings

    monkeypatch.setenv("TOGETHER_API_KEY", "test-key-not-real")
    monkeypatch.setenv("TOGETHER_MODEL", "")
    monkeypatch.setenv("OPENAI_MODEL", "openai/KHONG-DUOC-XUAT-HIEN")
    get_settings.cache_clear()
    try:
        client = TogetherClient()
        assert "KHONG-DUOC-XUAT-HIEN" not in client._default_model, (
            "TogetherClient rơi về model của OpenAI — sẽ 404 trên Together"
        )
        assert client._default_model == "meta-llama/Llama-3.3-70B-Instruct-Turbo"
    finally:
        get_settings.cache_clear()


def test_explicit_blank_model_argument_is_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Truyền `default_model="   "` (rỗng/khoảng trắng) cũng phải rơi về default, không được
    gửi model rỗng lên API. Cùng gốc với bug trên: falsy-nhưng-không-None trượt qua `or`."""
    from agent.providers.together_client import TogetherClient
    from app.config import get_settings

    monkeypatch.setenv("TOGETHER_API_KEY", "test-key-not-real")
    monkeypatch.delenv("TOGETHER_MODEL", raising=False)
    get_settings.cache_clear()
    try:
        assert TogetherClient(default_model="   ")._default_model.strip() != ""
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_rate_limit_reraised_even_when_hook_itself_throws() -> None:
    """Hook nổ thì RateLimitError vẫn phải tới tay caller NGUYÊN VẸN.

    Reviewer bắt được lỗ này (spec 07 G0 review round 1). Hook là code do caller tiêm — đếm
    số, đẩy metric, ghi DB. Nếu nó timeout/throw mà ta để lỗi đó bay lên, RateLimitError bị
    THAY bằng một exception không liên quan; mọi nhánh backoff phía trên đều phân loại theo
    KIỂU exception, nên caller sẽ ngừng nhận ra "đang bị rate limit" đúng vào lúc sự cố đang
    xảy ra — cũng chính là lúc telemetry dễ hỏng nhất.

    Bản gốc (`alert_service`) cũng mang đúng lỗ này; G0 vá luôn thay vì bê nguyên sang.
    """
    import openai

    from agent.providers.openai_client import OpenAIClient

    async def exploding_hook() -> None:
        raise RuntimeError("telemetry backend down")

    fake = _FakeAsyncOpenAI(raises=_rate_limit_error())
    client = OpenAIClient(client=fake, default_model="m", on_rate_limit=exploding_hook)  # type: ignore[arg-type]

    # RateLimitError, KHÔNG phải RuntimeError — đây là toàn bộ nội dung của test.
    with pytest.raises(openai.RateLimitError):
        await client._create(model="m", messages=[])


@pytest.mark.asyncio
async def test_hook_failure_log_does_not_carry_the_hook_error_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log lỗi hook chỉ được mang TÊN KIỂU exception, không mang message của nó.

    Bối cảnh (spec 07 G0 review round 2): reviewer báo `logger.exception` in local variables
    ra log → lộ tin nhắn khách. Kiểm thực nghiệm cho thấy điều đó SAI — traceback CPython in
    dòng lệnh nguồn, không bao giờ in giá trị biến local.

    Nhưng rủi ro THẬT nằm ở chỗ ngược lại: **message của chính exception hook**. Một client
    DB/HTTP thường nhét nguyên payload nó nghẹn vào message lỗi. Payload đó là tin nhắn khách
    hàng — thứ PDPL không cho phép ta rải vào log ứng dụng. Nên ta log tên kiểu, hết.
    """
    import logging

    import openai

    from agent.providers.openai_client import OpenAIClient

    leak = "SO-THE-CUA-KHACH-0123456789"

    async def leaky_hook() -> None:
        raise RuntimeError(f"insert failed for row: {leak}")

    fake = _FakeAsyncOpenAI(raises=_rate_limit_error())
    client = OpenAIClient(client=fake, default_model="m", on_rate_limit=leaky_hook)  # type: ignore[arg-type]

    with caplog.at_level(logging.WARNING), pytest.raises(openai.RateLimitError):
        await client._create(model="m", messages=[{"role": "user", "content": leak}])

    blob = caplog.text
    assert leak not in blob, "message lỗi của hook (chứa dữ liệu khách) đã lọt vào log"
    assert "RuntimeError" in blob, "vẫn phải log ĐƯỢC tên kiểu lỗi, nếu không thì mất tín hiệu"


def test_together_base_url_reaches_the_real_client_at_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bổ sung cho test grep source ở trên — grep chỉ chứng minh chuỗi CÓ MẶT trong file.

    Nếu ai đó thêm code ghi đè `base_url` sau `__init__`, hoặc `super().__init__()` bỏ qua nó,
    test grep vẫn xanh trong khi client thật trỏ sai endpoint. Đây kiểm giá trị THẬT trên
    object đã dựng xong.
    """
    from agent.providers.together_client import TOGETHER_BASE_URL, TogetherClient
    from app.config import get_settings

    monkeypatch.setenv("TOGETHER_API_KEY", "test-key-not-real")
    get_settings.cache_clear()
    try:
        client = TogetherClient()  # AsyncOpenAI thật, không gọi mạng
        assert str(client._client.base_url).rstrip("/") == TOGETHER_BASE_URL.rstrip("/")
        assert "together" in str(client._client.base_url)
    finally:
        get_settings.cache_clear()


def test_missing_key_fails_at_construction_not_at_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ghi lại sự thật vận hành phát hiện lúc viết test này (spec 07 G0 review round 2).

    SDK `openai` validate credentials ngay trong `AsyncOpenAI.__init__`, nên thiếu key thì vỡ
    lúc DỰNG client chứ không phải lúc gọi API. Hệ quả cho G1: **không được** dựng
    `TogetherClient` ở module scope `app/main.py` — deploy quên set key sẽ làm CẢ APP không
    boot, thay vì chỉ endpoint chat trả lỗi. Test này khoá sự thật đó lại để G1 không phải
    khám phá lại bằng một sự cố production.
    """
    import openai

    from agent.providers.together_client import TogetherClient
    from app.config import get_settings

    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    get_settings.cache_clear()
    try:
        with pytest.raises(openai.OpenAIError):
            TogetherClient()
    finally:
        get_settings.cache_clear()


def test_api_key_does_not_leak_into_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §7 bước 1(e): key KHÔNG được lộ qua `repr()`.

    `repr()` là chỗ rò rỉ âm thầm nhất: nó đi vào traceback, log lỗi, và pytest assertion
    output. Một client rò key trong repr sẽ phát tán key ra mọi log stack trace của production
    mà không ai chủ động in nó ra cả.

    Sentinel phải là chuỗi không xuất hiện tự nhiên ở đâu khác, để `not in` có ý nghĩa thật.

    KHÔNG inject fake client ở đây — cố ý. Nếu tiêm client giả thì `api_key` không bao giờ
    chạm tới object, và test sẽ pass kể cả khi client in thẳng key ra repr: một assertion
    không thể fail. Phải để `AsyncOpenAI` thật giữ key (dựng nó KHÔNG gọi mạng) thì phép thử
    mới có nội dung.
    """
    from agent.providers.together_client import TogetherClient
    from app.config import get_settings

    sentinel = "KEY-SENTINEL-cf83e1357eefb8bd"
    monkeypatch.setenv("TOGETHER_API_KEY", sentinel)
    get_settings.cache_clear()
    try:
        client = TogetherClient()  # AsyncOpenAI thật, giữ key thật — không có request nào bay đi
        assert client._client.api_key == sentinel, (
            "key chưa từng tới được client — test repr sẽ vô nghĩa nếu không có assert này"
        )
        assert sentinel not in repr(client), "API key rò qua repr() — sẽ vào mọi traceback"
        assert sentinel not in str(client)
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_rate_limit_still_reraises_unchanged_without_hook() -> None:
    """429 phải re-raise NGUYÊN khi không có hook — không nuốt, không retry.

    Đây là bất biến quan trọng nhất của G0: gỡ `alert_service` KHÔNG được nhân tiện đổi
    semantics lỗi. Caller phía trên vẫn phải thấy RateLimitError y như trước.
    """
    import openai

    from agent.providers.openai_client import OpenAIClient

    fake = _FakeAsyncOpenAI(raises=_rate_limit_error())
    client = OpenAIClient(client=fake, default_model="m")  # type: ignore[arg-type]

    with pytest.raises(openai.RateLimitError):
        await client._create(model="m", messages=[])


@pytest.mark.asyncio
async def test_rate_limit_hook_is_called_then_exception_reraised() -> None:
    """Có hook: hook được gọi RỒI exception vẫn re-raise. Hook là fire-and-forget telemetry,
    không phải error handler."""
    import openai

    from agent.providers.openai_client import OpenAIClient

    seen: list[str] = []

    async def hook() -> None:
        seen.append("called")

    fake = _FakeAsyncOpenAI(raises=_rate_limit_error())
    client = OpenAIClient(client=fake, default_model="m", on_rate_limit=hook)  # type: ignore[arg-type]

    with pytest.raises(openai.RateLimitError):
        await client._create(model="m", messages=[])
    assert seen == ["called"], "hook phải được gọi đúng 1 lần trước khi re-raise"


def test_openai_client_no_longer_imports_alert_service_at_module_level() -> None:
    """ISSUE-010 không còn khoá module này.

    Trước G0: `from app import alert_service` ở top-level làm CẢ module unimportable vì
    `app/alert_service.py` chưa port. Sau G0: telemetry là hook tiêm vào, module import sạch.
    ISSUE-010 vẫn OPEN cho phần alerting THẬT — G0 không port alert_service.
    """
    import inspect

    from agent.providers import openai_client as mod

    src = inspect.getsource(mod)
    assert "from app import alert_service" not in src, (
        "coupling module-level với alert_service vẫn còn — G0 chưa xong"
    )
