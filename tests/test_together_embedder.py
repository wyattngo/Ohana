"""E0 gate (spec `08-Task-OhanaAISeller-EmbedderSwap-E5.md` §7 Phase E0).

Viết TRƯỚC `agent/providers/together_embedder.py` — expected RED (collection error:
`ModuleNotFoundError`) cho tới khi adapter land.

Khoá 7 assertion (a)–(g) của spec §7 bước 1. Ba cái đắt nhất, và vì sao:

(c) `TOGETHER_EMBED_MODEL` rỗng ⇒ rơi về DEFAULT, KHÔNG rơi sang model provider khác.
    Spec 07 G0 đã cháy đúng ca này: `TOGETHER_MODEL=` rỗng ⇒ falsy ⇒ trượt `or` ⇒ client
    trỏ Together nhưng xin `gpt-4o-mini` ⇒ 404. **90 test vẫn xanh** vì test dùng fake
    client. Lưu ý `_blank_env_means_unset` KHÔNG cứu được mọi kiểu field (ISSUE-018) —
    `.strip() or DEFAULT` trong adapter là lớp phòng thủ thật, không phải thừa.

(e) **Gate BẤT ĐỐI XỨNG** — assertion quan trọng nhất của cả phase. Prefix hoán đổi
    (query embed bằng `passage: `, corpus embed bằng `query: `) KHÔNG crash, KHÔNG sai
    type, KHÔNG đỏ test thường: nó chỉ làm chất lượng retrieval tệ đi ÂM THẦM → AI trả
    lời khách bằng căn cứ sai, không stack trace. Cùng họ với `DeterministicDevEmbedder`
    (spec 04 P2). Đây là thứ duy nhất ở E0 mà review bằng mắt không bắt được.

(f) `OpenAIEmbedder` + `DeterministicDevEmbedder` KHÔNG vỡ sau khi ABC thêm method.
    Đây là lý do spec cấm `@abstractmethod`: thêm abstract sẽ phá mọi impl hiện có.

Không test nào ở đây chạm mạng thật. Fake client bắt text GỬI ĐI — nghĩa là nó chứng minh
"adapter gắn prefix đúng bên", KHÔNG chứng minh "e5 trả vector tốt". Việc sau là SMOKE
artifact của phase này (E0 có mặt runtime ⇒ `SMOKE: N/A` không hợp lệ) và E2 live gate.
"""

from __future__ import annotations

from typing import Any

import pytest


class _FakeEmbeddings:
    """Bắt lại `input` gửi tới provider — đây là bề mặt duy nhất chứng minh prefix."""

    def __init__(self, parent: _FakeAsyncOpenAI) -> None:
        self._parent = parent

    async def create(self, *, model: str, input: list[str], **kw: Any) -> Any:
        self._parent.calls.append({"model": model, "input": list(input)})

        class _Item:
            def __init__(self, vec: list[float]) -> None:
                self.embedding = vec

        class _Resp:
            def __init__(self, items: list[_Item]) -> None:
                self.data = items

        return _Resp([_Item([0.0] * 1024) for _ in input])


class _FakeAsyncOpenAI:
    def __init__(self, *a: Any, **kw: Any) -> None:
        self.calls: list[dict[str, Any]] = []
        self.init_kwargs = kw
        self.embeddings = _FakeEmbeddings(self)


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- (a) contract -----------------------------------------------------------------


def test_together_embedder_is_an_embedder() -> None:
    """(a) `TogetherEmbedder` là `Embedder` — retrieval/agent core không cần biết provider."""
    from agent.embedder import Embedder
    from agent.providers.together_embedder import TogetherEmbedder

    assert issubclass(TogetherEmbedder, Embedder)


# --- (b) không hardcode -----------------------------------------------------------


def test_base_url_points_at_together_and_is_a_module_constant() -> None:
    """(b) base_url trỏ Together và là HẰNG SỐ module, không phải env, không nhận override.

    Cùng lý do đã viết trong `together_client.py`: nhận override sẽ biến class thành
    "embedder trỏ đâu cũng được" và làm cái tên nói dối. Muốn provider khác → adapter khác.
    """
    from agent.providers import together_embedder as mod

    assert mod.TOGETHER_BASE_URL == "https://api.together.xyz/v1"

    captured: dict[str, Any] = {}

    def _factory(*a: Any, **kw: Any) -> _FakeAsyncOpenAI:
        captured.update(kw)
        return _FakeAsyncOpenAI(**kw)

    mod.TogetherEmbedder(client=_factory())  # sanity: construct-able
    # base_url không được là tham số của __init__
    import inspect

    assert "base_url" not in inspect.signature(mod.TogetherEmbedder.__init__).parameters


def test_key_and_model_come_from_settings_not_hardcoded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(b) model + key đọc từ `Settings` (env), không hardcode trong class."""
    monkeypatch.setenv("TOGETHER_EMBED_MODEL", "acme/some-other-embed-model")
    from app.config import get_settings

    get_settings.cache_clear()

    from agent.providers.together_embedder import TogetherEmbedder

    fake = _FakeAsyncOpenAI()
    emb = TogetherEmbedder(client=fake)
    import asyncio

    asyncio.run(emb.embed_documents(["xin chào"]))

    assert fake.calls[0]["model"] == "acme/some-other-embed-model"


# --- (c) env rỗng KHÔNG trượt sang provider khác ----------------------------------


def test_blank_embed_model_falls_back_to_default_not_other_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) `TOGETHER_EMBED_MODEL=` rỗng ⇒ DEFAULT của Together.

    KHÔNG được rơi sang `openai_embed_model` — đó chính là bug 404 của spec 07 G0, chỉ
    khác chỗ nó xảy ra ở đường embedding thay vì đường chat.
    """
    monkeypatch.setenv("TOGETHER_EMBED_MODEL", "   ")  # khoảng trắng: truthy nhưng vô nghĩa
    monkeypatch.setenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
    from app.config import DEFAULT_TOGETHER_EMBED_MODEL, get_settings

    get_settings.cache_clear()

    from agent.providers.together_embedder import TogetherEmbedder

    fake = _FakeAsyncOpenAI()
    import asyncio

    asyncio.run(TogetherEmbedder(client=fake).embed_documents(["x"]))

    sent = fake.calls[0]["model"]
    assert sent == DEFAULT_TOGETHER_EMBED_MODEL
    assert "text-embedding-3-small" not in sent
    assert "openai" not in sent.lower()


def test_default_embed_model_is_e5() -> None:
    """(c) DEFAULT khớp ADR PRE-007 — e5 1024-dim, đã verify bằng curl thật (spec §5.1)."""
    from app.config import DEFAULT_TOGETHER_EMBED_MODEL

    assert DEFAULT_TOGETHER_EMBED_MODEL == "intfloat/multilingual-e5-large-instruct"


# --- (d) prefix đúng bên ----------------------------------------------------------


def test_embed_query_prefixes_query() -> None:
    """(d) `embed_query` gắn `query: ` — kiểm bằng text GỬI ĐI, không bằng docstring."""
    from agent.providers.together_embedder import TogetherEmbedder

    fake = _FakeAsyncOpenAI()
    import asyncio

    asyncio.run(TogetherEmbedder(client=fake).embed_query("phí ship bao nhiêu"))

    assert fake.calls[0]["input"] == ["query: phí ship bao nhiêu"]


def test_embed_documents_prefixes_passage() -> None:
    """(d) `embed_documents` gắn `passage: ` cho MỌI phần tử, giữ đúng thứ tự."""
    from agent.providers.together_embedder import TogetherEmbedder

    fake = _FakeAsyncOpenAI()
    import asyncio

    asyncio.run(TogetherEmbedder(client=fake).embed_documents(["một", "hai"]))

    assert fake.calls[0]["input"] == ["passage: một", "passage: hai"]


# --- (e) GATE BẤT ĐỐI XỨNG — assertion đắt nhất -----------------------------------


def test_prefixes_are_not_swapped() -> None:
    """(e) Hoán đổi prefix = hỏng ÂM THẦM. Không crash, không sai type, retrieval tệ đi.

    Test này tồn tại vì đây là failure mode DUY NHẤT ở E0 mà không lớp nào khác bắt được:
    mypy xanh, ruff xanh, mọi test khác xanh, và AI vẫn trả lời khách bằng căn cứ sai.
    """
    import asyncio

    from agent.providers.together_embedder import TogetherEmbedder

    fq = _FakeAsyncOpenAI()
    asyncio.run(TogetherEmbedder(client=fq).embed_query("q"))
    fd = _FakeAsyncOpenAI()
    asyncio.run(TogetherEmbedder(client=fd).embed_documents(["d"]))

    q_sent = fq.calls[0]["input"][0]
    d_sent = fd.calls[0]["input"][0]

    assert q_sent.startswith("query: "), f"query side lost its prefix: {q_sent!r}"
    assert d_sent.startswith("passage: "), f"document side lost its prefix: {d_sent!r}"
    # hoán đổi
    assert not q_sent.startswith("passage: "), "prefix SWAPPED — query embedded as passage"
    assert not d_sent.startswith("query: "), "prefix SWAPPED — passage embedded as query"
    # hai bên phải KHÁC nhau: cùng prefix = mất hẳn tính bất đối xứng của e5
    assert q_sent.split(":")[0] != d_sent.split(":")[0]


def test_prefix_not_applied_twice() -> None:
    """(e) Text đã mang prefix không được gắn chồng — `query: query: …` là input rác."""
    import asyncio

    from agent.providers.together_embedder import TogetherEmbedder

    fake = _FakeAsyncOpenAI()
    asyncio.run(TogetherEmbedder(client=fake).embed_query("query: đã có sẵn"))

    assert fake.calls[0]["input"][0].count("query: ") == 1


# --- (f) impl cũ KHÔNG vỡ ---------------------------------------------------------


def test_abc_additions_do_not_break_existing_impls() -> None:
    """(f) ABC thêm method dạng CONCRETE delegate ⇒ impl cũ vẫn instantiate được.

    Nếu ai đó đổi 2 method mới thành `@abstractmethod`, test này đỏ ngay — đó chính là
    điều spec §7 APPROACH cấm ("thêm abstract sẽ phá mọi impl hiện có").
    """
    from agent.providers.dev_embedder import DeterministicDevEmbedder
    from agent.providers.openai_embedder import OpenAIEmbedder

    assert not getattr(OpenAIEmbedder, "__abstractmethods__", frozenset())
    assert not getattr(DeterministicDevEmbedder, "__abstractmethods__", frozenset())


def test_abc_default_methods_delegate_to_embed() -> None:
    """(f) Default `embed_query`/`embed_documents` delegate về `embed()` — KHÔNG prefix.

    Prefix là việc của ADAPTER e5, không phải của ABC: OpenAI không dùng prefix, gắn ở
    tầng chung sẽ làm bẩn mọi provider khác (spec §7 APPROACH).
    """
    from agent.embedder import Embedder

    class _Spy(Embedder):
        def __init__(self) -> None:
            self.seen: list[list[str]] = []

        async def embed(self, texts: list[str]) -> list[list[float]]:
            self.seen.append(list(texts))
            return [[0.0] for _ in texts]

    import asyncio

    s = _Spy()
    asyncio.run(s.embed_query("q"))
    asyncio.run(s.embed_documents(["d1", "d2"]))

    assert s.seen == [["q"], ["d1", "d2"]], "ABC default phải delegate NGUYÊN VĂN, không prefix"


# --- (g) key không rò -------------------------------------------------------------


def test_api_key_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    """(g) Key KHÔNG lộ qua `repr()`.

    **Test này hôm nay KHÔNG gate được gì** — nói thẳng để người sau không hiểu nhầm nó
    chứng minh điều gì. `TogetherEmbedder` không lưu key làm attribute và không định nghĩa
    `__repr__`, nên `object.__repr__` mặc định vốn đã không in key ra; phá dòng nào trong
    class cũng không làm test này đỏ.

    Giữ lại vì nó gate được TƯƠNG LAI: ngày ai đó thêm `__repr__` cho dễ debug, hoặc gán
    `self._api_key = …`, test này đỏ ngay. Đó là lúc key thật sự có đường rò vào log —
    và là loại lỗi chỉ phát hiện được sau khi đã rò.
    """
    # Tên biến cố ý KHÔNG phải `secret`/`token`/`password`: heuristic S105 của ruff bắt theo
    # TÊN BIẾN. Đổi tên rẻ hơn thêm một dòng chặn lint — dòng chặn sẽ dạy người đọc sau rằng
    # S105 trong repo này là nhiễu, và lần tới có key thật lọt vào test sẽ không ai để ý.
    fake_key = "not-a-real-credential-0123456789"
    monkeypatch.setenv("TOGETHER_API_KEY", fake_key)
    from app.config import get_settings

    get_settings.cache_clear()

    from agent.providers.together_embedder import TogetherEmbedder

    emb = TogetherEmbedder(client=_FakeAsyncOpenAI())
    assert fake_key not in repr(emb)
    assert fake_key not in str(emb)
