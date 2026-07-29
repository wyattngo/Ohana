"""Seller inbox REST scaffold (spec 01 §3 Sub-task E, step 19).

Every route derives `shop_id` from a verified `auth.identity.Identity` (never from body /
query / header / URL). The repo used inside each route is scoped to that same `shop_id`
at construction, so a seller for shop A cannot list, approve, or reject shop B's parked
drafts — even if they know a valid reply_id from B.

UI framework choice deferred (spec §12 [UNVERIFIED] `web/`). This scaffold ships only the
REST surface; whatever renders it (SPA, server-side, native app) plugs in later.

The `send-on-approve` path is NOT wired here — approve just flips the status. A separate
worker (Phase 3+) drains `approved` rows and calls the sender. Rationale: keep the review
API idempotent and cheap; make the actual outbound send an isolated retryable step.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from auth.identity import Identity
from db.repos import PendingReplyRepo


class PendingReplyOut(BaseModel):
    reply_id: str
    conversation_id: str
    customer_id: str
    draft_text: str
    intent: str
    confidence: float
    status: str


def build_router(
    session_factory: async_sessionmaker[AsyncSession],
    # Typed as the callable it actually is. It was `object`, which made every
    # `Depends(identity_dep)` an arg-type error and forced four `type: ignore` comments —
    # and those carried the WRONG code (`valid-type`), so they suppressed nothing while
    # themselves being flagged as unused. One accurate annotation removes all of it.
    # FastAPI dependency → Identity (wired by app.main). Sync HOẶC async: spec 11 S1 làm nó
    # async vì phải tra `shops`. FastAPI nhận cả hai — annotation phải nói đúng điều đó.
    identity_dep: Callable[..., Identity | Awaitable[Identity]],
) -> APIRouter:
    router = APIRouter(prefix="/inbox", tags=["inbox"])

    # An async generator, not a coroutine returning a session — the annotation has to say so.
    async def _session() -> AsyncIterator[AsyncSession]:  # yielded per-request
        async with session_factory() as s:
            yield s

    @router.get("", response_model=list[PendingReplyOut])
    async def list_pending(
        identity: Identity = Depends(identity_dep),
        session: AsyncSession = Depends(_session),
    ) -> list[PendingReplyOut]:
        repo = PendingReplyRepo(session, shop_scope=identity.shop_id)
        rows = await repo.list_pending()
        return [
            PendingReplyOut(
                reply_id=r.reply_id,
                conversation_id=r.conversation_id,
                customer_id=r.customer_id,
                draft_text=r.draft_text,
                intent=r.intent,
                confidence=r.confidence,
                status=r.status,
            )
            for r in rows
        ]

    @router.post("/{reply_id}/approve")
    async def approve(
        reply_id: str,
        identity: Identity = Depends(identity_dep),
        session: AsyncSession = Depends(_session),
    ) -> dict[str, str]:
        repo = PendingReplyRepo(session, shop_scope=identity.shop_id)
        updated = await repo.mark_decided(
            reply_id, new_status="approved", decided_by=identity.user_id
        )
        if updated == 0:
            # Same shape for cross-shop-attempt AND already-decided AND missing — never
            # tell the caller which of these it was (any leak here is a light S4 hint).
            raise HTTPException(status_code=404, detail="reply_not_found_or_already_decided")
        return {"status": "approved"}

    @router.post("/{reply_id}/reject")
    async def reject(
        reply_id: str,
        identity: Identity = Depends(identity_dep),
        session: AsyncSession = Depends(_session),
    ) -> dict[str, str]:
        repo = PendingReplyRepo(session, shop_scope=identity.shop_id)
        updated = await repo.mark_decided(
            reply_id, new_status="rejected", decided_by=identity.user_id
        )
        if updated == 0:
            raise HTTPException(status_code=404, detail="reply_not_found_or_already_decided")
        return {"status": "rejected"}

    return router
