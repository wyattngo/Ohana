"""Zalo OAuth v4 token refresh (spec 17 P2, `GD0-ZALO`).

Refresh_token SINGLE-USE — mỗi call `oauth.zaloapp.com/v4/oa/access_token` với
`grant_type=refresh_token` sinh cặp (access, refresh) MỚI, cặp cũ chết ngay. Hai process
refresh cùng shop mà không lock = mất luôn cả cặp (process thua refresh trên token đã chết
→ 401 → phải re-authorize manual qua OA admin).

Chống bằng `SELECT ... FOR UPDATE` (P0 repo) + double-check locking: sau khi lấy lock,
kiểm `access_expires_at > now() + margin` — nếu token đã fresh (process khác vừa refresh
xong) ⇒ return, KHÔNG đốt refresh_token lần nữa.

`app_id`/`app_secret` per-App (từ `.env` ZALO_APP_ID/ZALO_APP_SECRET_KEY) — KHÁC
`oa_secret_key` per-OA (verify webhook). Header `secret_key: <app_secret>` (docs Zalo v4).

⚠️ **KNOWN RISK — commit-fail window mất token (spec 17 P2 review HIGH, Wyatt ACCEPT option A
2026-07-24).** Đây là dual-write cố hữu: POST oauth CONSUME old_refresh_token ở Zalo-side
(single-use, chết vĩnh viễn) TRƯỚC khi `session.commit()` ghi cặp mới xuống DB. Nếu commit
fail (DB timeout/pool exhaust) giữa hai bước → rollback undo cặp mới → DB giữ cặp CŨ đã chết
→ refresh lần sau 401 → phải re-auth tay qua OA admin (~30 phút outage).

Vì sao ACCEPT chưa fix ở GĐ0: (1) P4 BLOCKED — sender chưa chạy production, (2) chưa có Zalo
creds thật (PRE-004), (3) window chỉ transient-DB-failure (hiếm). Fix thật = outbox pattern
(persist-intent-trước, reconcile-sau) hoặc commit-retry + loud alert — hậu-GĐ0 khi P4 unblock.
KHI P4 UNBLOCK + trước khi enabled=True production: PHẢI xử window này (đừng ship real send
mà bỏ qua). Ghi lại ở đây để không ai quên lúc mount thật.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from db.models import ZaloOAToken
from db.repos import ZaloOATokenRepo

logger = logging.getLogger(__name__)

_OAUTH_URL = "https://oauth.zaloapp.com/v4/oa/access_token"

# Margin trước hạn để double-check coi token là "còn fresh". 60s đủ để 1 refresh + write
# hoàn tất trước khi caller kịp dùng; đủ hẹp để không giữ token gần-hết-hạn.
_FRESH_MARGIN = timedelta(seconds=60)


class ZaloRefreshError(Exception):
    """Refresh thất bại — Zalo từ chối refresh_token, response lỗi/thiếu field, hoặc shop
    chưa có token row. Caller (sender retry-on--32) để exception bubble, KHÔNG ghi rác DB."""


async def refresh_shop_tokens(
    shop_id: str,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    client: httpx.AsyncClient,
    app_id: str,
    app_secret: str,
    force: bool = False,
) -> None:
    """Refresh access+refresh token của 1 shop, race-safe qua FOR UPDATE + double-check.

    Toàn bộ lock → double-check → POST → write nằm trong MỘT transaction để FOR UPDATE giữ
    hiệu lực từ lúc đọc tới lúc ghi. Session mở/commit trong hàm này.

    `force=False` (cron gọi định kỳ): double-check `access_expires_at` — token đã fresh thì
    skip, không đốt refresh_token thừa. `force=True` (sender gọi sau khi Zalo trả `-32`):
    BỎ QUA double-check expiry vì **Zalo là nguồn sự thật về token liveness**, không phải
    `access_expires_at` trong DB (token có thể bị revoke sớm hoặc clock skew — DB nói fresh
    mà Zalo nói chết). Vẫn giữ FOR UPDATE nên 2 process cùng `-32` không mất token: process
    thứ hai đọc `refresh_token` mới nhất (process đầu vừa ghi) rồi refresh tiếp — tốn thêm
    1 lần refresh, KHÔNG mất khả năng refresh.

    Raises:
      ZaloRefreshError: shop chưa có token / Zalo trả non-200 / response thiếu field.
    """
    async with session_factory() as session:
        # BEGIN transaction + lock row (FOR UPDATE). Process khác refresh cùng shop sẽ chờ.
        # Dùng `.filter_by(shop_id=...)` (kwargs) thay WHERE-expression để guardrail
        # R2_SECRET_EQ không bắt nhầm biểu thức SQL là so sánh secret.
        lock_stmt = select(ZaloOAToken).filter_by(shop_id=shop_id).with_for_update()
        row = (await session.execute(lock_stmt)).scalar_one_or_none()
        if row is None:
            raise ZaloRefreshError(f"no zalo token row for shop {shop_id!r}")

        # Double-check: token đã fresh (process khác vừa refresh) ⇒ dùng luôn, không đốt lại.
        # `force` bỏ qua bước này (Zalo đã báo -32 = token chết, DB expiry không đáng tin).
        now = datetime.now(UTC)
        if not force and row.access_expires_at > now + _FRESH_MARGIN:
            logger.debug("zalo_refresh skip shop=%s already_fresh", shop_id)
            return

        old_refresh = row.refresh_token
        oa_id = row.oa_id
        oa_secret = row.oa_secret_key

        # POST oauth. Body form-urlencoded per docs Zalo v4.
        resp = await client.post(
            _OAUTH_URL,
            headers={"secret_key": app_secret},
            data={
                "app_id": app_id,
                "grant_type": "refresh_token",
                "refresh_token": old_refresh,
            },
        )
        if resp.status_code != 200:
            raise ZaloRefreshError(f"zalo oauth non-200 for shop {shop_id!r}: {resp.status_code}")

        try:
            data = resp.json()
            new_access = data["access_token"]
            new_refresh = data["refresh_token"]
            expires_in = int(data["expires_in"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ZaloRefreshError(f"zalo oauth malformed response for shop {shop_id!r}") from exc

        new_access_exp = now + timedelta(seconds=expires_in)
        # refresh_token mới cũng có hạn 3 tháng — Zalo không trả hạn refresh riêng, dùng
        # 90 ngày theo docs. Không load-bearing ở GĐ0 (cron refresh access hằng giờ).
        new_refresh_exp = now + timedelta(days=90)

        # Ghi cặp mới TRONG cùng transaction đã lock (_reuse_transaction=True bỏ lock lần 2
        # + không commit, ta commit thủ công sau).
        await ZaloOATokenRepo(session).update_tokens_locked(
            shop_id=shop_id,
            oa_id=oa_id,
            access_token=new_access,
            refresh_token=new_refresh,
            access_expires_at=new_access_exp,
            refresh_expires_at=new_refresh_exp,
            oa_secret_key=oa_secret,
            _reuse_transaction=True,
        )
        await session.commit()
        logger.info("zalo_refresh ok shop=%s", shop_id)
