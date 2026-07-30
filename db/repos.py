"""Shop-scoped repositories — the ONLY sanctioned access path for tenant-scoped tables.

Every method takes a scope in the constructor (`shop_scope: str`) and every SELECT/UPDATE
statement threads it into a `WHERE shop_id = :scope` clause SQL-level. A caller cannot
build a repo without picking a shop, and one repo instance can only ever surface / mutate
rows for that shop. Ad-hoc `session.execute(select(PendingReply)…)` outside these repos is
a S4 breach.

`ConversationRepo`, `PendingReplyRepo` and `MessageRepo` live here. `Embedding` stays
in-place at the retrieval boundary because that path locks shop scope in a different layer
(`PgvectorRetriever(shop_scope=…)`).

`MessageRepo` landed in spec 10 H1 — the old note here said messages could stay as an
"orchestrator direct-insert with a verified shop_id", which was the wrong seam: it puts
`shop_id` back in the caller's hands at exactly the point where a bug becomes a cross-tenant
write. Baking the scope into the repo removes the parameter a caller could get wrong.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import Uuid, bindparam, func, select, text, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession

from agent.persona import PERSONA_MAX_CHARS
from db.models import (
    Conversation,
    CostBudget,
    Message,
    Outbox,
    PendingReply,
    ShopKnowledge,
    ShopProfile,
    ZaloOAToken,
)

# Khai tường minh thay vì nhận string tuỳ ý: `role` sai chính tả (vd "Assistant") sẽ làm
# `last_n` trả đúng row nhưng LLM đọc sai vai — hỏng âm thầm, không exception nào.
_MESSAGE_ROLES = frozenset({"user", "assistant", "seller", "system"})


class ConversationRepo:
    """Shop-scoped access to `conversations` (spec 06 Phase F0).

    Same seam as PendingReplyRepo: scope is chosen at construction, every statement carries
    `WHERE shop_id = :scope`. Note this is belt-AND-braces with the composite FKs in
    db/models.py — the FKs stop a row from being WRITTEN across shops, this repo stops rows
    from being READ across shops. Neither replaces the other.
    """

    def __init__(self, session: AsyncSession, *, shop_scope: str) -> None:
        if not shop_scope:
            raise ValueError("shop_scope is required — no default, no cross-tenant surface")
        self._session = session
        self._shop_scope = shop_scope

    async def list_recent(self, *, limit: int = 50) -> Sequence[Conversation]:
        """Most-recent-first threads for THIS shop."""
        stmt = (
            select(Conversation)
            .where(Conversation.shop_id == self._shop_scope)
            .order_by(Conversation.created_at.desc())
            .limit(limit)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def get(self, conversation_id: str) -> Conversation | None:
        """Fetch one thread by id — scoped. An id owned by another shop returns None
        (same shape as "not found"; we do not distinguish, so the caller cannot probe
        for existence of another shop's rows)."""
        stmt = (
            select(Conversation)
            .where(Conversation.shop_id == self._shop_scope)
            .where(Conversation.id == conversation_id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()


class PendingReplyRepo:
    def __init__(self, session: AsyncSession, *, shop_scope: str) -> None:
        if not shop_scope:
            raise ValueError("shop_scope is required — no default, no cross-tenant surface")
        self._session = session
        self._shop_scope = shop_scope

    async def create(
        self,
        *,
        reply_id: str,
        conversation_id: str,
        customer_id: str,
        draft_text: str,
        intent: str,
        confidence: float,
        trace_id: uuid.UUID,
        escalation_reasons: list[str] | None = None,
        snapshot: dict[str, Any] | None = None,
        expires_at: datetime | None = None,
    ) -> PendingReply:
        """Insert a new parked draft. `shop_id` is baked from the repo scope — the caller
        does NOT pass it, so a compromised caller cannot cause a row to land under a shop
        other than the one this repo was scoped to.

        `trace_id` BẮT BUỘC (A5/G6): trace sinh tại webhook, xuyên webhook_event_log →
        outbox → đây. Không default — draft không trace là draft không đối chiếu được §9.

        `escalation_reasons` (A8/C5): output của policy_gate, đã sắp theo SEVERITY_RANK —
        repo ghi nguyên trạng, KHÔNG sắp lại (thứ tự là quyết định của gate, một chỗ).
        Giá trị lạ bị CHECK `escalation_reasons_known` từ chối ở tầng DB.

        `snapshot` / `expires_at` are OPTIONAL (spec 14 A0) — the tier-1 T0 snapshot and the
        TTL are captured by deferred runtime; today every call-site omits them and the row
        lands with both NULL. Wiring them is a later phase, but the columns exist now so that
        wiring is an INSERT-shape change, not a data migration on live shop rows."""
        row = PendingReply(
            reply_id=reply_id,
            shop_id=self._shop_scope,
            conversation_id=conversation_id,
            customer_id=customer_id,
            draft_text=draft_text,
            intent=intent,
            confidence=confidence,
            status="pending",
            trace_id=trace_id,
            escalation_reasons=escalation_reasons if escalation_reasons is not None else [],
            snapshot=snapshot,
            expires_at=expires_at,
        )
        self._session.add(row)
        await self._session.commit()
        return row

    async def list_pending(self) -> Sequence[PendingReply]:
        """List parked drafts for THIS shop, oldest-first (fair queue for the seller)."""
        stmt = (
            select(PendingReply)
            .where(PendingReply.shop_id == self._shop_scope)
            .where(PendingReply.status == "pending")
            .order_by(PendingReply.created_at)
        )
        return (await self._session.execute(stmt)).scalars().all()

    async def get(self, reply_id: str) -> PendingReply | None:
        """Fetch one parked draft by id — scoped. A reply_id belonging to another shop
        returns None (not a leak, not a raise — same shape as "row not found")."""
        stmt = (
            select(PendingReply)
            .where(PendingReply.shop_id == self._shop_scope)
            .where(PendingReply.reply_id == reply_id)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def mark_decided(self, reply_id: str, *, new_status: str, decided_by: str) -> int:
        """Transition a parked reply to approved / rejected / sent. Returns the number of
        rows updated — 0 means the reply_id doesn't exist FOR THIS SHOP (either wrong
        shop, or already-decided). The `shop_id` clause is the S4 ownership seam: a shop_b
        seller cannot approve a shop_a draft even if they somehow know the reply_id."""
        if new_status not in {"approved", "rejected", "sent"}:
            raise ValueError(f"invalid status transition: {new_status!r}")
        # `label` = train signal cho auto-send (spec 14 A0, workflow §8.1) — derive TRONG repo
        # từ `new_status`, KHÔNG để caller tự khai (caller khai = chỗ ghi sai nhãn vào training
        # set). CHỈ approve/reject là quyết định của SELLER; `sent` là lifecycle worker gửi,
        # không phải tín hiệu train ⇒ KHÔNG đè label (một reply approved→sent giữ label
        # 'approved'). `edited` là đường ghi riêng khi edit-endpoint land (chưa có).
        values: dict[str, Any] = {
            "status": new_status,
            "decided_by": decided_by,
            "decided_at": datetime.now(UTC),
        }
        if new_status in {"approved", "rejected"}:
            values["label"] = new_status
        stmt = (
            update(PendingReply)
            .where(PendingReply.shop_id == self._shop_scope)
            .where(PendingReply.reply_id == reply_id)
            .where(PendingReply.status.in_(["pending", "approved"]))
            .values(**values)
        )
        # `AsyncSession.execute` is typed as returning `Result`, but a DML statement always
        # yields a `CursorResult` — that is the only variant carrying `rowcount`, and the
        # rowcount is what tells the caller whether the reply belonged to THIS shop.
        result = cast("CursorResult[Any]", await self._session.execute(stmt))
        await self._session.commit()
        return int(result.rowcount or 0)


class MessageRepo:
    """Shop-scoped access to `messages` (spec 10 Phase H1).

    **Append-only log, KHÔNG phải hàng đợi gửi.** Một row ở đây nghĩa là "việc này ĐÃ xảy
    ra", không phải "hãy gửi cái này". Đường duy nhất tới khách hàng đi qua
    `agent/policy_gate.py`; drain bảng này để gửi là bypass gate — nếu bạn đang định viết
    một worker đọc từ đây rồi gọi sender, dừng lại và đọc `agent/orchestrator.py` trước.

    **Idempotency: `append()` KHÔNG có, `append_inbound()` CÓ — cố ý hai mức** (spec 10 H1
    GOAL-AMEND → A5/C1). `append()` phục vụ message KHÔNG mang khoá platform (assistant/
    seller/system): gọi hai lần tạo HAI row, đúng như đã ký 2026-07-20. `append_inbound()`
    phục vụ tin khách từ outbox worker, mang `platform_msg_id` — khoá UNIQUE
    `(conversation_id, platform_msg_id)` (C1, design §9) làm lần ghi thứ hai thành no-op ở
    TẦNG DB: reaper R2 requeue một job kẹt thì tin khách không nhân đôi. 🚫 Đừng "vá" bằng
    select-then-insert ở bất kỳ mức nào — ISSUE-017: hai writer đồng thời vẫn lọt cả hai,
    test đơn luồng vẫn xanh. Dedup phải ở tầng DB hoặc không làm.
    """

    def __init__(self, session: AsyncSession, *, shop_scope: str) -> None:
        if not shop_scope:
            raise ValueError("shop_scope is required — no default, no cross-tenant surface")
        self._session = session
        self._shop_scope = shop_scope

    async def append(
        self,
        *,
        conversation_id: str,
        customer_id: str,
        role: str,
        content: str,
        commit: bool = True,
    ) -> Message:
        """Ghi một message. `shop_id` BAKED từ scope repo — caller KHÔNG truyền.

        Không có tham số `shop_id` nghĩa là không có tham số nào để bẻ: một caller bị lỗi
        hoặc bị chiếm quyền vẫn không ghi được row sang shop khác. Composite FK của H0 là
        lớp thứ hai — Postgres từ chối nếu `(shop_id, conversation_id)` không khớp.

        `commit=False` (spec 17 P3): KHÔNG commit — caller gộp `append` với việc ghi khác
        trong MỘT transaction. Từ A5 đường webhook không đi qua đây nữa (webhook chỉ ghi
        sổ + enqueue §6.1; tin khách do worker ghi bằng `append_inbound`), nhưng transaction
        control này vẫn đúng cho caller tương lai cần atomic-với-append.
        """
        if role not in _MESSAGE_ROLES:
            raise ValueError(f"invalid role: {role!r} (hợp lệ: {sorted(_MESSAGE_ROLES)})")
        row = Message(
            shop_id=self._shop_scope,
            conversation_id=conversation_id,
            customer_id=customer_id,
            role=role,
            content=content,
        )
        self._session.add(row)
        if commit:
            await self._session.commit()
        else:
            await self._session.flush()  # đẩy INSERT xuống DB (bắt FK sớm) nhưng chưa commit
        return row

    async def append_inbound(
        self,
        *,
        conversation_id: str,
        customer_id: str,
        content: str,
        platform_msg_id: str,
    ) -> bool:
        """Ghi tin KHÁCH từ outbox worker — idempotent qua khoá C1. Trả `True` nếu row mới,
        `False` nếu tin đã có (job requeue bởi R2 / double-claim hụt) — caller coi `False`
        là "đã ghi rồi", tiếp tục bước sau bình thường, KHÔNG phải lỗi.

        `platform_msg_id` BẮT BUỘC — đường không-có-khoá đi `append()`. Role đóng cứng
        `user`: tin từ webhook là của khách; assistant/seller/system không có platform id
        và không đi đường này.

        MỘT câu `INSERT ... ON CONFLICT DO NOTHING RETURNING` — cùng kỷ luật ISSUE-017 với
        mọi dedup khác trong repo: race hai worker thì Postgres cho đúng một bên thắng.
        """
        stmt = (
            pg_insert(Message)
            .values(
                shop_id=self._shop_scope,
                conversation_id=conversation_id,
                customer_id=customer_id,
                role="user",
                content=content,
                platform_msg_id=platform_msg_id,
            )
            .on_conflict_do_nothing(index_elements=["conversation_id", "platform_msg_id"])
            .returning(Message.id)
        )
        inserted = (await self._session.execute(stmt)).first()
        await self._session.commit()
        return inserted is not None

    async def latest_customer_message(self, conversation_id: str) -> Message | None:
        """Tin KHÁCH mới nhất của conversation — cho loop debounce tìm 'câu cần trả lời'.

        Query đúng 1 row `role='user'`, KHÔNG phải last_n rồi lọc ở Python (bug review
        A5-A8 #8): lọc trong cửa sổ N row là hàm của N — N tin phía shop liên tiếp là
        pre-scan mù tin khách và draft bị bỏ qua êm, trong khi WHERE ở SQL thì không có
        cửa sổ nào để trượt. Trả None = conversation chưa có tin khách nào (row ghi tay/
        edge) — caller quyết, không raise.
        """
        stmt = (
            select(Message)
            .where(Message.shop_id == self._shop_scope)
            .where(Message.conversation_id == conversation_id)
            .where(Message.role == "user")
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def last_n(self, conversation_id: str, *, limit: int = 20) -> list[Message]:
        """N message GẦN NHẤT của conversation này, trả theo thứ tự thời gian TĂNG dần.

        Conversation của shop khác trả **rỗng**, KHÔNG raise — raise sẽ phân biệt được
        "không tồn tại" với "tồn tại nhưng của shop khác", tức rò rỉ chính sự TỒN TẠI của
        dữ liệu shop khác. Cùng hình dạng với `PendingReplyRepo.get` trả None.

        Lấy `DESC LIMIT n` rồi đảo lại trong Python: cần n cái MỚI nhất, nhưng LLM cần đọc
        chúng theo thứ tự hội thoại. `ASC LIMIT n` sẽ lấy nhầm n cái CŨ nhất — sai âm thầm,
        và càng dài hội thoại càng sai.
        """
        if limit <= 0:
            raise ValueError(f"limit phải > 0, nhận {limit}")
        stmt = (
            select(Message)
            .where(Message.shop_id == self._shop_scope)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        rows = list((await self._session.execute(stmt)).scalars().all())
        rows.reverse()
        return rows


class ShopProfileRepo:
    """Shop-scoped access to `shop_profile` (spec 11 Phase S0).

    **Validate `knowledge` ở ĐÂY, không ở tầng API.** Đặt ở repo nghĩa là MỌI đường ghi đều
    đi qua nó — endpoint admin, script seed, test, data-fix. Đặt ở API thì mọi đường còn lại
    đều là lỗ, và cái lọt qua sẽ không nổ lúc ghi mà nổ lúc `lookup_size` chạy: ở production,
    trên dữ liệu một shop thật, giữa cuộc trò chuyện với khách.

    **Cap `persona_md` kiểm ở đây LẪN ở CHECK constraint.** Không thừa: lớp này cho thông
    báo lỗi người đọc được, CHECK là thứ raw SQL không lách được. Ngân sách token là ràng
    buộc của hệ thống, nó không nên phụ thuộc vào việc người ghi có nhớ dùng repo hay không.
    """

    def __init__(self, session: AsyncSession, *, shop_scope: str) -> None:
        if not shop_scope:
            raise ValueError("shop_scope is required — no default, no cross-tenant surface")
        self._session = session
        self._shop_scope = shop_scope

    async def get(self) -> ShopProfile | None:
        """Profile của THIS shop, hoặc None.

        Profile của shop khác trả **None**, KHÔNG raise — raise sẽ phân biệt được "không
        tồn tại" với "tồn tại nhưng của shop khác", tức rò rỉ chính sự TỒN TẠI của dữ liệu
        shop khác. Cùng hình dạng `PendingReplyRepo.get`.
        """
        stmt = select(ShopProfile).where(ShopProfile.shop_id == self._shop_scope)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def upsert(
        self,
        *,
        persona_md: str,
        knowledge: dict[str, Any],
        published_at: datetime | None = None,
    ) -> ShopProfile:
        """Ghi/đè profile. `shop_id` BAKED từ scope repo — caller KHÔNG truyền.

        Không có tham số `shop_id` nghĩa là không có tham số nào để bẻ: caller lỗi hoặc bị
        chiếm quyền vẫn không ghi được sang shop khác. FK về `shops.id` là lớp thứ hai —
        Postgres từ chối nếu shop không tồn tại.

        `knowledge` đi qua `ShopKnowledge.model_validate` (extra="forbid") TRƯỚC khi chạm
        DB. Field lạ bị TỪ CHỐI chứ không bỏ qua im lặng: seller gõ `size_charts` thừa `s`
        mà bị nuốt ⇒ họ thấy "lưu thành công" rồi `lookup_size` trả `not_found` mãi mãi.
        """
        if len(persona_md) > PERSONA_MAX_CHARS:
            raise ValueError(f"persona_md {len(persona_md)} ký tự, vượt cap {PERSONA_MAX_CHARS}")
        # Validate rồi ghi lại dạng đã chuẩn hoá — KHÔNG ghi dict thô của caller.
        validated = ShopKnowledge.model_validate(knowledge)

        row = await self.get()
        if row is None:
            row = ShopProfile(shop_id=self._shop_scope)
            self._session.add(row)
        row.persona_md = persona_md
        row.knowledge = validated.model_dump()
        if published_at is not None:
            row.published_at = published_at
        await self._session.commit()
        return row


# §6.1 NGUYÊN VĂN (design — I7, w§2.1), chỉ đổi hai thứ so với doc: tên bảng theo ánh xạ
# adopt-plan §1 (`seller.webhook_seen` → `webhook_event_log`, `seller.outbox` → `outbox`) và
# placeholder $1…$6 → bind theo tên (chi tiết driver, không đổi hình dạng câu lệnh).
#
# 🚫 MUỐN "REFACTOR" THÀNH HAI INSERT: DỪNG LẠI. `ON CONFLICT DO NOTHING` không báo cho câu
# sau biết nó có thật sự insert hay không — tách đôi là draft đôi khi retry, trong khi
# `webhook_event_log` vẫn TRÔNG đúng. Đây là bug im lặng số 1 mà I7 tồn tại để chặn.
_RECORD_AND_ENQUEUE = text("""
    WITH ins AS (
      INSERT INTO webhook_event_log (channel, platform_msg_id, shop_id, raw_event, trace_id)
      VALUES (:channel, :platform_msg_id, :shop_id, :raw_event, :trace_id)
      ON CONFLICT (channel, platform_msg_id) DO NOTHING
      RETURNING event_id, shop_id, trace_id
    )
    INSERT INTO outbox (event_id, shop_id, payload, trace_id)
    SELECT event_id, shop_id, :payload, trace_id FROM ins
    RETURNING outbox_id
""").bindparams(
    bindparam("raw_event", type_=JSONB),
    bindparam("payload", type_=JSONB),
    bindparam("trace_id", type_=Uuid()),
)


class WebhookEventRepo:
    """Idempotency + enqueue cho inbound webhook (spec 14 B0 → A5 · workflow §2.1 #2 · I7).

    KHÔNG `shop_scope`, KHÁC mọi repo khác trong file này — idempotency là biên giới
    NỀN-TẢNG, không phải dữ liệu tenant. 🚫 Đừng "sửa" thành shop-scoped: `platform_msg_id`
    duy nhất theo channel trên toàn nền tảng, và scope theo shop sẽ cho retry của cùng một
    event (đến trước lúc `shop_id` được suy ra) lọt hai lần.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_and_enqueue(
        self,
        *,
        channel: str,
        platform_msg_id: str,
        shop_id: str,
        raw_event: dict[str, Any],
        payload: dict[str, Any],
        trace_id: uuid.UUID,
    ) -> int | None:
        """Ghi sổ idempotency + enqueue outbox trong MỘT câu lệnh (§6.1). Trả `outbox_id`
        nếu đây là lần đầu thấy `(channel, platform_msg_id)`, `None` nếu đã thấy (retry) —
        caller dùng `None` để ACK 200 rồi dừng, KHÔNG xử lý lại.

        Thay thế `record_event` cũ (A5): trước đây webhook ghi sổ rồi append message cùng
        transaction; giờ webhook CHỈ ghi sổ + enqueue rồi ACK — message do worker outbox ghi
        (design §3). Race-safe cùng cơ chế cũ: hai webhook đồng thời cùng key thì Postgres
        serialize INSERT trên PK, đúng một bên thắng, bên thua nhận CTE rỗng ⇒ `None`.

        `raw_event` là payload THÔ (audit + re-derive, O8 retention còn treo); `payload` là
        bản đã chuẩn hoá + resolve identity mà worker tiêu thụ — resolve ở webhook để worker
        không cần adapter, và vì `resolve_conversation` vốn idempotent nên chạy trước không
        phá dedup.
        """
        result = await self._session.execute(
            _RECORD_AND_ENQUEUE,
            {
                "channel": channel,
                "platform_msg_id": platform_msg_id,
                "shop_id": shop_id,
                "raw_event": raw_event,
                "payload": payload,
                "trace_id": trace_id,
            },
        )
        row = result.first()
        await self._session.commit()
        return int(row[0]) if row is not None else None


# §6.2 NGUYÊN VĂN (design, amend 2026-07-30 thêm điều kiện next_retry_at) — chỉ đổi tên
# bảng theo ánh xạ adopt-plan §1. LIMIT 20 là của doc. `FOR UPDATE SKIP LOCKED` trong
# subquery + UPDATE ngoài là MỘT câu: N worker cùng chạy thì mỗi row đúng một chủ, không
# chờ lock của nhau. 🚫 Bỏ `SKIP LOCKED` = worker serialize; tách SELECT rồi UPDATE = hai
# worker claim cùng row. 🚫 Bỏ điều kiện `next_retry_at` = row lỗi bị claim lại ngay tick
# kế tiếp, 5 attempts cháy trong ~1 giây (lý do amend — xem doc §6.2).
_CLAIM_OUTBOX = text("""
    UPDATE outbox SET status='processing', claimed_at=now(), attempts=attempts+1
    WHERE outbox_id IN (
      SELECT outbox_id FROM outbox
       WHERE status='pending'
         AND (next_retry_at IS NULL OR next_retry_at <= now())
       ORDER BY created_at
       FOR UPDATE SKIP LOCKED LIMIT 20
    )
    RETURNING *
""")

# Lỗi ⇒ pending (thử lại) hay dead (chạm trần) quyết trong MỘT câu UPDATE — đọc attempts
# rồi update ở Python là check-then-act, cùng họ race mà §6.5 cấm. Backoff 2^attempts giây
# (2·4·8·16s): đủ sống qua blip DB vài giây, đủ ngắn để không giam tin khách; power/
# make_interval tính trong DB từ attempts ĐÃ ghi lúc claim — không có số nào ở Python.
_MARK_FAILED = text("""
    UPDATE outbox
       SET status = CASE WHEN attempts >= :max_attempts
                         THEN 'dead'::outbox_status
                         ELSE 'pending'::outbox_status END,
           next_retry_at = now() + make_interval(secs => power(2, attempts)),
           last_error = :error
     WHERE outbox_id = :outbox_id
""")


@dataclass(frozen=True)
class OutboxJob:
    """Một row outbox đã claim — snapshot để worker xử lý SAU khi transaction claim đã đóng."""

    outbox_id: int
    event_id: int
    shop_id: str
    payload: dict[str, Any]
    attempts: int
    trace_id: uuid.UUID


class OutboxRepo:
    """Claim + chuyển trạng thái queue (A5 · design §6.2 · I13).

    KHÔNG `shop_scope` — cùng biên nền-tảng với `WebhookEventRepo`: worker claim theo thứ
    tự đến, không theo tenant. Enqueue KHÔNG ở đây mà ở `record_and_enqueue` (§6.1 là một
    câu, không tách enqueue riêng được).
    """

    # Trần retry trước khi row thành `dead` (quyết 2026-07-30, cùng lượt duyệt A5): mỗi lần
    # claim là attempts+1, lỗi lần thứ 5 thì dừng — row độc (payload làm drafter nổ mãi) mà
    # quay vòng vô hạn là đốt LLM cost theo chu kỳ 200ms. `dead` + `last_error` là chỗ người
    # vận hành đọc; chưa có đường tự hồi — resurrect là việc của người, có chủ đích.
    MAX_ATTEMPTS = 5

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def claim_batch(self) -> list[OutboxJob]:
        """Claim tối đa 20 row pending (§6.2), COMMIT NGAY rồi mới trả về.

        Commit-ngay là yêu cầu của doc ("MUST NOT giữ transaction mở trong lúc gọi LLM"):
        giữ transaction qua lời gọi LLM là giữ row-lock hàng chục giây — mọi worker khác
        `SKIP LOCKED` bỏ qua row của mình thì không sao, nhưng vacuum/reaper và chính
        connection pool sẽ nghẹn. Sau commit, quyền sở hữu row nằm ở `status='processing'`
        chứ không ở lock; worker chết thì reaper R2 (A7) trả row về pending sau 5'.
        """
        rows = (await self._session.execute(_CLAIM_OUTBOX)).mappings().all()
        await self._session.commit()
        return [
            OutboxJob(
                outbox_id=r["outbox_id"],
                event_id=r["event_id"],
                shop_id=r["shop_id"],
                payload=r["payload"],
                attempts=r["attempts"],
                trace_id=r["trace_id"],
            )
            for r in rows
        ]

    async def mark_done(self, outbox_id: int) -> None:
        """Row xử lý xong. Giữ row (status=done) thay vì DELETE — sổ sách cho
        `test_trace_propagation` và đối chiếu §9; dọn dẹp là việc của retention sau."""
        await self._session.execute(
            update(Outbox).where(Outbox.outbox_id == outbox_id).values(status="done")
        )
        await self._session.commit()

    async def mark_failed(self, outbox_id: int, error: str) -> None:
        """Row lỗi: quay về `pending` để tick sau thử lại, hoặc `dead` khi attempts đã chạm
        trần. Đếm bằng `attempts` ĐÃ ghi lúc claim (§6.2 là chỗ duy nhất tăng nó) — không
        tăng ở đây để "claim rồi chết trước khi chạy" (R2 requeue) cũng tính là một lần thử.
        """
        await self._session.execute(
            _MARK_FAILED,
            {"max_attempts": self.MAX_ATTEMPTS, "outbox_id": outbox_id, "error": error},
        )
        await self._session.commit()


# §6.3 NGUYÊN VĂN (design — PRE-010 C2), đổi tên theo ánh xạ: `seller.conversation` →
# `conversations`, cột khoá `conversation_id` → `id`.
#
# 🚫 BỎ `debounce_claimed_at IS NULL` khỏi WHERE = hai draft cho một hội thoại — hai
# scheduler cùng thấy "đến hạn" rồi cùng compose, và không câu nào lỗi. `IS NULL` trong
# WHERE là TOÀN BỘ nội dung C2: Postgres serialize UPDATE trên row, đúng một bên thắng.
_CLAIM_DEBOUNCE = text("""
    UPDATE conversations SET debounce_claimed_at = now()
     WHERE id = :conversation_id
       AND next_debounce_at <= now()
       AND debounce_claimed_at IS NULL
    RETURNING id
""")

# Tin mới ĐẨY LÙI timer (w§2.2 coalesce) + chở trace G6 của tin cuối sang conversation.
_SET_DEBOUNCE = text("""
    UPDATE conversations
       SET next_debounce_at = now() + make_interval(secs => :delay_seconds),
           debounce_trace_id = :trace_id
     WHERE id = :conversation_id
""").bindparams(bindparam("trace_id", type_=Uuid()))

# Đọc đúng theo partial index idx_conversations_debounce_due (predicate trùng khít).
# `next_debounce_at` đi kèm làm ECHO cho finish — xem _FINISH_DEBOUNCE.
_DUE_CONVERSATIONS = text("""
    SELECT id, shop_id, customer_id, channel, debounce_trace_id, next_debounce_at
      FROM conversations
     WHERE next_debounce_at IS NOT NULL AND debounce_claimed_at IS NULL
       AND next_debounce_at <= now()
     ORDER BY next_debounce_at
     LIMIT :limit
""")

# Thả claim sau khi compose xong — HAI echo, mỗi cái chặn một bug review A5-A8:
# · :due_at — `next_debounce_at` chỉ về NULL khi VẪN LÀ ĐÚNG timer đã claim; tin đến GIỮA
#   lúc compose đặt timer MỚI thì giữ nguyên ⇒ compose lại sau (O9). 🚫 Đừng "đơn giản
#   hoá" thành so `<= now()`: compose (LLM) thường lâu hơn DEBOUNCE_DELAY_SECONDS, timer
#   mới cũng đã quá hạn lúc finish — so với now() là nuốt luôn tin B (#1).
# · :claimed_at trong WHERE — token sở hữu: compose treo >5' bị R3 gỡ rồi worker khác
#   claim lại thì finisher CŨ (mang claimed_at đã bị đè) thành no-op, không xoá claim
#   của người đang compose ⇒ không draft đôi (#5). Cùng bài với cost_reservation có danh
#   tính: claim vô danh thì không release an toàn được.
# `compose_failures = 0`: compose THÀNH CÔNG reset trần poison — chỉ chuỗi lỗi liên tiếp
# mới tích đủ MAX_COMPOSE_FAILURES.
_FINISH_DEBOUNCE = text("""
    UPDATE conversations
       SET debounce_claimed_at = NULL,
           compose_failures = 0,
           next_debounce_at = CASE WHEN next_debounce_at = :due_at
                                   THEN NULL ELSE next_debounce_at END
     WHERE id = :conversation_id AND debounce_claimed_at = :claimed_at
""")

# Trần poison (review A5-A8 #6): compose lỗi ⇒ +1 (một câu, không check-then-act).
_RECORD_COMPOSE_FAILURE = text("""
    UPDATE conversations SET compose_failures = compose_failures + 1
     WHERE id = :conversation_id
    RETURNING compose_failures
""")

# Bỏ cuộc (đạt trần): NULL timer để thôi retry, GIỮ counter cho vận hành query (quyết
# 2026-07-30: dừng + counter, không park draft ESCALATE). Echo :claimed_at cùng lý do
# finish — không dọn claim của worker khác đang compose.
_GIVE_UP_DEBOUNCE = text("""
    UPDATE conversations
       SET next_debounce_at = NULL, debounce_claimed_at = NULL
     WHERE id = :conversation_id AND debounce_claimed_at = :claimed_at
""")

# ── Reaper — 4 việc, design §3, chạy mỗi 10s. R3/R4 NGUYÊN VĂN §6.9/§6.10. ──────────────
# R1 · draft hết TTL → expired. `expires_at IS NOT NULL`: TTL chưa wire (A0 để nullable),
# row không TTL thì không bao giờ expire — đúng hành vi trước khi B5 wire TTL thật.
_REAP_R1_EXPIRED_DRAFTS = text("""
    UPDATE pending_reply SET status = 'expired'
     WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < now()
""")

# R2 · outbox kẹt processing >5' → pending, TRỪ KHI đã cạn attempts → dead (review A5-A8
# #4: job giết chết cả process không bao giờ đi qua mark_failed, nên trần MAX_ATTEMPTS
# phải được kiểm ở ĐÂY nữa — thiếu nó là crash-loop mỗi 5' vĩnh viễn, kéo sập cả worker).
# attempts KHÔNG tăng ở đây — §6.2 là chỗ duy nhất tăng, lần claim lại sẽ đếm.
_REAP_R2_STUCK_OUTBOX = text("""
    UPDATE outbox
       SET status = CASE WHEN attempts >= :max_attempts
                         THEN 'dead'::outbox_status
                         ELSE 'pending'::outbox_status END,
           last_error = CASE WHEN attempts >= :max_attempts
                             THEN 'r2_exhausted: worker chết ' || attempts::text || ' lần'
                             ELSE last_error END
     WHERE status = 'processing' AND claimed_at < now() - interval '5 minutes'
""")

# R3 · §6.9 NGUYÊN VĂN — thiếu câu này, worker chết sau §6.3 ⇒ conversation rơi khỏi
# partial index (claimed IS NOT NULL) ⇒ im lặng VĨNH VIỄN. Đây là lý do I13 tồn tại.
_REAP_R3_STUCK_DEBOUNCE = text("""
    UPDATE conversations SET debounce_claimed_at = NULL
     WHERE debounce_claimed_at < now() - interval '5 minutes'
""")

# R4 · §6.10 NGUYÊN VĂN (amend 2026-07-30 thêm CTE agg) — release reservation treo (LLM
# timeout giữa reserve và reconcile). 🚫 Bỏ `agg` mà join thẳng `rel`: UPDATE…FROM chỉ áp
# MỘT row FROM mỗi row đích — hai reservation treo cùng shop/ngày chỉ trừ được một, phần
# còn lại rò trong reserved_tokens tới nửa đêm và không còn gì cho R4 gỡ (lý do amend).
# GREATEST(0, ...) phòng double-release; không thay cho việc release đúng.
_REAP_R4_STUCK_RESERVATIONS = text("""
    WITH rel AS (
      UPDATE cost_reservation SET released_at = now()
       WHERE released_at IS NULL AND created_at < now() - interval '5 minutes'
      RETURNING shop_id, budget_date, tokens
    ), agg AS (
      SELECT shop_id, budget_date, sum(tokens) AS tokens
        FROM rel GROUP BY shop_id, budget_date
    )
    UPDATE cost_budget b
       SET reserved_tokens = GREATEST(0, b.reserved_tokens - a.tokens)
      FROM agg a
     WHERE b.shop_id = a.shop_id AND b.budget_date = a.budget_date
""")


@dataclass(frozen=True)
class DebounceDue:
    """Một conversation đến hạn compose — snapshot cho loop debounce."""

    conversation_id: str
    shop_id: str
    customer_id: str
    channel: str
    trace_id: uuid.UUID | None  # None: row từ trước A7 / ghi tay — caller tự sinh trace mới
    due_at: datetime  # giá trị timer LÚC ĐỌC — echo cho finish_debounce, xem _FINISH_DEBOUNCE


class SchedulerRepo:
    """Debounce scheduler + reaper (A7 · design §6.3 §6.9 §6.10 · I13, C2).

    KHÔNG `shop_scope` — cùng biên nền-tảng với `OutboxRepo`: scheduler quét mọi shop theo
    thứ tự đến hạn, không theo tenant. Tenant isolation sống ở bước compose (MessageRepo/
    PendingReplyRepo scope theo shop_id ĐỌC TỪ ROW conversation, không từ input ngoài).
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def set_debounce(
        self, *, conversation_id: str, delay_seconds: float, trace_id: uuid.UUID
    ) -> None:
        """Đặt/đẩy lùi timer compose — outbox loop gọi SAU khi ghi message. Gọi lặp với tin
        mới là chủ đích (coalesce): timer dời về sau, draft trả lời cả cụm tin."""
        await self._session.execute(
            _SET_DEBOUNCE,
            {
                "conversation_id": conversation_id,
                "delay_seconds": delay_seconds,
                "trace_id": trace_id,
            },
        )
        await self._session.commit()

    async def due_conversations(self, *, limit: int = 20) -> list[DebounceDue]:
        """Các conversation đến hạn compose, cũ nhất trước. Đọc KHÔNG claim — caller phải
        `claim_debounce` từng cái trước khi compose (đọc-rồi-claim là hai bước, nhưng an
        toàn: claim §6.3 mới là chỗ quyết, đọc chỉ để biết ứng viên)."""
        rows = (await self._session.execute(_DUE_CONVERSATIONS, {"limit": limit})).all()
        return [
            DebounceDue(
                conversation_id=r[0],
                shop_id=r[1],
                customer_id=r[2],
                channel=r[3],
                trace_id=r[4],
                due_at=r[5],
            )
            for r in rows
        ]

    async def claim_debounce(self, conversation_id: str) -> datetime | None:
        """§6.3 — trả `debounce_claimed_at` vừa ghi (token sở hữu, echo lúc finish/give-up),
        `None` = instance khác đã lấy (hoặc chưa đến hạn) ⇒ bỏ qua, KHÔNG chờ. Đúng 1
        draft dù N scheduler (C2). Token đọc bằng SELECT sau claim trong CÙNG session —
        §6.3 giữ nguyên văn (RETURNING id), không nới RETURNING."""
        row = (
            await self._session.execute(_CLAIM_DEBOUNCE, {"conversation_id": conversation_id})
        ).first()
        if row is None:
            await self._session.commit()
            return None
        claimed_at = (
            await self._session.execute(
                text("SELECT debounce_claimed_at FROM conversations WHERE id = :cid"),
                {"cid": conversation_id},
            )
        ).scalar_one()
        await self._session.commit()
        return cast("datetime", claimed_at)

    async def finish_debounce(
        self, conversation_id: str, *, due_at: datetime, claimed_at: datetime
    ) -> None:
        """Thả claim sau compose THÀNH CÔNG (reset luôn compose_failures). Hai echo:
        `due_at` giữ timer tin-giữa-compose, `claimed_at` chặn finisher cũ xoá claim của
        worker khác (xem _FINISH_DEBOUNCE). Compose LỖI thì ĐỪNG gọi — đi đường
        `record_compose_failure`, để claim treo cho R3 gỡ sau 5'."""
        await self._session.execute(
            _FINISH_DEBOUNCE,
            {"conversation_id": conversation_id, "due_at": due_at, "claimed_at": claimed_at},
        )
        await self._session.commit()

    async def record_compose_failure(self, conversation_id: str) -> int:
        """Compose lỗi ⇒ +1, trả số lỗi LIÊN TIẾP hiện tại — caller so với trần để bỏ cuộc.
        Không reset ở đây; reset sống trong _FINISH_DEBOUNCE (chỉ thành công mới xoá)."""
        count = (
            await self._session.execute(
                _RECORD_COMPOSE_FAILURE, {"conversation_id": conversation_id}
            )
        ).scalar_one()
        await self._session.commit()
        return int(count)

    async def give_up_debounce(self, conversation_id: str, *, claimed_at: datetime) -> None:
        """Đạt trần poison: thôi retry (NULL timer + thả claim), GIỮ compose_failures cho
        vận hành query hội thoại bị bỏ cuộc. Echo `claimed_at` — không dọn claim của
        worker khác."""
        await self._session.execute(
            _GIVE_UP_DEBOUNCE, {"conversation_id": conversation_id, "claimed_at": claimed_at}
        )
        await self._session.commit()

    async def reap(self) -> dict[str, int]:
        """Bốn việc reaper (design §3) trong MỘT lần gọi, trả số row mỗi việc để log.

        Mỗi câu commit chung một lần — bốn việc độc lập nhau, nhưng không có lý do tách
        transaction: reaper chạy mỗi 10s, câu nào cũng idempotent, lỗi một câu thì lần
        chạy sau làm lại cả bốn."""
        counts: dict[str, int] = {}
        params_by_stmt: tuple[tuple[str, Any, dict[str, Any]], ...] = (
            ("r1_expired_drafts", _REAP_R1_EXPIRED_DRAFTS, {}),
            # R2 mang trần attempts của OutboxRepo — job giết chết process không đi qua
            # mark_failed, nên đây là chỗ thứ hai (và cuối cùng) trần được kiểm.
            ("r2_stuck_outbox", _REAP_R2_STUCK_OUTBOX, {"max_attempts": OutboxRepo.MAX_ATTEMPTS}),
            ("r3_stuck_debounce", _REAP_R3_STUCK_DEBOUNCE, {}),
            ("r4_stuck_reservations", _REAP_R4_STUCK_RESERVATIONS, {}),
        )
        for name, stmt, params in params_by_stmt:
            result = cast("CursorResult[Any]", await self._session.execute(stmt, params))
            counts[name] = int(result.rowcount or 0)
        await self._session.commit()
        return counts


# §6.5 NGUYÊN VĂN (design — I8, I13), đổi tên bảng theo ánh xạ adopt-plan §1 + bind theo tên.
#
# 🚫 Điều kiện cap nằm TRONG WHERE của UPDATE — sức mạnh của câu này. "Đọc budget, so ở
# Python, rồi UPDATE" là race kinh điển: hai request cùng thấy còn chỗ rồi cùng cộng, shop
# vượt cap mà không câu nào sai. 🚫 Cộng `reserved_tokens` mà không ghi `cost_reservation`
# cũng cấm — reservation vô danh thì reaper R4 không gỡ được (I13).
_RESERVE_COST = text("""
    WITH upd AS (
      UPDATE cost_budget
         SET reserved_tokens = reserved_tokens + :tokens
       WHERE shop_id = :shop_id AND budget_date = CURRENT_DATE
         AND reserved_tokens + actual_tokens + :tokens <= cap_tokens
      RETURNING shop_id, budget_date
    )
    INSERT INTO cost_reservation (shop_id, budget_date, tokens, trace_id)
    SELECT shop_id, budget_date, :tokens, :trace_id FROM upd
    RETURNING reservation_id
""").bindparams(bindparam("trace_id", type_=Uuid()))

# §6.5b NGUYÊN VĂN — release + trừ reserved + cộng token THẬT, một câu. Guard
# `released_at IS NULL` làm double-reconcile thành no-op (CTE rỗng ⇒ UPDATE ngoài 0 row).
_RECONCILE_COST = text("""
    WITH rel AS (
      UPDATE cost_reservation SET released_at = now()
       WHERE reservation_id = :reservation_id AND released_at IS NULL
      RETURNING shop_id, budget_date, tokens
    )
    UPDATE cost_budget b
       SET reserved_tokens = b.reserved_tokens - rel.tokens,
           actual_tokens   = b.actual_tokens   + :actual_tokens
      FROM rel
     WHERE b.shop_id = rel.shop_id AND b.budget_date = rel.budget_date
""")


class CostRepo:
    """Cost cap pre-charge (A6 · design §6.5 §6.5b · I8, I13).

    Shop-scoped như mọi repo tenant: scope bake ở constructor, caller không truyền
    `shop_id` vào method nào — không có tham số để bẻ. Nhịp dùng (wire ở B7):

        ensure_today(cap) → reserve(ước_lượng, trace) → gọi LLM → reconcile(id, token_thật)

    LLM nổ/timeout giữa reserve và reconcile ⇒ reservation treo, reaper R4 (A7) release
    sau 5'. Timeout của router (O10) PHẢI ngắn hơn 5' — reconcile đến SAU khi R4 đã
    release là no-op nhờ guard, token thật của lượt đó không vào sổ.
    """

    def __init__(self, session: AsyncSession, *, shop_scope: str) -> None:
        if not shop_scope:
            raise ValueError("shop_scope is required — no default, no cross-tenant surface")
        self._session = session
        self._shop_scope = shop_scope

    async def ensure_today(self, *, cap_tokens: int) -> None:
        """Tạo row ngân sách CURRENT_DATE nếu chưa có — idempotent, KHÔNG đè cap đang có.

        Tách khỏi `reserve` có chủ đích: §6.5 là UPDATE-first nguyên văn, nhét provisioning
        vào đó là đổi hình dạng câu lệnh. `cap_tokens` do caller truyền — nguồn cap thật
        (`shop_config.cost_cap_tokens_day`, design §5.3) chưa có bảng, B7 quyết khi wire;
        không đặt hằng số mặc định ở đây để khỏi thành nguồn sự thật thứ hai.

        ON CONFLICT DO NOTHING thay vì DO UPDATE: đổi cap giữa ngày là quyết định vận hành
        có chủ đích (row đã có reserved/actual đang đếm), không phải side-effect của một
        lần gọi ensure.
        """
        stmt = (
            pg_insert(CostBudget)
            .values(
                shop_id=self._shop_scope,
                budget_date=func.current_date(),
                cap_tokens=cap_tokens,
            )
            .on_conflict_do_nothing(index_elements=["shop_id", "budget_date"])
        )
        await self._session.execute(stmt)
        await self._session.commit()

    async def reserve(self, *, tokens: int, trace_id: uuid.UUID) -> int | None:
        """Giữ chỗ `tokens` cho hôm nay (§6.5). Trả `reservation_id`, hoặc `None` khi
        chạm trần HOẶC chưa có row ngân sách hôm nay — hai ca cùng một nghĩa cho caller:
        KHÔNG gọi LLM, cổng chính sách chuyển GIỮ với `reason='cost_cap'` (w§2.4).
        Fail-closed có chủ đích: thiếu provisioning thì im lặng không-tốn-tiền, không phải
        im lặng tốn-không-giới-hạn."""
        if tokens <= 0:
            raise ValueError(f"tokens phải > 0, nhận {tokens}")
        row = (
            await self._session.execute(
                _RESERVE_COST,
                {"shop_id": self._shop_scope, "tokens": tokens, "trace_id": trace_id},
            )
        ).first()
        await self._session.commit()
        return int(row[0]) if row is not None else None

    async def reconcile(self, *, reservation_id: int, actual_tokens: int) -> bool:
        """Trả chỗ đã giữ + ghi token THẬT (§6.5b). Trả `False` nếu reservation đã released
        (double-reconcile, hoặc R4 đã gỡ trước) — no-op, caller chỉ nên log.

        `reservation_id` là danh tính toàn cục (identity), không lọc theo shop trong câu
        §6.5b nguyên văn — id do chính `reserve` của repo NÀY trả ra trong cùng lượt xử lý,
        không phải input ngoài."""
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                _RECONCILE_COST,
                {"reservation_id": reservation_id, "actual_tokens": actual_tokens},
            ),
        )
        await self._session.commit()
        return int(result.rowcount or 0) > 0


class ZaloOATokenRepo:
    """Zalo OA credentials + verify secret per shop (spec 17 P0, `GD0-ZALO`).

    KHÔNG `shop_scope` — cùng biên với `WebhookEventRepo`: đây là bảng nền-tảng
    (credentials/creds-adjacent), lookup theo `shop_id` (từ auth) HOẶC theo `oa_id` (từ
    webhook body chưa verify, chỉ tra key rồi verify signature). Method `get_by_shop` dùng
    khi đã có scope; `get_oa_secret_by_oa_id` là seam của P1 verify.

    `update_tokens_locked` PHẢI dùng `SELECT ... FOR UPDATE` — refresh_token Zalo là
    SINGLE-USE, hai process refresh cùng shop mà không lock = 1 process ghi cặp mới, 1
    process refresh trên cặp CŨ (đã bị Zalo invalidate) rồi ghi đè cặp mới bằng lỗi. Kết
    quả: mất luôn khả năng refresh, phải re-auth code manual (cần OA admin). Lock scope là
    1 row PostgreSQL, không advisory global — không serialize giữa các shop khác nhau.

    P0 chỉ dựng seam. Refresh cron trong P2 sẽ implement pattern double-check:
    (1) BEGIN; SELECT ... FOR UPDATE trả row (2) nếu `access_expires_at > now() + margin`
    ⇒ process khác đã refresh xong, dùng luôn (3) nếu chưa ⇒ gọi Zalo refresh + ghi cặp mới
    + COMMIT.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_shop(self, shop_id: str) -> ZaloOAToken | None:
        """Row theo `shop_id` PK, hoặc None."""
        stmt = select(ZaloOAToken).where(ZaloOAToken.shop_id == shop_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_oa_secret_by_oa_id(self, oa_id: str) -> str | None:
        """Verify key theo `oa_id` — dùng ở P1 signature verify.

        `oa_id` KHÔNG unique ở DB (2 shop có thể liên kết cùng OA test/shared brand). Ở
        runtime thật, **operational requirement là 1 OA thuộc 1 shop per env** — enforcement
        ở tầng onboard/admin, không tầng DB constraint (để test env chia sẻ OA giữa 2 shop
        dev/staging). `LIMIT 1` không xác định row nào — thứ tự do Postgres quyết, có thể
        đổi giữa các version.

        **P1 review MED 4 defer to P4:** khi P4 land `endpoint_to_shop` map với binding
        `(channel, external_id) → (shop_id, allowed_oa_ids)`, verify sẽ check candidate ∈
        allowed_oa_ids trước khi tra secret — lookup ambiguity không còn nguy hiểm vì
        endpoint đã pre-declare oa_id nào chấp nhận. Hôm nay P4 BLOCKED, gap này chỉ
        materialize khi mount (enabled=True) + có multi-shop shared oa_id trong DB thật.
        """
        stmt = select(ZaloOAToken.oa_secret_key).where(ZaloOAToken.oa_id == oa_id).limit(1)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def update_tokens_locked(
        self,
        *,
        shop_id: str,
        oa_id: str,
        access_token: str,
        refresh_token: str,
        access_expires_at: datetime,
        refresh_expires_at: datetime,
        oa_secret_key: str,
        _reuse_transaction: bool = False,
    ) -> None:
        """Upsert row với `SELECT ... FOR UPDATE` lock — race-safe cho refresh cron.

        Nếu row chưa tồn tại: lock KHÔNG có tác dụng (không có row để lock), fallback về
        INSERT — chấp nhận được vì "chưa tồn tại" là initial seed từ OAuth code flow (P2),
        không race với refresh cron. Refresh chỉ chạy khi đã có row.

        `_reuse_transaction=True` cho test concurrent (writer_a đã mở transaction + lock
        bằng `_lock_row_for_test`). Production caller luôn dùng default (False) — mỗi call
        tự mở/commit transaction.
        """
        if not _reuse_transaction:
            # BEGIN implicit — SQLAlchemy async session bắt đầu transaction ở query đầu tiên.
            # `FOR UPDATE` sẽ block writer khác cho tới commit/rollback.
            lock_stmt = select(ZaloOAToken).where(ZaloOAToken.shop_id == shop_id).with_for_update()
            await self._session.execute(lock_stmt)

        upsert_stmt = (
            pg_insert(ZaloOAToken)
            .values(
                shop_id=shop_id,
                oa_id=oa_id,
                access_token=access_token,
                refresh_token=refresh_token,
                access_expires_at=access_expires_at,
                refresh_expires_at=refresh_expires_at,
                oa_secret_key=oa_secret_key,
            )
            .on_conflict_do_update(
                index_elements=["shop_id"],
                set_={
                    "oa_id": oa_id,
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "access_expires_at": access_expires_at,
                    "refresh_expires_at": refresh_expires_at,
                    "oa_secret_key": oa_secret_key,
                    "updated_at": datetime.now(UTC),
                },
            )
        )
        await self._session.execute(upsert_stmt)
        if not _reuse_transaction:
            await self._session.commit()

    async def _lock_row_for_test(self, shop_id: str) -> None:
        """Test-only helper — mở transaction + lock row để mô phỏng process A giữ lock
        trong khi test writer B đang chờ. KHÔNG dùng ở production code (name-prefix `_`).
        """
        lock_stmt = select(ZaloOAToken).where(ZaloOAToken.shop_id == shop_id).with_for_update()
        await self._session.execute(lock_stmt)
