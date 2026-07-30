"""Ollama provider gate — adapter local qua wire OpenAI-compat + nhánh LLM_PROVIDER.

Cùng họ với tests/test_together_client.py (G0): KHÔNG gọi mạng, mọi assert đứng trên
Settings/cấu trúc class. Smoke thật với server Ollama là tests/test_ollama_live.py
(marker `live`, deselect mặc định).

Ba nhóm chốt:
1. Adapter đúng seam: là `LLMClient` thật, không secret trong source, model/base_url
   từ env với ba-lớp chống-rỗng (bài học G1: env set-nhưng-rỗng trượt qua `or`).
2. Nhánh factory: `LLM_PROVIDER=ollama` → OllamaClient; default vẫn Together (không
   đổi hành vi hiện có); giá trị lạ → raise (typo env không được âm thầm chạy
   provider trả phí).
3. Không có field API key nào cho Ollama trong Settings — server local không xác thực,
   một field key sẽ gợi ý sai rằng có secret cần quản.
"""

from __future__ import annotations

import pytest


def test_ollama_client_is_an_llm_client() -> None:
    from agent.llm_client import LLMClient
    from agent.providers.ollama_client import OllamaClient

    assert issubclass(OllamaClient, LLMClient)


def test_ollama_client_source_has_no_secret() -> None:
    import inspect

    from agent.providers import ollama_client as mod

    src = inspect.getsource(mod)
    assert "sk-" not in src and "Bearer " not in src, "nghi ngờ key/hardcode auth trong source"


def test_ollama_settings_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Đổi OLLAMA_MODEL/OLLAMA_BASE_URL trong env → Settings mới, không sửa code."""
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:14b")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://10.0.0.5:11434/v1")

    from app.config import Settings

    s = Settings()
    assert s.ollama_model == "qwen2.5:14b"
    assert s.ollama_base_url == "http://10.0.0.5:11434/v1"


def test_empty_env_does_not_defeat_ollama_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env khai-nhưng-rỗng phải rơi về default (cái bẫy pydantic-settings của G1 áp cho
    MỌI field str có default — adapter còn thêm lớp `.strip() or default` sau Settings)."""
    monkeypatch.setenv("OLLAMA_MODEL", "")
    monkeypatch.setenv("OLLAMA_BASE_URL", "")

    from agent.providers.ollama_client import OllamaClient
    from app.config import DEFAULT_OLLAMA_MODEL, get_settings

    get_settings.cache_clear()
    try:
        client = OllamaClient()
        assert client._default_model == DEFAULT_OLLAMA_MODEL  # noqa: SLF001
        assert str(client._client.base_url).startswith("http://localhost:11434/v1")  # noqa: SLF001
    finally:
        get_settings.cache_clear()


def test_base_url_without_v1_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """`OLLAMA_BASE_URL=host:port` trần (thiếu `/v1`) phải được adapter tự chuẩn hoá —
    đã cháy thật 2026-07-30: SDK gọi `/chat/completions` trần ⇒ Ollama 404 "page not
    found" (404 của Go router, không phải 404 model — khó lần đúng kiểu env-sai G1)."""
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")

    from agent.providers.ollama_client import OllamaClient
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        client = OllamaClient()
        assert str(client._client.base_url).rstrip("/").endswith("/v1")  # noqa: SLF001
    finally:
        get_settings.cache_clear()


def test_settings_have_no_ollama_api_key_field() -> None:
    """Server local không xác thực — field key trong Settings là gợi ý sai về một secret
    không tồn tại (và một chỗ để lỡ tay log nó)."""
    from app.config import Settings

    assert not any("ollama" in f and "key" in f for f in Settings.model_fields), (
        "xuất hiện field key cho Ollama — xem docstring ollama_client.py"
    )


def _fresh_factory_client(monkeypatch: pytest.MonkeyPatch, provider: str):
    from agent.llm_client import default_llm_client
    from app.config import get_settings

    monkeypatch.setenv("LLM_PROVIDER", provider)
    monkeypatch.setenv("TOGETHER_API_KEY", "sk-test-fake")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    try:
        return default_llm_client()
    finally:
        get_settings.cache_clear()


def test_factory_selects_ollama_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM_PROVIDER=ollama → chuỗi bọc giữ nguyên (PII ngoài cùng — I16), ruột là Ollama."""
    from agent.pii_client import PIIFilteringClient
    from agent.providers.ollama_client import OllamaClient

    client = _fresh_factory_client(monkeypatch, "ollama")
    assert isinstance(client, PIIFilteringClient)
    assert isinstance(client._inner, OllamaClient)  # noqa: SLF001


def test_factory_default_is_still_together(monkeypatch: pytest.MonkeyPatch) -> None:
    """Không set LLM_PROVIDER → hành vi CŨ giữ nguyên: Together. (Đây là chốt hồi quy —
    thêm provider không được đổi mặc định của môi trường đang chạy.)"""
    from agent.llm_client import default_llm_client
    from agent.pii_client import PIIFilteringClient
    from agent.providers.together_client import TogetherClient
    from app.config import get_settings

    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("TOGETHER_API_KEY", "sk-test-fake")
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    get_settings.cache_clear()
    try:
        client = default_llm_client()
        assert isinstance(client, PIIFilteringClient)
        assert isinstance(client._inner, TogetherClient)  # noqa: SLF001
    finally:
        get_settings.cache_clear()


def test_factory_rejects_unknown_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """Typo env không được âm thầm rơi về provider trả phí — raise ngay lúc dựng."""
    with pytest.raises(ValueError, match="không hỗ trợ"):
        _fresh_factory_client(monkeypatch, "olama")  # typo cố ý
