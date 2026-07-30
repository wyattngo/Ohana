"""C0 gate (spec `09-Task-OhanaAISeller-ConversationRace.md` §7 Phase C0) — đóng ISSUE-017.

Viết TRƯỚC constraint + migration ⇒ expected RED.

**Bài học khi viết chính file này — đọc trước khi "dọn dẹp" nó.** Bản đầu tái hiện race bằng
`asyncio.gather` trên hai `resolve_conversation()`, và nó **XANH TRƯỚC KHI CÓ CONSTRAINT**: hai
transaction không đan xen, mỗi cái chạy trọn trước khi cái kia bắt đầu. Một test xanh trước bản
vá thì không chứng minh gì về bản vá — nó chỉ trông như có kiểm tra, đúng loại "assertion trang
trí" mà review của spec 08 đã soi ra.

Nên bằng chứng thật là `test_interleaved_select_then_insert_is_rejected_by_db`: nó viết thẳng
thứ tự SELECT-A → SELECT-B → INSERT-A → INSERT-B, tất định, không phụ thuộc scheduler. Test
`gather` được giữ lại nhưng bị hạ xuống vai trò canh đường đi của caller, và docstring của nó
nói rõ là KHÔNG được đọc như bằng chứng race.

**Vì sao có test riêng cho `NULLS NOT DISTINCT`.** Mặc định của SQL là NULL **distinct**: hai row
`(shop, cus, chan, NULL)` được coi là KHÁC nhau, nên một `UNIQUE` thường sẽ CHO QUA cả hai — tức
constraint có mặt mà race vẫn còn nguyên. Và `thread_id=NULL` chính là ca phổ biến nhất hôm nay
(`channels/zalo` đọc `payload.get("thread_id")`, Zalo không phải lúc nào cũng gửi). Nghĩa là nếu
thiếu cờ này, constraint sẽ trông như đã vá trong khi thực tế không vá gì — đúng họ hỏng-âm-thầm
mà spec 08 và spec 04 đã dính. `test_nulls_not_distinct_is_actually_enabled` là gate cho đúng ca đó.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa

_CONSTRAINT = "uq_conversations_shop_cus_chan_thread"


def _uid(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:12]}"


# --- (a) constraint có mặt, và mang đúng cờ ------------------------------------------


@pytest.mark.asyncio
async def test_unique_constraint_exists_with_nulls_not_distinct(fresh_db) -> None:
    """(a) Hỏi thẳng `pg_constraint`, không tin `__table_args__` trong Python.

    Model khai một đằng mà DB có một nẻo là chuyện có thật (migration quên chạy, migration
    viết khác model). Nguồn sự thật ở đây là Postgres.
    """
    engine, _ = await fresh_db()
    async with engine.connect() as c:
        row = (
            await c.execute(
                sa.text("""
                -- Cờ NULLS NOT DISTINCT sống trên INDEX đỡ lưng constraint (`pg_index`),
                -- KHÔNG trên `pg_constraint` — pg_constraint không có cột nào như vậy.
                -- Bản đầu của test này hỏi nhầm bảng và chết bằng UndefinedColumn.
                select c.conname, i.indnullsnotdistinct,
                       array_agg(a.attname order by k.ord) as cols
                from pg_constraint c
                join pg_class t on t.oid = c.conrelid
                join pg_index i on i.indexrelid = c.conindid
                join unnest(c.conkey) with ordinality k(attnum, ord) on true
                join pg_attribute a on a.attrelid = t.oid and a.attnum = k.attnum
                where t.relname = 'conversations' and c.conname = :n
                group by c.conname, i.indnullsnotdistinct
                """),
                {"n": _CONSTRAINT},
            )
        ).one_or_none()

    assert row is not None, f"KHÔNG thấy constraint {_CONSTRAINT} trên bảng conversations"
    assert list(row.cols) == ["shop_id", "customer_id", "channel", "external_thread_id"]
    assert row.indnullsnotdistinct is True, (
        "constraint có mặt nhưng NULLS NOT DISTINCT chưa bật ⇒ hai row thread_id=NULL vẫn "
        "lọt qua ⇒ race KHÔNG được vá, chỉ trông như đã vá."
    )


# --- (b) cờ đó THẬT SỰ chặn, không chỉ được khai ---------------------------------------


@pytest.mark.asyncio
async def test_nulls_not_distinct_is_actually_enabled(fresh_db) -> None:
    """(b) Insert 2 row cùng `(shop, cus, chan)` với `thread_id=NULL` ⇒ cái thứ hai bị TỪ CHỐI.

    Đây là ca mà một `UNIQUE` thường sẽ cho qua. Test khẳng định về HÀNH VI của Postgres,
    không về nội dung catalog — (a) đọc cờ, (b) chứng minh cờ có tác dụng.
    """
    from db.models import Conversation, Customer

    _, session_factory = await fresh_db()
    cust = _uid("cus")

    async with session_factory() as s:
        s.add(Customer(id=cust, shop_id="shop_a", channel="zalo", external_id="u1"))
        await s.flush()
        s.add(Conversation(id=_uid("cnv"), shop_id="shop_a", customer_id=cust, channel="zalo"))
        await s.commit()

    with pytest.raises(Exception) as ei:
        async with session_factory() as s:
            s.add(Conversation(id=_uid("cnv"), shop_id="shop_a", customer_id=cust, channel="zalo"))
            await s.commit()
    assert (
        "uq_conversations_shop_cus_chan_thread" in str(ei.value).lower()
        or "unique" in str(ei.value).lower()
    ), f"kỳ vọng vi phạm unique, nhận: {ei.value!r}"


# --- (c) RACE — tái hiện TẤT ĐỊNH, không dựa vào may rủi của scheduler ------------------


@pytest.mark.asyncio
async def test_interleaved_select_then_insert_is_rejected_by_db(fresh_db) -> None:
    """(c) Tái hiện ĐÚNG chuỗi sự kiện của ISSUE-017, theo thứ tự tất định:

        session A: SELECT → (không thấy)
        session B: SELECT → (không thấy)      ← cả hai cùng kết luận "chưa có"
        session A: INSERT → OK
        session B: INSERT → phải bị Postgres TỪ CHỐI

    **Vì sao không dùng `asyncio.gather`.** Bản đầu của test này gọi hai
    `resolve_conversation()` qua `gather` và **ĐÃ XANH TRƯỚC KHI CÓ CONSTRAINT** — hai
    transaction không thật sự đan xen, mỗi cái chạy trọn trước khi cái kia bắt đầu. Một test
    xanh trước bản vá thì không chứng minh gì về bản vá; nó chỉ trông như có kiểm tra. Ở đây
    thứ tự được viết ra tường minh, nên nó ĐỎ nếu thiếu constraint và XANH khi có — tức nó
    thật sự đo cái nó khai là đang đo.

    Test này khẳng định về TẦNG DB. Nó cố ý không đi qua `resolve_conversation()`: bảo vệ
    thật nằm ở ràng buộc, và ràng buộc phải đúng kể cả khi caller viết sai.
    """
    from sqlalchemy import select

    from db.models import Conversation, Customer

    _, session_factory = await fresh_db()
    cust = _uid("cus")
    async with session_factory() as s:
        s.add(Customer(id=cust, shop_id="shop_a", channel="zalo", external_id="u_race"))
        await s.commit()

    q = (
        select(Conversation.id)
        .where(Conversation.shop_id == "shop_a")
        .where(Conversation.customer_id == cust)
        .where(Conversation.channel == "zalo")
    )

    async with session_factory() as sa_, session_factory() as sb:
        assert (await sa_.execute(q)).scalar_one_or_none() is None
        assert (await sb.execute(q)).scalar_one_or_none() is None  # B cũng thấy trống

        sa_.add(Conversation(id=_uid("cnv"), shop_id="shop_a", customer_id=cust, channel="zalo"))
        await sa_.commit()

        sb.add(Conversation(id=_uid("cnv"), shop_id="shop_a", customer_id=cust, channel="zalo"))
        with pytest.raises(Exception) as ei:
            await sb.commit()
        assert "unique" in str(ei.value).lower() or _CONSTRAINT in str(ei.value), (
            f"INSERT thứ hai LỌT QUA — đây chính là ISSUE-017. Nhận: {ei.value!r}"
        )


@pytest.mark.asyncio
async def test_upsert_shape_survives_real_blocking_race(fresh_db) -> None:
    """(c'') Đúng CƠ CHẾ mà `resolve_conversation()` dùng, dưới race THẬT có chặn.

    Test (c) chứng minh constraint từ chối plain INSERT. Nhưng code thật KHÔNG dùng plain
    INSERT — nó dùng `on_conflict_do_nothing` rồi re-select. Giữa hai điều đó có một khoảng
    trống mà review chỉ ra: khi B đụng row của A mà A CHƯA commit, B bị Postgres CHẶN; A
    commit; B bỏ qua (no-op) — lúc đó re-select của B có thấy row của A không? Nếu không,
    `scalar_one()` ném `NoResultFound` và webhook trả 500.

    Trước test này câu trả lời dựa trên ngữ nghĩa UPSERT trong tài liệu Postgres. Giờ nó
    được ĐO. Thứ tự dựng tường minh (A giữ transaction mở → B chạy nền và kẹt → A commit →
    B thoát kẹt), nên không phụ thuộc may rủi của scheduler như bản `gather`.
    """
    import uuid as _uuid

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from db.models import Conversation, Customer

    _, session_factory = await fresh_db()
    cust = _uid("cus")
    async with session_factory() as s:
        s.add(Customer(id=cust, shop_id="shop_a", channel="zalo", external_id="u_block"))
        await s.commit()

    def _upsert():
        return (
            pg_insert(Conversation)
            .values(
                id=f"cnv_{_uuid.uuid4().hex[:16]}",
                shop_id="shop_a",
                customer_id=cust,
                channel="zalo",
                external_thread_id=None,
            )
            .on_conflict_do_nothing(constraint=_CONSTRAINT)
        )

    q = (
        select(Conversation.id)
        .where(Conversation.shop_id == "shop_a")
        .where(Conversation.customer_id == cust)
        .where(Conversation.channel == "zalo")
        .where(Conversation.external_thread_id.is_(None))
    )

    async with session_factory() as sa_, session_factory() as sb:
        await sa_.execute(_upsert())  # A đã ghi, CHƯA commit

        task = asyncio.create_task(sb.execute(_upsert()))  # B sẽ kẹt trên index
        await asyncio.sleep(0.3)
        assert not task.done(), (
            "B KHÔNG bị chặn — nghĩa là không có xung đột nào xảy ra, tức constraint không "
            "bao phủ ca này. Race vẫn còn nguyên."
        )

        await sa_.commit()  # A commit ⇒ B thoát kẹt và no-op
        await task

        found = (await sb.execute(q)).scalar_one()  # ← chỗ có thể ném NoResultFound
        assert found is not None

    async with session_factory() as s:
        n = (
            await s.execute(
                sa.text("select count(*) from conversations where customer_id=:c"), {"c": cust}
            )
        ).scalar()
    assert n == 1, f"kỳ vọng đúng 1 conversation sau race có chặn, DB có {n}"


@pytest.mark.asyncio
async def test_concurrent_resolve_returns_one_conversation(fresh_db) -> None:
    """(c') Mức tích hợp: hai `resolve_conversation()` chạy qua `gather` ⇒ cùng id, 1 row.

    ⚠️ Test này YẾU hơn (c) và tôi ghi rõ vì sao: `gather` KHÔNG đảm bảo hai transaction đan
    xen, nên nó có thể xanh mà chưa từng chạm tới race. Giữ lại vì nó canh đường đi thật của
    caller (upsert + re-select trả về đúng id), nhưng **đừng đọc nó như bằng chứng race đã
    được vá** — bằng chứng đó là (c).
    """
    from channels.identity import resolve_conversation
    from db.models import Conversation  # noqa: F401  (schema phải load trước khi query)

    engine, session_factory = await fresh_db()

    async def _call() -> tuple[str, str]:
        async with session_factory() as s:
            return await resolve_conversation(
                s, shop_id="shop_a", channel="zalo", external_user_id="zalo_u_race"
            )

    (cus1, cnv1), (cus2, cnv2) = await asyncio.gather(_call(), _call())

    assert cus1 == cus2, f"ra 2 customer: {cus1} vs {cus2}"
    assert cnv1 == cnv2, f"ra 2 conversation: {cnv1} vs {cnv2}"

    async with engine.connect() as c:
        n = (
            await c.execute(sa.text("select count(*) from conversations where shop_id='shop_a'"))
        ).scalar()
    assert n == 1, f"kỳ vọng đúng 1 conversation, DB có {n}"


@pytest.mark.asyncio
async def test_repeated_calls_are_idempotent(fresh_db) -> None:
    """(c') Gọi tuần tự nhiều lần cũng phải tái dùng — hành vi cũ KHÔNG được hồi quy.

    Test này yếu hơn (c) và cố ý giữ riêng: nó canh idempotency, còn (c) canh race. Gộp hai
    thứ vào một test sẽ khiến lần sau ai đó sửa một cái mà vô tình nới cái kia.
    """
    from channels.identity import resolve_conversation

    _, session_factory = await fresh_db()
    seen = set()
    for _ in range(3):
        async with session_factory() as s:
            seen.add(
                await resolve_conversation(
                    s, shop_id="shop_a", channel="zalo", external_user_id="zalo_u_seq"
                )
            )
    assert len(seen) == 1, f"gọi 3 lần ra {len(seen)} cặp id khác nhau: {seen}"


# --- (d) thread khác nhau vẫn tách được ------------------------------------------------


@pytest.mark.asyncio
async def test_distinct_thread_ids_create_distinct_conversations(fresh_db) -> None:
    """(d) `external_thread_id` khác nhau ⇒ 2 conversation. Đây là điều phương án B giữ lại.

    Phương án A (`UNIQUE (shop_id, customer_id, channel)`) sẽ làm test này ĐỎ — và đó là lý
    do Wyatt chọn B: nếu Zalo thật sự có nhiều thread (PRE-004 chưa trả lời), A gộp nhầm hai
    mạch hội thoại, và dữ liệu đã gộp thì không tách lại được.
    """
    from channels.identity import resolve_conversation

    _, session_factory = await fresh_db()

    async with session_factory() as s:
        _, cnv_a = await resolve_conversation(
            s,
            shop_id="shop_a",
            channel="zalo",
            external_user_id="zalo_u_multi",
            external_thread_id="thread_A",
        )
    async with session_factory() as s:
        _, cnv_b = await resolve_conversation(
            s,
            shop_id="shop_a",
            channel="zalo",
            external_user_id="zalo_u_multi",
            external_thread_id="thread_B",
        )

    assert cnv_a != cnv_b, (
        "hai thread khác nhau bị gộp vào một conversation — constraint đang là phương án A, "
        "không phải B (spec 09 §14)"
    )


@pytest.mark.asyncio
async def test_cross_shop_same_customer_stays_separate(fresh_db) -> None:
    """(d') Constraint mới KHÔNG được nới lỏng ranh giới tenant.

    Cùng một người nhắn 2 shop ⇒ 2 customer, 2 conversation. Đây là bất biến cốt lõi
    (CLAUDE.md §3); thêm constraint là lúc dễ vô tình đụng vào nó nhất.
    """
    from channels.identity import resolve_conversation

    _, session_factory = await fresh_db()
    out = []
    for shop in ("shop_a", "shop_b"):
        async with session_factory() as s:
            out.append(
                await resolve_conversation(
                    s, shop_id=shop, channel="zalo", external_user_id="zalo_same_human"
                )
            )
    (cus_a, cnv_a), (cus_b, cnv_b) = out
    assert cus_a != cus_b, "cùng người nhắn 2 shop phải ra 2 customer — rò tenant"
    assert cnv_a != cnv_b, "cùng người nhắn 2 shop phải ra 2 conversation — rò tenant"
