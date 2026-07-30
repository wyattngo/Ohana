"""Adversarial tenant-isolation gate (spec 01 Phase 2 step 5).

Written BEFORE db/models.py, db/session.py, auth/identity.py, and the retrieval/pgvector.py
`shop_scope` wire — expected RED until steps 6/7/8 land. Failure mode we're guarding against:
cross-shop data leak (R1.22 analog) — one shop querying the DB returns another shop's row.

Three layers of isolation, each an independent test:
  1. SQL row-level — a query for shop A never surfaces a shop B row (WHERE shop_id filter)
  2. pgvector similarity — PgvectorRetriever(shop_scope="A") never returns B rows even when
     B's vector is nearer to the query in cosine distance (SQL-level filter, NOT post-filter)
  3. JWT identity — verified identity carries (user_id, shop_id, role); missing shop_id claim
     is rejected outright (the body/client can never provide shop_id — must come from the token)

Requires a live Postgres with pgvector 0.5+ enabled at DATABASE_URL (default:
postgresql+psycopg://ohana:ohana@localhost:5432/ohana). CI provisions this via the
pgvector/pgvector:pg16 service (spec 02 phase 1.3 .github/workflows/ci.yml).
"""

from __future__ import annotations

import os

import pytest

from app.config import EMBED_DIM

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://ohana:ohana@localhost:5432/ohana"
)

# OpenAI text-embedding-3-small default; step 6 wires this into Embedding.embedding column
# Đọc từ nguồn sự thật, KHÔNG khai lại số. Trước spec 08 E1 đây là `1536` viết cứng —
# và khi cột DB đổi sang 1024 thì test này vỡ với lỗi dim mismatch, tức nó đang khoá
# một hằng số thay vì khoá một hành vi. Import thì nó đi theo schema.
VECTOR_DIM = EMBED_DIM


@pytest.mark.asyncio
async def test_sql_row_scope_isolates_shops() -> None:
    """Layer 1 — SQL WHERE shop_id filter: query for shop A never returns shop B rows."""
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from db.models import Base, Conversation, Customer, Message  # step 6 lands these

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as s:
        # Spec 10 H0 làm `conversation_id`/`customer_id` thành NOT NULL + composite FK, nên
        # `Message` không còn dựng trần được nữa — phải có parent thật cho từng shop. Đây là
        # thay đổi ĐÚNG hướng: message mồ côi (không thuộc conversation nào) chính là thứ
        # khiến "load last-N của conversation" vô nghĩa. Test vẫn kiểm đúng một điều như cũ —
        # WHERE shop_id lọc sạch cross-shop — chỉ là giờ nó chạy trên dữ liệu hợp lệ.
        # Flush theo TỪNG TẦNG, không gộp: unit-of-work của SQLAlchemy không suy ra được
        # thứ tự từ composite FK khai trong `__table_args__`, nên gộp một flush sẽ đẩy
        # `conversations` đi trước `customers` và Postgres từ chối. customers → conversations
        # → messages, mỗi tầng một flush.
        for shop in ("shop_a", "shop_b"):
            s.add(Customer(id=f"cus_{shop}", shop_id=shop, channel="zalo", external_id=shop))
        await s.flush()

        for shop in ("shop_a", "shop_b"):
            s.add(
                Conversation(
                    id=f"conv_{shop}", shop_id=shop, customer_id=f"cus_{shop}", channel="zalo"
                )
            )
        await s.flush()

        for shop, body in (
            ("shop_a", "from shop a — private"),
            ("shop_b", "from shop b — private"),
        ):
            s.add(
                Message(
                    shop_id=shop,
                    conversation_id=f"conv_{shop}",
                    customer_id=f"cus_{shop}",
                    role="user",
                    content=body,
                )
            )
        await s.commit()

    async with session_factory() as s:
        rows = (await s.execute(select(Message).where(Message.shop_id == "shop_a"))).scalars().all()

    await engine.dispose()

    assert len(rows) == 1, f"shop_a query returned {len(rows)} rows (expected exactly 1)"
    assert rows[0].shop_id == "shop_a"
    assert "shop b" not in rows[0].content.lower(), "cross-shop content leaked into shop_a result"


@pytest.mark.asyncio
async def test_pgvector_retriever_shop_scope_hard_filter() -> None:
    """Layer 2 — pgvector: PgvectorRetriever(shop_scope='A') never surfaces shop B rows even
    when B's vector is nearer in cosine distance. This must be a SQL-level WHERE, NOT post-filter.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from db.models import Base, Embedding  # step 6
    from retrieval.pgvector import PgvectorRetriever  # step 8 adds shop_scope param

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    # Adversarial setup: B's embedding is CLOSER to the query than A's. Without a shop-scope
    # hard filter, a naive top-k would return B first — that's the R1.22 leak we're preventing.
    async with session_factory() as s:
        s.add(
            Embedding(
                shop_id="shop_a",
                namespace="chat",
                source_ref="a1",
                chunk="shop A private content",
                embedding=[0.1] * VECTOR_DIM,
            )
        )
        s.add(
            Embedding(
                shop_id="shop_b",
                namespace="chat",
                source_ref="b1",
                chunk="shop B private content",
                embedding=[0.9] * VECTOR_DIM,  # nearer to query
            )
        )
        await s.commit()

    retriever = PgvectorRetriever(session_factory, shop_scope="shop_a")
    hits = await retriever.search(namespaces=["chat"], query=[0.9] * VECTOR_DIM, k=10)

    await engine.dispose()

    assert len(hits) == 1, f"expected exactly 1 hit for shop_a, got {len(hits)}"
    assert "shop A" in hits[0].chunk, "shop_a retrieval returned wrong chunk"
    assert not any("shop B" in h.chunk for h in hits), "cross-shop chunk LEAKED — R1.22 breach"


def test_jwt_identity_carries_shop_id() -> None:
    """Layer 3 — JWT: verify_token returns Identity(user_id, shop_id, role). shop_id MUST come
    from the signed claim, never from the request body (R1_TIER_HINT / R1_TIER_FROM_BODY analog).
    """
    import jwt as pyjwt

    from auth.identity import Identity, verify_token  # step 7

    secret = "test-secret-do-not-use-in-prod"  # noqa: S105 — fixture literal, not a real secret

    # Happy path: signed token with shop_id → Identity carries it verbatim.
    tok = pyjwt.encode(
        {"sub": "u1", "shop_id": "shop_a", "role": "seller"}, secret, algorithm="HS256"
    )
    ident = verify_token(tok, secret=secret)
    assert isinstance(ident, Identity)
    assert ident.user_id == "u1"
    assert ident.shop_id == "shop_a"
    assert ident.role == "seller"

    # Adversarial: token missing shop_id claim → REJECT (must never fall through to a default).
    tok_no_shop = pyjwt.encode({"sub": "u1", "role": "seller"}, secret, algorithm="HS256")
    with pytest.raises(ValueError, match=r"(?i)shop_id"):
        verify_token(tok_no_shop, secret=secret)

    # Adversarial: bad signature → REJECT (pyjwt raises InvalidSignatureError).
    with pytest.raises(pyjwt.InvalidSignatureError):
        verify_token(tok, secret="different-secret")  # noqa: S106 — fixture literal
