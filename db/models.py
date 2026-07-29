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

from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from pydantic import BaseModel, ConfigDict
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agent.persona import PERSONA_MAX_CHARS
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("idx_msg_shop_created", "shop_id", "created_at"),
        # Index thứ hai, KHÔNG thay thế cái trên: cái cũ phục vụ truy vấn theo shop, cái này
        # phục vụ đường đọc history của H2 (`last-N của conversation này`).
        Index("idx_msg_shop_conv_created", "shop_id", "conversation_id", "created_at"),
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

    __table_args__ = (Index("idx_emb_shop_ns", "shop_id", "namespace"),)


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

    B0 chỉ dựng bảng + repo. Wire vào `api/webhook.py` là runtime `GD0-INGEST`, cần
    signature-verify (`GD0-ZALO`, PRE-004) đứng trước.
    """

    __tablename__ = "webhook_event_log"

    channel: Mapped[str] = mapped_column(Text, primary_key=True)
    platform_msg_id: Mapped[str] = mapped_column(Text, primary_key=True)
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
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
