"""P1 gate (spec `05-Task-OhanaAISeller-ConfigEmbedder-F1.md` §7 Phase P1).

Proves WIRING only: `default_embedder()` selects the right class for each
(`openai_api_key` present?) x (`OHANA_ENV == "dev"`?) combination, and the
`OpenAIEmbedder` adapter calls `AsyncOpenAI.embeddings.create(...)` with the
right shape and returns `list[list[float]]` at OpenAI's dim (1536).

⚠️ Cập nhật spec 08 E1: 1536 ở đây là dim của MODEL OpenAI, KHÔNG còn là dim của
cột DB (cột giờ 1024 — Together e5). Hai con số từng trùng nhau nên từng được
nói như một; giờ tách. `default_embedder()` ưu tiên `TogetherEmbedder` khi có
`TOGETHER_API_KEY` — xem `test_default_embedder_prefers_together`.

This is NOT proof that F1 retrieval works with real embeddings — that semantic
claim is ISSUE-016's live acceptance test (`tests/test_wiki_rag_live.py`,
`@pytest.mark.live`), which needs a real `OPENAI_API_KEY` + network and is
excluded from this gate. Nothing here calls real OpenAI: (a)/(b)/(c) only
touch `default_embedder()`'s branch logic, and (d) injects a fake
`AsyncOpenAI`-shaped client into `OpenAIEmbedder(client=...)` so `.embed()`
never leaves the process. A deterministic PASS here means "the factory picks
the right adapter and the adapter's call-shape/dim contract holds against a
mock" — never "the AI answers customers correctly."

`get_settings()` is `@lru_cache`d — every test that mutates `OPENAI_API_KEY`
via monkeypatch MUST call `get_settings.cache_clear()` after the env change
(same tautology trap `test_config.py` documents: skip the clear and you
assert on a stale cached Settings instance instead of the branch you just
set up).

**Deviation from spec §3 B's literal factory pseudocode (flagged at ANCHOR):** spec proposes
`default_embedder()` itself raising `RuntimeError` for "no key + not dev". This file instead
locks in that `default_embedder()` NEVER raises — `app/main.py` calls it at MODULE IMPORT time,
not per-request, so an eager raise there crashes the whole app's import (every route, not just
wiki ingest) on a deploy missing `OHANA_ENV=dev` without a key. Confirmed empirically: with the
eager-raise version, `pytest tests/ -m 'not live'` failed at COLLECTION, before any test ran.
The refusal instead happens one layer down, at the returned embedder's `.embed()` — see
`test_no_key_non_dev_refuses_at_embed_time_not_at_wiring_time` for the full rationale, matching
`api/mock_auth.py::_is_dev_env`'s established "check per-request, not at import/build time"
convention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    """Every test starts and ends with a clean `get_settings()` cache — see module docstring."""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_key_present_selects_openai_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """(a) `openai_api_key` set → `default_embedder()` returns a real `OpenAIEmbedder`, not the
    dev placeholder. A fake key is fine here — `OpenAIEmbedder.__init__` never touches the
    network; only `.embed()` would, and this test doesn't call it."""
    from agent.embedder import default_embedder
    from agent.providers.openai_embedder import OpenAIEmbedder
    from app.config import get_settings

    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)  # spec 08 E1: Together ưu tiên cao hơn
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-not-a-real-key")
    get_settings.cache_clear()

    embedder = default_embedder()

    assert isinstance(embedder, OpenAIEmbedder)


def test_key_present_and_dev_still_selects_openai_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """Key present AND `OHANA_ENV=dev` → still the real embedder. Dev-with-a-key means "test the
    real path locally," not "fall back to the hash placeholder." Key presence wins regardless of
    env."""
    from agent.embedder import default_embedder
    from agent.providers.openai_embedder import OpenAIEmbedder
    from app.config import get_settings

    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)  # spec 08 E1: Together ưu tiên cao hơn
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-not-a-real-key")
    monkeypatch.setenv("OHANA_ENV", "dev")
    get_settings.cache_clear()

    embedder = default_embedder()

    assert isinstance(embedder, OpenAIEmbedder)


def test_no_key_dev_selects_deterministic_dev_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) no key + `OHANA_ENV=dev` → the hash-vector dev placeholder, same as today's behavior
    for local dev without a configured key."""
    from agent.embedder import default_embedder
    from agent.providers.dev_embedder import DeterministicDevEmbedder
    from app.config import get_settings

    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)  # spec 08 E1: Together ưu tiên cao hơn
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OHANA_ENV", "dev")
    get_settings.cache_clear()

    embedder = default_embedder()

    assert isinstance(embedder, DeterministicDevEmbedder)


@pytest.mark.asyncio
async def test_no_key_non_dev_refuses_at_embed_time_not_at_wiring_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(c) no key + `OHANA_ENV` unset (i.e. not dev) → the safety property spec asks for (never
    silently write meaningless vectors outside dev) holds, but NOT via `default_embedder()`
    itself raising.

    Deviation from spec §3 B's literal pseudocode, disclosed at ANCHOR: `default_embedder()` is
    called by `app/main.py` at MODULE IMPORT time (not per-request) — raising directly inside it
    would crash the whole app's import (every route, not just wiki ingest) on any deploy missing
    `OHANA_ENV=dev` without a key yet. Confirmed empirically: with an eager raise,
    `pytest tests/ -m 'not live'` fails at COLLECTION (`test_smoke.py` can't even import
    `app.main`), not just at this route. So `default_embedder()` always returns successfully
    here (`DeterministicDevEmbedder`, same as the no-key+dev branch) — the refusal happens one
    layer down, at `.embed()`, which already had this exact guard before this phase (see
    `api/admin.py::DeterministicDevEmbedder.embed`). This matches the established codebase
    convention of checking env gates per-request, not baking them in at import/build time
    (`api/mock_auth.py::_is_dev_env`)."""
    from agent.embedder import default_embedder
    from agent.providers.dev_embedder import DeterministicDevEmbedder
    from app.config import get_settings

    monkeypatch.delenv("TOGETHER_API_KEY", raising=False)  # spec 08 E1: Together ưu tiên cao hơn
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OHANA_ENV", raising=False)
    get_settings.cache_clear()

    embedder = default_embedder()  # must NOT raise — see docstring above

    assert isinstance(embedder, DeterministicDevEmbedder)
    with pytest.raises(RuntimeError, match="embedder"):
        await embedder.embed(["chính sách đổi trả"])


@dataclass
class _FakeEmbeddingItem:
    embedding: list[float]


@dataclass
class _FakeEmbeddingResponse:
    data: list[_FakeEmbeddingItem]


class _FakeEmbeddingsResource:
    def __init__(self, dim: int) -> None:
        self._dim = dim
        self.calls: list[dict[str, Any]] = []

    async def create(self, *, model: str, input: list[str]) -> _FakeEmbeddingResponse:  # noqa: A002
        self.calls.append({"model": model, "input": input})
        return _FakeEmbeddingResponse(
            data=[_FakeEmbeddingItem(embedding=[0.0] * self._dim) for _ in input]
        )


class _FakeAsyncOpenAI:
    """Stands in for `openai.AsyncOpenAI` — only the `.embeddings.create(...)` surface
    `OpenAIEmbedder.embed()` touches. No network, no real client construction."""

    def __init__(self, dim: int = 1536) -> None:
        self.embeddings = _FakeEmbeddingsResource(dim)


@pytest.mark.asyncio
async def test_openai_embedder_embed_returns_1536_dim_vectors() -> None:
    """(d) `OpenAIEmbedder.embed()` call-shape + dim contract, proven against a fake client
    injected via `client=` (never touches real OpenAI). Locks: một vector mỗi input, độ dài
    1536 = dim của `text-embedding-3-small`.

    ⚠️ 1536 ở đây là dim của MODEL, KHÔNG phải dim của cột DB (spec 08 E1 đổi cột sang 1024).
    Trước E1 hai số này trùng nhau nên docstring cũ nói chúng như một thứ — không còn đúng."""
    from agent.providers.openai_embedder import OpenAIEmbedder

    fake_client = _FakeAsyncOpenAI(dim=1536)
    embedder = OpenAIEmbedder(client=fake_client, model="text-embedding-3-small")  # type: ignore[arg-type]

    vectors = await embedder.embed(["a", "b"])

    assert len(vectors) == 2
    assert all(len(v) == 1536 for v in vectors)
    assert fake_client.embeddings.calls == [
        {"model": "text-embedding-3-small", "input": ["a", "b"]}
    ]


def test_default_embedder_prefers_together(monkeypatch: pytest.MonkeyPatch) -> None:
    """spec 08 E1 — `TOGETHER_API_KEY` set ⇒ `TogetherEmbedder`, KỂ CẢ khi có `OPENAI_API_KEY`.

    Thứ tự ưu tiên không phải tuỳ thích: kể từ E1 cột DB là `Vector(1024)`, và `TogetherEmbedder`
    là adapter DUY NHẤT sinh vector khớp chiều đó. Chọn nhầm OpenAI ở đây nghĩa là mọi lần ingest
    đều bị Postgres từ chối — ồn ào, nhưng vẫn là hỏng.
    """
    from agent.embedder import default_embedder
    from agent.providers.together_embedder import TogetherEmbedder
    from app.config import get_settings

    monkeypatch.setenv("TOGETHER_API_KEY", "tk-fake-not-real")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake-not-a-real-key")  # cả hai cùng có
    get_settings.cache_clear()

    assert isinstance(default_embedder(), TogetherEmbedder)


def test_dev_embedder_dim_follows_config_not_a_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """spec 08 E1 — vector của `DeterministicDevEmbedder` phải dài đúng `EMBED_DIM`.

    Trước E1 `api/admin.py` khai `_DEV_EMBED_DIM = 1536` kèm comment "must match db.models".
    Comment là lời nhắc cho người, và nó KHÔNG giữ được lời hứa: đổi cột sang 1024 thì hằng số
    kia vẫn 1536 và dev-embedder ghi vector sai chiều. Test này khoá bằng máy thay vì bằng comment.
    """
    import asyncio

    from agent.providers.dev_embedder import DeterministicDevEmbedder
    from app.config import EMBED_DIM

    # `OHANA_ENV=dev` phải set TƯỜNG MINH: `DeterministicDevEmbedder.embed()` cố ý raise ngoài
    # dev (spec 04 P2 — không bao giờ ghi vector giả vào môi trường thật). Bản đầu của test này
    # không set, nên nó xanh trên shell CÓ sẵn biến đó và đỏ trong suite — đúng loại phụ thuộc
    # env ngầm mà chính test này đang đi sửa cho các test khác trong file.
    monkeypatch.setenv("OHANA_ENV", "dev")

    vecs = asyncio.run(DeterministicDevEmbedder().embed(["xin chào"]))

    assert len(vecs[0]) == EMBED_DIM
