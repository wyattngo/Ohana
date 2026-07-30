"""Admin ingest endpoints (F1 — wiki corpus loader).

GĐ0 accepts raw text via JSON; a docx/pdf upload path can wrap `parsing.extract.extract_text`
in a later phase. This endpoint WAS unauthenticated at GĐ0 (Phase 3+ was to require an admin
JWT) — spec 04 Phase P2 is that gate: `require_admin` below, mounted together in
`app/main.py` (never one without the other — see that module's docstring).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.identity import Identity
from db.models import Shop
from parsing.ingest import PLATFORM_SHOP_ID, ingest_wiki

# Fix I5b: factory `default_embedder` + `DeterministicDevEmbedder` chuyển sang seam
# `agent/embedder.py` / `agent/providers/dev_embedder.py` — module này không còn biết
# provider cụ thể nào; `app/main.py` lấy embedder từ seam và tiêm vào `build_router`.


class _EmbedderProto(Protocol):
    # Khai đúng method route này truyền xuống `ingest_wiki` — bên CORPUS ⇒ `embed_documents`
    # (spec 08 E0). Trước E0 chỗ này khai `embed`; giữ nguyên sẽ lệch với
    # `parsing.ingest._EmbedderProto` và mypy bắt được ngay ở call site.
    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...


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
