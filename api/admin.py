"""Admin ingest endpoints (F1 — wiki corpus loader).

GĐ0 accepts raw text via JSON; a docx/pdf upload path can wrap `parsing.extract.extract_text`
in a later phase. This endpoint WAS unauthenticated at GĐ0 (Phase 3+ was to require an admin
JWT) — spec 04 Phase P2 is that gate: `require_admin` below, mounted together in
`app/main.py` (never one without the other — see that module's docstring).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.embedder import Embedder
from agent.providers.openai_embedder import OpenAIEmbedder
from agent.providers.together_embedder import TogetherEmbedder
from app.config import EMBED_DIM, get_settings
from auth.identity import Identity
from db.models import Shop
from parsing.ingest import PLATFORM_SHOP_ID, ingest_wiki

# Alias tới nguồn sự thật duy nhất, KHÔNG phải bản sao. Trước spec 08 E1 đây là `1536` viết
# cứng kèm comment "must match db.models" — comment đó là lời nhắc cho con người, và nó đã
# KHÔNG giữ được lời hứa: đổi cột sang 1024 thì hằng số này vẫn 1536, dev-embedder sinh vector
# sai chiều, và triệu chứng chỉ hiện ra ở `test_admin_ui` khi ingest chạm DB thật. Import thì
# máy giữ hộ.
_DEV_EMBED_DIM = EMBED_DIM


class _EmbedderProto(Protocol):
    # Khai đúng method route này truyền xuống `ingest_wiki` — bên CORPUS ⇒ `embed_documents`
    # (spec 08 E0). Trước E0 chỗ này khai `embed`; giữ nguyên sẽ lệch với
    # `parsing.ingest._EmbedderProto` và mypy bắt được ngay ở call site.
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


class _DeterministicDevEmbedder(Embedder):
    """GD0.5 placeholder embedder — only selected by `default_embedder()` below when there is no
    `OPENAI_API_KEY` AND `OHANA_ENV=dev`. Since spec 05 Phase P1, `app/config.py` exists and
    `agent.providers.openai_embedder.OpenAIEmbedder` is the default whenever a key IS configured
    (any env) — this class is now the no-key dev fallback, not the unconditional default.

    Deterministic hash-based vectors, same shape `tests/test_wiki_rag.py`'s `FakeEmbedder`
    already uses to gate the ingest/search round-trip without a network call. This exercises
    the real storage/HTTP contract (chunk count, DB rows) for real; it is NOT representative
    of real semantic search quality — `tests/test_wiki_rag_live.py` (`@pytest.mark.live`) is the
    real-embedding acceptance check (ISSUE-016), run manually with a real key.

    Refuses to run outside dev (see `embed`) — defense in depth: even if selection logic above
    ever mis-picks this class outside dev, `embed()` itself still refuses. A docstring saying
    "placeholder" does not make a placeholder safe — the same reasoning that gated
    `auth.identity.get_jwt_secret`'s dev fallback on `OHANA_ENV` applies here.
    """

    async def embed(self, texts: list[str]) -> list[list[float]]:
        # Fail LOUD outside dev rather than returning hash vectors that look like embeddings.
        # The failure mode this closes is silent-wrong, not crash: ingest would answer
        # {"success": true, "chunks": N} while writing semantically meaningless vectors, so
        # `search_wiki` would then feed near-random chunks to the drafter and the AI would
        # answer a customer confidently from the wrong source. Nobody sees a stack trace; they
        # see a plausible wrong reply. Raising here (not in `default_embedder()` — see that
        # function's docstring) keeps `app/main.py` importable (P0/P1 screens stay up) and
        # confines the blast radius to this one route.
        if os.environ.get("OHANA_ENV") != "dev":
            raise RuntimeError(
                "Wiki ingest is unavailable: no real embedder is configured "
                "(no OPENAI_API_KEY outside dev). Refusing to write placeholder vectors "
                "outside dev — they would silently corrupt wiki-RAG answers."
            )
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * _DEV_EMBED_DIM
            for tok in t.lower().split():
                vec[hash(tok) % _DEV_EMBED_DIM] = 1.0
            out.append(vec)
        return out


def default_embedder() -> _EmbedderProto:
    """Factory `app/main.py` calls at wiring time (spec 05 §3 Sub-task B). Env-selecting:

    - `TOGETHER_API_KEY` set -> `TogetherEmbedder` (e5, 1024-dim). ƯU TIÊN CAO NHẤT kể từ
      spec 08 E1: cột DB giờ là `Vector(1024)`, tức đây là adapter DUY NHẤT sinh ra vector
      khớp schema hiện tại.
    - `OPENAI_API_KEY` set, không có Together key -> `OpenAIEmbedder` (1536).
      ⚠️ Nhánh này sinh vector SAI CHIỀU so với cột DB ⇒ ingest sẽ bị Postgres TỪ CHỐI. Giữ
      lại có chủ ý (spec 08 §3 "Out of scope": `OpenAIEmbedder` là adapter thay thế, và là
      bằng chứng lớp abstraction hoạt động thật). Hỏng ở đây ỒN ÀO — Postgres raise, không
      ghi được vector vô nghĩa nào. Đó là lý do để nhánh này tồn tại mà không nguy hiểm:
      thà từ chối rõ ràng còn hơn im lặng chọn provider khác sau lưng người vận hành.
    - Không key nào -> `_DeterministicDevEmbedder` (đúng chiều, nhưng raise ngoài dev).

    **Deviation from spec §3 B's proposed pseudocode, flagged at ANCHOR:** the spec's factory
    sketch raises `RuntimeError` directly in this function for the "no key + not dev" branch.
    This function deliberately does NOT do that — `app/main.py` calls `default_embedder()` at
    MODULE IMPORT time (`app.include_router(build_admin_router(default_embedder(), ...))` runs
    at import, before even `/health` is defined), not per-request. Raising here would crash the
    entire app's import — every route (health check, inbox, static SPA), not just wiki ingest —
    on any deploy that's missing `OHANA_ENV=dev` without a key configured yet. Confirmed via
    `pytest tests/ -m 'not live'`: with the eager raise, collection itself fails
    (`ERROR collecting tests/test_smoke.py`) before a single test runs.

    That also contradicts this codebase's established convention: env gates are checked
    PER-REQUEST, never baked in at import/build time (see `api/mock_auth.py::_is_dev_env`'s
    docstring, same reasoning). The safety property spec actually wants — never silently write
    placeholder vectors outside dev — is fully preserved: `_DeterministicDevEmbedder.embed()`
    already raises on first real use outside dev (unchanged since before this phase; see its
    docstring). This factory just doesn't ALSO raise a step earlier, at construction, where the
    only caller happens to run at import time.
    """
    settings = get_settings()
    if settings.together_api_key:
        return TogetherEmbedder()
    if settings.openai_api_key:
        return OpenAIEmbedder()
    return _DeterministicDevEmbedder()


class WikiIngestRequest(BaseModel):
    text: str = Field(..., min_length=1)
    source_ref: str = Field(..., min_length=1)
    shop_id: str = Field(default=PLATFORM_SHOP_ID)


class WikiIngestResponse(BaseModel):
    success: bool
    chunks: int


class ShopOnboardRequest(BaseModel):
    """Body của onboard. `extra="ignore"` CÓ CHỦ ĐÍCH — cùng bất biến `ChatIn` của spec 07.

    Client gửi kèm `id`, `shop_id`, `status`… thì các field đó bị BỎ QUA hoàn toàn: không có
    đường nào để chúng ảnh hưởng tới row được tạo. Cho client chọn `shop_id` nghĩa là cho họ
    chọn danh tính tenant — tức mở lại đúng cái lỗ mà phase S1 sinh ra để đóng.
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(..., min_length=1, max_length=200)


class ShopOnboardResponse(BaseModel):
    shop_id: str
    name: str


def build_router(
    embedder: _EmbedderProto,
    session_factory: async_sessionmaker[AsyncSession],
    # See api/inbox.py for why this is a typed callable and not `object`.
    # 403s non-admin. Sync hoặc async — spec 11 S1 truyền `build_admin_dep(...)` (async, tra
    # `shops` TRƯỚC khi kiểm role, nên shop bị treo bị chặn kể cả khi role=admin).
    admin_dep: Callable[..., Identity | Awaitable[Identity]],
) -> APIRouter:
    router = APIRouter(prefix="/admin", tags=["admin"])

    @router.post("/wiki/ingest", response_model=WikiIngestResponse)
    async def wiki_ingest(
        req: WikiIngestRequest,
        _admin: Identity = Depends(admin_dep),
    ) -> WikiIngestResponse:
        n = await ingest_wiki(
            session_factory,
            embedder,
            text=req.text,
            source_ref=req.source_ref,
            shop_id=req.shop_id,
        )
        return WikiIngestResponse(success=True, chunks=n)

    @router.post("/shops", response_model=ShopOnboardResponse, status_code=201)
    async def onboard_shop(
        req: ShopOnboardRequest,
        _admin: Identity = Depends(admin_dep),
    ) -> ShopOnboardResponse:
        """Tạo một shop THẬT. Admin-only — đây là đường SINH RA tenant.

        `shop_id` do SERVER sinh (`uuid4`), không bao giờ lấy từ body. Nếu client chọn được
        id thì họ chọn được danh tính tenant, và mọi thứ S1 làm phía verify trở nên vô nghĩa.

        ⚠️ `require_admin` là gate DUY NHẤT ở đây (§4 RED FLAG, đã ghi nhận). Admin token rò
        ⇒ kẻ tấn công tạo shop tuỳ ý. Chấp nhận được ở GĐ0 khi admin = Wyatt/Sơn; khi có
        nhiều admin thật thì cần audit log riêng cho đường này.
        """
        shop_id = f"shop_{uuid.uuid4().hex[:16]}"
        async with session_factory() as session:
            session.add(Shop(id=shop_id, name=req.name, status="active"))
            await session.commit()
        return ShopOnboardResponse(shop_id=shop_id, name=req.name)

    return router
