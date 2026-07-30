"""ORM models — tenant-first schema (spec 01 §3 Sub-task B, §8).

Every row-owning table carries a `shop_id` (Text). Cross-shop leakage is prevented at the
query layer by requiring shop scope on every SELECT (retrieval/pgvector.py enforces it for
vector search; other repos will follow the same shape when they land). The tenant-isolation
gate in tests/test_tenant_isolation.py is the contract.

GĐ0 lands only the tables the Phase 2 gate exercises: `messages`, `embeddings`. Phase 5
adds `pending_reply` for the F3 copilot park path (spec §3 Sub-task E). Wider schema
(shops, sellers, customers, conversations) still deferred — Phase 5 uses free-form string
ids for those relations since GĐ0 doesn't need normalized joins.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agent.persona import PERSONA_MAX_CHARS
from agent.policy_gate import SEVERITY_RANK
from app.config import EMBED_DIM

# Alias, KHÔNG phải bản sao — mọi chỗ trong file này vẫn đọc `_EMBED_DIM` như trước, nhưng giá
# trị chỉ tồn tại ở MỘT nơi (`app/config.EMBED_DIM`). Trước spec 08 E1 đây là số 1536 viết
# cứng, tức nguồn sự thật thứ hai: đổi một bên mà quên bên kia thì insert bị từ chối ở một
# đường code còn đường khác vẫn chạy. `tests/test_embedding_dim.py` canh đúng ca lệch đó.
#
# Hướng phụ thuộc `db/` → `app/config` đã kiểm: `app/config.py` không import gì từ `db/`, nên
# không có vòng lặp. Nó chỉ import một hằng số, không kéo theo `Settings`/env.
_EMBED_DIM = EMBED_DIM


class Base(DeclarativeBase):
    """Declarative base; alembic autogenerate targets `Base.metadata`."""


class Message(Base):
    """A message in a customer conversation (inbound customer OR seller reply OR drafted).

    `shop_id` is the tenant scope — never derived from client input, always from a verified
    JWT (auth.identity.verify_token). Cross-shop reads MUST include `WHERE shop_id = :scope`
    at the SQL level; post-filter is an R1.22 breach.

    **Append-only log, KHÔNG phải hàng đợi gửi.** Ghi vào đây nghĩa là "việc này đã xảy ra",
    không phải "hãy gửi cái này". Đường duy nhất tới khách đi qua `agent/policy_gate.py`;
    drain bảng này để gửi là bypass gate.

    **Composite FK, không phải FK đơn (spec 10 H0).** Trước H0, bảng này là entity DUY NHẤT
    trong repo không có FK nào — spec 06 F0 gắn composite FK cho `Conversation`/`OrderDraft`/
    `PendingReply` rồi bỏ sót `Message`. Hệ quả không phải thẩm mỹ: không có `conversation_id`
    thì câu "load last-N của conversation này" KHÔNG viết được, và AI không phân giải nổi đại
    từ ở lượt thứ hai ("cái áo đó còn size M không").
    Lý do phải COMPOSITE giống hệt `Conversation`: `FK (conversation_id) → conversations(id)`
    chỉ khẳng định conversation TỒN TẠI, nên nó cho phép message của shop A trỏ conversation
    của shop B. Dạng composite ghim row được tham chiếu vào CÙNG shop, và Postgres tự từ chối
    thay vì trông chờ code review bắt được.
    Gate: tests/test_message_history.py::test_cross_shop_message_rejected_by_database.
    """

    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(Text, nullable=False)  # user | assistant | seller | system
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # C1 (A5, design §5.5/§9): id tin phía platform — mang khoá dedup tầng message. NULL cho
    # message không đến từ webhook (assistant/seller/system); UNIQUE bên dưới coi NULL là
    # distinct nên các row đó không bị ràng.
    platform_msg_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_msg_shop_created", "shop_id", "created_at"),
        # Index thứ hai, KHÔNG thay thế cái trên: cái cũ phục vụ truy vấn theo shop, cái này
        # phục vụ đường đọc history của H2 (`last-N của conversation này`).
        Index("idx_msg_shop_conv_created", "shop_id", "conversation_id", "created_at"),
        # C1 — chặn double-process của worker (R2 requeue ⇒ job chạy lại ⇒ append lần 2 thành
        # no-op qua ON CONFLICT). KHÁC tầng với `webhook_event_log` (chặn retry của platform).
        UniqueConstraint(
            "conversation_id", "platform_msg_id", name="uq_messages_conv_platform_msg"
        ),
        ForeignKeyConstraint(
            ["shop_id", "conversation_id"],
            ["conversations.shop_id", "conversations.id"],
            name="fk_messages_conversation_same_shop",
        ),
        ForeignKeyConstraint(
            ["shop_id", "customer_id"],
            ["customers.shop_id", "customers.id"],
            name="fk_messages_customer_same_shop",
        ),
    )


class Embedding(Base):
    """Vector chunk. `namespace` scopes by kind (chat | platform_wiki | file:{id} …);
    `shop_id` scopes by tenant. Retrieval must filter on BOTH — namespace decides where to
    look, shop_id decides whose rows are eligible. `platform_wiki` is the only shared
    namespace and even then the shop scope still applies to per-shop wiki extensions.
    """

    __tablename__ = "embeddings"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str] = mapped_column(Text, nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    chunk: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(_EMBED_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # A1 (I2): bảng duy nhất luồng A đọc được — nằm ở schema `platform`, không phải `public`.
    __table_args__ = (Index("idx_emb_shop_ns", "shop_id", "namespace"), {"schema": "platform"})


class Customer(Base):
    """An end-consumer as known to ONE shop, on ONE channel (spec 06 Phase F0).

    Deliberately NOT a global person: the same human messaging two shops is two rows. That
    keeps the tenant boundary absolute — there is no cross-shop identity object to leak
    through, and no join that could surface shop B's customer to shop A.

    `UniqueConstraint(shop_id, id)` looks redundant next to the `id` primary key, but it is
    load-bearing: it is what lets child tables declare a COMPOSITE foreign key on
    `(shop_id, customer_id)`. See `Conversation.__table_args__` for why that matters.
    """

    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)  # zalo | messenger | …
    external_id: Mapped[str] = mapped_column(Text, nullable=False)  # id phía kênh
    display_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("shop_id", "id", name="uq_customers_shop_id"),
        UniqueConstraint("shop_id", "channel", "external_id", name="uq_customers_shop_chan_ext"),
        Index("idx_customer_shop_created", "shop_id", "created_at"),
    )


class Conversation(Base):
    """A message thread between one shop and one customer on one channel (spec 06 Phase F0).

    **Composite FK, not a plain one.** `FOREIGN KEY (shop_id, customer_id)` →
    `customers(shop_id, id)` is the whole point. A plain `FK customer_id -> customers.id`
    would only assert "this customer exists" — it would happily let a shop A conversation
    point at a shop B customer, which is an R1.22 cross-tenant breach that no amount of
    code review reliably catches. The composite form makes Postgres itself reject the
    mismatch, so tenant integrity survives a buggy or hostile caller.
    Gate: tests/test_foundation_models.py::test_cross_shop_reference_rejected_by_database.

    `last_inbound_at` + `window_status` land here (not in a later ALTER) so Zalo's 48h
    reactive window has a home from day one — spec 03 Phase 10 planned to ALTER a
    `conversations` table that had never been created (spec 06 §1 finding #3).
    """

    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    channel: Mapped[str] = mapped_column(Text, nullable=False)
    external_thread_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="active"
    )  # active | warning | expired
    # A7 (design §5.5 · I13, C2): timer coalesce + khoá claim của §6.3. Tin mới đẩy lùi
    # `next_debounce_at`; `debounce_claimed_at IS NULL` trong WHERE của §6.3 là toàn bộ C2,
    # và claim này có reaper R3 gỡ (§6.9). Đường ghi DUY NHẤT: `SchedulerRepo`.
    next_debounce_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    debounce_claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # KHÔNG có trong design §5.5 (lỗ doc — Wyatt ký 2026-07-30, A7): chỗ trace G6 đi qua
    # conversation khi compose sống ở loop debounce. Batch coalesce ⇒ trace của TIN CUỐI.
    debounce_trace_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # A9 — trần thử lại cho đường compose (nơi duy nhất gọi LLM): lỗi ⇒ +1, thành công ⇒ 0,
    # đạt trần ⇒ worker NULL timer và GIỮ counter cho vận hành query (poison conversation
    # không được phép đốt LLM mỗi 5' vĩnh viễn). Xem worker_seller.MAX_COMPOSE_FAILURES.
    compose_failures: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("shop_id", "id", name="uq_conversations_shop_id"),
        # ISSUE-017 (spec 09 C0). Trước constraint này, `resolve_conversation()` là
        # select-then-insert không có gì đỡ lưng: hai tin nhắn đến đồng thời từ cùng một
        # khách ⇒ 2 conversation ⇒ lịch sử tách đôi, AI mất ngữ cảnh, KHÔNG có exception nào.
        #
        # `postgresql_nulls_not_distinct=True` là phần bắt buộc, không phải tuỳ chọn: mặc
        # định của SQL coi NULL là DISTINCT, nên một UNIQUE thường sẽ cho qua hai row
        # `(shop, cus, chan, NULL)`. Mà `external_thread_id=NULL` chính là ca phổ biến nhất
        # hôm nay (`channels/zalo` đọc `payload.get("thread_id")`, Zalo không phải lúc nào
        # cũng gửi). Thiếu cờ ⇒ constraint trông như đã vá mà thực tế không vá gì.
        #
        # Vì sao có `external_thread_id` trong khoá (phương án B, Wyatt ký 2026-07-20):
        # câu "Zalo có xoay thread_id giữa cùng một mạch không?" nằm trong PRE-004 đang
        # BLOCKED. Khi phải đoán, chọn cái mà đoán sai còn sửa được — B sai ⇒ phân mảnh,
        # gộp lại được; A sai ⇒ gộp nhầm hai mạch, và đã gộp thì không tách lại được.
        UniqueConstraint(
            "shop_id",
            "customer_id",
            "channel",
            "external_thread_id",
            name="uq_conversations_shop_cus_chan_thread",
            postgresql_nulls_not_distinct=True,
        ),
        ForeignKeyConstraint(
            ["shop_id", "customer_id"],
            ["customers.shop_id", "customers.id"],
            name="fk_conversations_customer_same_shop",
        ),
        Index("idx_conv_shop_last_inbound", "shop_id", "last_inbound_at"),
        # A7 — partial index nguyên văn design §5.5: đường quét 500ms của loop debounce.
        # Predicate trùng khít WHERE của `SchedulerRepo.due_conversations`.
        Index(
            "idx_conversations_debounce_due",
            "next_debounce_at",
            postgresql_where=text("next_debounce_at IS NOT NULL AND debounce_claimed_at IS NULL"),
        ),
        # A9 — phần BÙ của index trên, cho R3: chỉ chứa claim đang treo (vài row) để reaper
        # 10s là index probe thay vì seq-scan toàn bảng.
        Index(
            "idx_conversations_debounce_claimed",
            "debounce_claimed_at",
            postgresql_where=text("debounce_claimed_at IS NOT NULL"),
        ),
    )


class OrderDraft(Base):
    """An order the AI extracted from a conversation, parked for seller approval.

    Scope note: this is a HOLDER, not an order state machine. `status` stays in draft-land
    (`draft | confirmed | discarded`); the real `draft→paid→shipped→delivered→refunded`
    machine with its transition audit log is GĐ1 (spec 07) and must NOT be grown here.

    `status` defaults to `draft` on purpose — guardrail §1.3 says the AI never confirms an
    order by itself, so the default must not imply confirmation.
    """

    __tablename__ = "order_drafts"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    total_amount: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["shop_id", "conversation_id"],
            ["conversations.shop_id", "conversations.id"],
            name="fk_order_drafts_conversation_same_shop",
        ),
        ForeignKeyConstraint(
            ["shop_id", "customer_id"],
            ["customers.shop_id", "customers.id"],
            name="fk_order_drafts_customer_same_shop",
        ),
        Index("idx_od_shop_status_created", "shop_id", "status", "created_at"),
    )


class PendingReply(Base):
    """A drafted reply parked for seller review (spec 01 §3 Sub-task E).

    Ported shape from drnickv4's `pending_action` with the financial pieces stripped
    (`requires_2fa`, `error_code` gone) and the ownership seam (S4) tightened: every
    read/write MUST include `WHERE shop_id = :scope`. A seller for shop A can never see —
    let alone approve — shop B's parked replies. The `PendingReplyRepo` in db/repos.py
    is the ONLY sanctioned access path; ad-hoc raw SQL outside that repo is a S4 breach.

    `status` transitions: pending → approved → sent | rejected. `expired` is a future
    cron-driven state (deferred to Phase 3+ once TTLs land).
    """

    __tablename__ = "pending_reply"

    reply_id: Mapped[str] = mapped_column(Text, primary_key=True)
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    conversation_id: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    draft_text: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    decided_by: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Spec 14 A0 (workflow §2.3/§2.5/§8.1) — schema-shaping, nullable ⇒ không backfill.
    #
    # `snapshot`: dữ kiện tầng-1 tại T0 (giá/tồn/order-status). Chỗ CHỨA — đường ghi (capture
    #   lúc draft) là runtime sau, validate-lúc-ghi lúc đó. Nullable vì draft gọi trực tiếp
    #   (không qua webhook) có thể chưa có snapshot.
    # `expires_at`: TTL = min(messaging window platform, ngưỡng shop). Chỗ CHỨA — tính toán +
    #   cron expiry là runtime sau.
    # `label`: tín hiệu train auto-send (§8.1) — KHÁC `status` (lifecycle gửi). Trùng cho
    #   approve/reject, LỆCH cho `edited` (sửa text rồi duyệt: status=approved, label=edited).
    #   Gộp vào `status` = mất `edited` mãi mãi. CHECK ở DB là hàng rào không ai bypass được.
    snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    # G6 (A5): trace sinh tại webhook, xuyên webhook_event_log → outbox → đây. NOT NULL và
    # KHÔNG default — caller phải mang trace thật từ đường ingest, không được để DB bịa.
    trace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    # A8 (design §5.5 · C5, I10): output của policy_gate — lý do seller cần chú ý, sắp theo
    # SEVERITY_RANK. Rỗng = draft thường, vẫn park (phase 1 không có nhánh gửi). CHECK ở
    # __table_args__ chặn typo tầng DB — label bẩn đầu độc training set w§8.1.
    escalation_reasons: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )

    # spec 06 F0: `conversation_id` / `customer_id` were bare Text with nothing behind them —
    # they could point at ids that never existed and Postgres accepted it. Now composite FKs,
    # same reasoning as Conversation: they pin the referenced row to THIS shop, not merely to
    # an existing row. Gate: test_pending_reply_orphan_columns_now_have_fk.
    __table_args__ = (
        Index("idx_pending_shop_status_created", "shop_id", "status", "created_at"),
        CheckConstraint(
            "label IS NULL OR label IN ('approved', 'rejected', 'edited')",
            name="ck_pending_reply_label",
        ),
        # DERIVE từ policy_gate.SEVERITY_RANK — cùng cơ chế PERSONA_MAX_CHARS ở trên:
        # model là code SỐNG, chép tay bảy giá trị là nguồn sự thật thứ N (review A5-A8;
        # migration a8 giữ bản chép — snapshot đóng băng đúng nghĩa migration). B4 đổi
        # rank thì metadata tự khớp; DB thật cần migration đi kèm — gate là contract test
        # insert đủ list(SEVERITY_RANK) qua role thật.
        CheckConstraint(
            "escalation_reasons <@ ARRAY["
            + ",".join(f"'{reason}'" for reason in SEVERITY_RANK)
            + "]::text[]",
            name="escalation_reasons_known",
        ),
        ForeignKeyConstraint(
            ["shop_id", "conversation_id"],
            ["conversations.shop_id", "conversations.id"],
            name="fk_pending_reply_conversation_same_shop",
        ),
        ForeignKeyConstraint(
            ["shop_id", "customer_id"],
            ["customers.shop_id", "customers.id"],
            name="fk_pending_reply_customer_same_shop",
        ),
        # A9 — đường quét của reaper R1 (draft quá TTL, mỗi 10s): index cũ dẫn đầu bằng
        # shop_id nên R1 không dùng được; partial này chỉ chứa draft đang chờ có TTL.
        Index(
            "idx_pending_reply_ttl",
            "expires_at",
            postgresql_where=text("status = 'pending'"),
        ),
    )


# =====================================================================================
# Spec 11 S0 — `shops` là BẢNG CHA đầu tiên của `shop_id`.
#
# Trước nó, `shop_id` là Text trần ở mọi bảng và không FK về đâu: một JWT hợp lệ mang
# `shop_id` là chuỗi BẤT KỲ và mọi tầng dưới đều tin. Composite FK của spec 06/10 chặn
# được row shop A trỏ row shop B, nhưng KHÔNG chặn được một shop chưa từng tồn tại.
# =====================================================================================


class SizeRule(BaseModel):
    """Một dòng bảng size. Khoảng ĐÓNG hai đầu — biên là chỗ seller hay hiểu nhầm nhất."""

    model_config = ConfigDict(extra="forbid")

    size: str
    height_min_cm: int
    height_max_cm: int
    weight_min_kg: int
    weight_max_kg: int


class ShippingZone(BaseModel):
    model_config = ConfigDict(extra="forbid")

    zone: str
    fee_vnd: int
    eta_days: int


class ShopKnowledge(BaseModel):
    """Fact CÓ CẤU TRÚC của shop — đi hàm tra cứu tất định, KHÔNG đi RAG (D8/D9).

    Validate lúc **GHI** (`ShopProfileRepo.upsert`), không phải lúc đọc. Validate lúc đọc
    là hoãn lỗi tới thời điểm đắt nhất: `lookup_size` nổ ở production, trên dữ liệu một
    shop thật, giữa cuộc trò chuyện với khách.

    `extra="forbid"` là phần có ý nghĩa nhất ở đây, không phải sự khắt khe thừa: seller gõ
    `size_charts` (thừa `s`) mà model lặng lẽ bỏ qua ⇒ họ thấy "lưu thành công" rồi
    `lookup_size` trả `not_found` mãi mãi, và không có gì trên màn hình giải thích vì sao.
    """

    model_config = ConfigDict(extra="forbid")

    size_chart: list[SizeRule] = []
    shipping_zones: list[ShippingZone] = []


class Shop(Base):
    """Một shop có thật. `id` do onboard sinh (spec 11 S1), KHÔNG do client tự khai."""

    __tablename__ = "shops"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ShopProfile(Base):
    """Persona (văn xuôi → prompt) + knowledge (JSONB → lookup tất định) của một shop.

    **`shop_id` vừa là PK vừa là FK ⇒ đúng MỘT profile mỗi shop.** Nếu sau này cần
    versioning thì đó là bảng khác, KHÔNG phải nới PK này: hai profile "đang hoạt động"
    cho một shop nghĩa là không ai biết AI đang nói bằng giọng nào.

    **`published_at NULL` = chưa phát hành** (PRE-1102, Wyatt ký 2026-07-20). Cố ý KHÔNG có
    `profile_status`/`approved_by`/`approved_at`: chưa có người duyệt thứ hai nào tồn tại,
    nên một cột tên "approved" sẽ dựng tên cho một quy trình không có thật — và về sau sẽ
    có người đọc nó như bằng chứng đã qua kiểm duyệt. Khi Ohana review thật land thì thêm
    cột lúc đó, kèm role + queue + UI.

    **Cap `persona_md` sống ở CHECK constraint**, không chỉ ở Pydantic: Pydantic bảo vệ
    đường ứng dụng, CHECK bảo vệ mọi đường còn lại (psql tay, script seed, data-fix). Ngân
    sách token là ràng buộc của hệ thống, không nên phụ thuộc việc người ghi có nhớ dùng
    repo hay không.
    """

    __tablename__ = "shop_profile"

    shop_id: Mapped[str] = mapped_column(
        Text, ForeignKey("shops.id", name="fk_shop_profile_shop"), primary_key=True
    )
    persona_md: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    knowledge: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            f"char_length(persona_md) <= {PERSONA_MAX_CHARS}",
            name="ck_shop_profile_persona_len",
        ),
    )


class WebhookEventLog(Base):
    """Sổ idempotency cho inbound webhook (spec 14 B0, workflow §2.1 ràng buộc #2).

    Một row = "đã xử lý event này". Zalo/FB retry cùng payload ⇒ PK compound
    `(channel, platform_msg_id)` từ chối bản sao ở TẦNG DB, không dựa vào cache (workflow §2.1
    nói thẳng "Không dựa vào cache"). Đây là cơ chế chống-nhân-đôi mà `messages` cố ý KHÔNG có
    (spec 10 H1: `messages` không idempotent, dedup sống ở ĐÂY).

    ⚠️ **KHÔNG shop-scoped, KHÔNG FK về `shops`.** `platform_msg_id` duy nhất theo channel
    trên toàn nền tảng — idempotency là biên giới NỀN-TẢNG, không phải dữ liệu tenant. `shop_id`
    lưu để audit/truy vết, không vào PK và không ràng buộc FK: khi wire runtime, `shop_id` suy
    từ `(endpoint, page_id sau verify)` và có thể là sentinel/pre-verify chưa là shop thật
    (cùng lý do `embeddings._platform`, spec 11 PRE-1104).

    A5 nâng cấp bảng thành nửa đầu của §6.1 (≙ `seller.webhook_seen` design §5.4):
    `event_id` là đích FK cho `outbox`, `raw_event` giữ payload thô (worker re-derive được,
    tin khách không mất dù worker chết trước khi ghi `messages` — O8 retention còn treo),
    `trace_id` sinh tại webhook và xuyên suốt G6. Ghi vào đây CHỈ qua
    `WebhookEventRepo.record_and_enqueue` — một câu CTE §6.1, I7 cấm tách đôi.
    """

    __tablename__ = "webhook_event_log"

    channel: Mapped[str] = mapped_column(Text, primary_key=True)
    platform_msg_id: Mapped[str] = mapped_column(Text, primary_key=True)
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    raw_event: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Outbox(Base):
    """Queue của luồng B (A5 · design §5.4 · I7). `outbox` CHÍNH LÀ queue — §10 cấm
    Redis/RabbitMQ/SQS: broker ngoài = dual-write, tức chính bug mà §6.1 tồn tại để chặn.

    Hai đường ghi DUY NHẤT, cả hai là raw SQL nguyên văn trong `db/repos.py`:
    enqueue qua CTE §6.1 (`record_and_enqueue` — cùng câu lệnh với `webhook_event_log`),
    claim qua §6.2 (`OutboxRepo.claim_batch` — `FOR UPDATE SKIP LOCKED` + commit ngay).
    Model này tồn tại cho metadata + các UPDATE trạng thái sau claim (done/lỗi), KHÔNG
    phải để ORM-insert — insert qua ORM là tách §6.1 thành hai câu, I7 vỡ im lặng.

    KHÔNG FK về `shops` — `shop_id` copy từ `webhook_event_log`, nơi nó có thể là
    sentinel/pre-verify (PRE-1104, xem docstring `WebhookEventLog`).

    `status`: pending → processing → done | dead; processing kẹt >5' do worker chết sẽ
    được reaper R2 (A7) trả về pending. `dead` khi attempts chạm trần (quyết 2026-07-30,
    xem `db/repos.py::OutboxRepo.MAX_ATTEMPTS`).
    """

    __tablename__ = "outbox"

    outbox_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("webhook_event_log.event_id"), nullable=False
    )
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        PG_ENUM("pending", "processing", "done", "dead", name="outbox_status", create_type=False),
        nullable=False,
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # A9 (amend §6.2): job lỗi chờ 2^attempts giây trước khi claim lại — không có backoff
    # thì 5 attempts cháy trong ~1s và lỗi thoáng qua cũng thành dead.
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        # Hai partial index đúng design §5.4: đường claim §6.2 (pending theo created_at) và
        # đường quét của reaper R2 (processing kẹt theo claimed_at).
        Index("idx_outbox_pending", "created_at", postgresql_where=text("status = 'pending'")),
        Index(
            "idx_outbox_processing", "claimed_at", postgresql_where=text("status = 'processing'")
        ),
    )


class CostBudget(Base):
    """Sổ cái cost theo ngày — một shop một ngày một row (A6 · design §5.6 · I8).

    `reserved_tokens` (đang giữ chỗ) và `actual_tokens` (đã reconcile) tách bạch có chủ
    đích: điều kiện cap của §6.5 đọc CẢ HAI trong WHERE của UPDATE — atomic, không bao giờ
    check-rồi-update. Đường ghi DUY NHẤT là `CostRepo` (§6.5/§6.5b nguyên văn); UPDATE hai
    cột đếm ngoài repo đó là phá I8.

    FK về `shops` — KHÁC `outbox`/`webhook_event_log`: ngân sách là dữ liệu tenant thật,
    không có ca sentinel pre-verify, shop phải tồn tại trước khi có ngân sách.

    Row ngày do `CostRepo.ensure_today` tạo idempotent; §6.5 là UPDATE-first nên thiếu row
    ⇒ reserve trả 0 row ⇒ fail-closed (coi như chạm trần, không gọi LLM — w§2.4).
    """

    __tablename__ = "cost_budget"

    shop_id: Mapped[str] = mapped_column(Text, ForeignKey("shops.id"), primary_key=True)
    budget_date: Mapped[date] = mapped_column(Date, primary_key=True)
    cap_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    actual_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")


class CostReservation(Base):
    """Một lần giữ chỗ token, CÓ DANH TÍNH (A6 · design §5.6 · I13).

    Không có bảng này thì LLM timeout ⇒ `reserved_tokens` rò rỉ ⇒ shop bị khoá tới nửa
    đêm, và reaper không có cách nào biết reservation nào treo. `released_at IS NULL` =
    đang giữ chỗ; partial index bên dưới là đường quét của reaper R4 (§6.10 — A7).

    `trace_id` nối tiếp G6: webhook → outbox → draft → reservation cùng một trace, đối
    chiếu được ai giữ chỗ cho lượt nào. Composite FK ghim reservation vào đúng row ngân
    sách ngày — không bao giờ trỏ vào một ngày không tồn tại.
    """

    __tablename__ = "cost_reservation"

    reservation_id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    budget_date: Mapped[date] = mapped_column(Date, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    trace_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index(
            "idx_cost_reservation_unreleased",
            "created_at",
            postgresql_where=text("released_at IS NULL"),
        ),
        ForeignKeyConstraint(
            ["shop_id", "budget_date"],
            ["cost_budget.shop_id", "cost_budget.budget_date"],
            name="cost_reservation_shop_id_budget_date_fkey",
        ),
    )


class ZaloOAToken(Base):
    """Credentials + secret của MỘT OA (spec 17 P0, `GD0-ZALO`).

    `shop_id` PK ⇒ đúng MỘT OA per shop ở GĐ0 (multi-brand — nhiều OA cho 1 shop — là item
    riêng, không đổi PK ở đây; đổi PK sang `(shop_id, oa_id)` sau này là migration schema,
    không phải nới nhánh code).

    **`oa_secret_key` ở CÙNG BẢNG với token, không tách sang `shops`.** Cùng vòng đời liên
    kết App↔OA, cùng phải xoay khi rotate. Tách ra = 2 query mỗi webhook (P1 verify + P2
    send) + 2 nơi phải sync khi Tân cấp lại creds. `oa_secret_key` **KHÁC** `ZALO_APP_SECRET_KEY`:
    OA Secret là per-OA (dùng verify webhook, bẫy #1), App Secret là per-App (dùng OAuth v4).

    **`access_expires_at` là `TIMESTAMPTZ`, không phải `expires_in` seconds.** Zalo trả
    `expires_in` (giây) nhưng lưu như vậy phải cộng `now()` mỗi lần đọc — mở đường cho
    off-by-race giữa refresh cron và send. Snapshot lúc refresh ⇒ so sánh tất định.

    Index `idx_zalo_oa_tokens_oa_id` để P1 lookup verify key theo `oa_id` (suy từ
    `sender.id`/`recipient.id` trong webhook body chưa verify) — không dùng làm scope,
    chỉ để tra secret rồi verify.

    ⚠️ P0 chỉ dựng bảng + repo. Refresh cron (P2) sẽ update qua `update_tokens_locked`
    với `SELECT ... FOR UPDATE` — refresh_token Zalo SINGLE-USE, hai process cùng refresh
    mà không lock = mất luôn cả cặp.
    """

    __tablename__ = "zalo_oa_tokens"

    shop_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("shops.id", ondelete="CASCADE"),
        primary_key=True,
    )
    oa_id: Mapped[str] = mapped_column(Text, nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    access_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    refresh_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    oa_secret_key: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
