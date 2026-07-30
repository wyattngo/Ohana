"""S0 gate (spec `11-Task-OhanaAISeller-ShopsPersona.md` §7 Phase S0).

Viết TRƯỚC `Shop`/`ShopProfile`/`ShopKnowledge`/`ShopProfileRepo` ⇒ expected RED.

**Vì sao spec này tồn tại.** `shop_id` là `Text` trần ở mọi bảng và không FK về đâu — nghĩa
là một JWT hợp lệ mang `shop_id` là chuỗi BẤT KỲ và mọi tầng dưới đều tin. Composite FK của
spec 06/10 chặn được row shop A trỏ row shop B, nhưng KHÔNG chặn được một shop chưa từng tồn
tại, vì chưa có bảng cha nào để tham chiếu. S0 dựng bảng cha đó.

**Ba bất biến các test dưới đây bảo vệ, và vì sao mỗi cái đáng một test riêng:**

1. `knowledge` JSONB validate lúc **GHI**, không phải lúc ĐỌC. Validate lúc đọc là hoãn lỗi
   tới thời điểm đắt nhất: `lookup_size` nổ ở production, trên dữ liệu một shop thật, giữa
   cuộc trò chuyện với khách. Chặn ở đường ghi thì người sai là người sửa, ngay lúc sai.
2. Cap persona ở **cả hai tầng** — Pydantic cho thông báo lỗi tử tế, CHECK constraint cho
   thứ không ai bypass được. Pydantic bị bỏ qua bởi raw SQL; CHECK thì không.
3. Đọc cross-shop trả **None**, không raise. Raise phân biệt được "không tồn tại" với "tồn
   tại nhưng của shop khác" — tức rò rỉ chính sự TỒN TẠI của dữ liệu shop khác. Cùng hình
   dạng với `PendingReplyRepo.get`.

**Giới hạn đã biết:** `fresh_db` dựng schema bằng `Base.metadata.create_all`, KHÔNG qua
Alembic (xem `conftest.py`). Mọi assertion ở đây kiểm **model**, không kiểm **migration**.
Migration `0007` có gate riêng và phải chạy thật trên Postgres:
`alembic upgrade head && alembic downgrade -1 && alembic upgrade head`.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from agent.persona import PERSONA_MAX_CHARS
from db.models import ShopKnowledge
from db.repos import ShopProfileRepo


def _uid(p: str) -> str:
    return f"{p}_{uuid.uuid4().hex[:12]}"


async def _seed_shop(conn: sa.ext.asyncio.AsyncConnection, shop_id: str) -> str:
    await conn.execute(
        sa.text("insert into shops (id, name, status) values (:i, :n, 'active')"),
        {"i": shop_id, "n": f"Shop {shop_id}"},
    )
    return shop_id


# --- (a) bảng cha tồn tại, và FK thật sự bắt buộc ------------------------------------


@pytest.mark.asyncio
async def test_profile_requires_existing_shop(fresh_db) -> None:
    """(a) Profile trỏ shop KHÔNG tồn tại ⇒ Postgres TỪ CHỐI.

    Đây là điều spec 11 sinh ra để làm: trước nó, `shop_id` không có bảng cha nên không có
    cách nào phân biệt một shop thật với một chuỗi ai đó tự khai.
    """
    engine, _ = await fresh_db()
    with pytest.raises(IntegrityError):
        async with engine.begin() as c:
            await c.execute(
                sa.text(
                    "insert into shop_profile (shop_id, persona_md, knowledge) "
                    "values (:s, 'xin chào', '{}'::jsonb)"
                ),
                {"s": _uid("shop_khong_ton_tai")},
            )


@pytest.mark.asyncio
async def test_profile_accepted_for_existing_shop(fresh_db) -> None:
    """(a-đối chứng) Shop có thật thì PHẢI ghi được.

    Thiếu test này thì một FK viết sai tới mức chặn cả ghi hợp lệ vẫn làm test trên xanh —
    "từ chối mọi thứ" cũng thoả `pytest.raises`.
    """
    engine, _ = await fresh_db()
    shop = _uid("shop")
    async with engine.begin() as c:
        await _seed_shop(c, shop)
        await c.execute(
            sa.text(
                "insert into shop_profile (shop_id, persona_md, knowledge) "
                "values (:s, 'Shop thời trang nữ, giọng thân thiện', '{}'::jsonb)"
            ),
            {"s": shop},
        )
    async with engine.connect() as c:
        n = (
            await c.execute(
                sa.text("select count(*) from shop_profile where shop_id = :s"), {"s": shop}
            )
        ).scalar_one()
    assert n == 1


@pytest.mark.asyncio
async def test_one_profile_per_shop(fresh_db) -> None:
    """(a-bis) `shop_id` là PK của `shop_profile` ⇒ đúng MỘT profile mỗi shop.

    Nếu cần versioning về sau thì đó là bảng khác, KHÔNG phải nới PK này — hai profile
    "đang hoạt động" cho một shop nghĩa là không ai biết AI đang nói bằng giọng nào.
    """
    engine, _ = await fresh_db()
    shop = _uid("shop")
    async with engine.begin() as c:
        await _seed_shop(c, shop)
        await c.execute(
            sa.text(
                "insert into shop_profile (shop_id, persona_md, knowledge) "
                "values (:s, 'bản 1', '{}'::jsonb)"
            ),
            {"s": shop},
        )
    with pytest.raises(IntegrityError):
        async with engine.begin() as c:
            await c.execute(
                sa.text(
                    "insert into shop_profile (shop_id, persona_md, knowledge) "
                    "values (:s, 'bản 2', '{}'::jsonb)"
                ),
                {"s": shop},
            )


# --- (b) knowledge JSONB validate lúc GHI --------------------------------------------


def test_knowledge_rejects_unknown_field() -> None:
    """(b) Field lạ bị TỪ CHỐI, không bị bỏ qua im lặng.

    `extra="forbid"` chứ không phải mặc định: nếu seller gõ nhầm `size_charts` (thừa `s`)
    mà model lặng lẽ bỏ qua, họ sẽ thấy "lưu thành công" rồi `lookup_size` trả `not_found`
    mãi mãi, và không có gì trên màn hình giải thích vì sao.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ShopKnowledge.model_validate({"size_charts": []})


def test_knowledge_rejects_wrong_shape() -> None:
    """(b) Shape sai bị chặn — đây là loại lỗi chỉ lộ ở production nếu không chặn lúc ghi."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ShopKnowledge.model_validate({"size_chart": [{"size": "M", "height_min_cm": "cao"}]})


@pytest.mark.asyncio
async def test_repo_rejects_invalid_knowledge_at_write(fresh_db) -> None:
    """(b) Chặn ở tầng REPO, không phải tầng API.

    Đặt validate ở repo nghĩa là MỌI đường ghi đều đi qua nó — API, script seed, test,
    migration data-fix. Đặt ở API thì mọi đường khác đều là lỗ.
    """
    from pydantic import ValidationError

    engine, sf = await fresh_db()
    shop = _uid("shop")
    async with engine.begin() as c:
        await _seed_shop(c, shop)

    async with sf() as s:
        repo = ShopProfileRepo(s, shop_scope=shop)
        with pytest.raises((ValidationError, ValueError)):
            await repo.upsert(persona_md="ok", knowledge={"size_chart": [{"size": 123}]})


# --- (c) cap persona: hai tầng, vì chúng hỏng khác nhau ------------------------------


@pytest.mark.asyncio
async def test_persona_over_cap_rejected_by_repo(fresh_db) -> None:
    """(c) Tầng 1 — repo từ chối, thông báo lỗi đọc được."""
    engine, sf = await fresh_db()
    shop = _uid("shop")
    async with engine.begin() as c:
        await _seed_shop(c, shop)

    async with sf() as s:
        repo = ShopProfileRepo(s, shop_scope=shop)
        with pytest.raises(ValueError):
            await repo.upsert(persona_md="x" * (PERSONA_MAX_CHARS + 1), knowledge={})


@pytest.mark.asyncio
async def test_persona_over_cap_rejected_by_database(fresh_db) -> None:
    """(c) Tầng 2 — CHECK constraint, thứ raw SQL không lách được.

    Đây KHÔNG phải test thừa của cái trên. Chúng bảo vệ hai đường khác nhau: Pydantic/repo
    bảo vệ đường ứng dụng, CHECK bảo vệ mọi đường còn lại (psql tay, script, data-fix).
    Ngân sách token là ràng buộc chung của hệ thống — nó không nên phụ thuộc vào việc
    người ghi có nhớ dùng repo hay không.
    """
    engine, _ = await fresh_db()
    shop = _uid("shop")
    async with engine.begin() as c:
        await _seed_shop(c, shop)

    with pytest.raises(IntegrityError):
        async with engine.begin() as c:
            await c.execute(
                sa.text(
                    "insert into shop_profile (shop_id, persona_md, knowledge) "
                    "values (:s, :p, '{}'::jsonb)"
                ),
                {"s": shop, "p": "x" * (PERSONA_MAX_CHARS + 1)},
            )


# --- (d) đọc cross-shop trả None, KHÔNG raise ----------------------------------------


@pytest.mark.asyncio
async def test_cross_shop_profile_read_returns_none(fresh_db) -> None:
    """(d) Shop A đọc profile shop B ⇒ **None**, không raise.

    Raise sẽ phân biệt được "không tồn tại" với "tồn tại nhưng của shop khác" — và chính
    sự phân biệt đó rò rỉ thông tin về shop khác. Cùng hình dạng `PendingReplyRepo.get`.
    """
    engine, sf = await fresh_db()
    shop_a, shop_b = _uid("shopA"), _uid("shopB")
    async with engine.begin() as c:
        await _seed_shop(c, shop_a)
        await _seed_shop(c, shop_b)

    async with sf() as s:
        await ShopProfileRepo(s, shop_scope=shop_b).upsert(
            persona_md="giọng của shop B", knowledge={}
        )

    async with sf() as s:
        assert await ShopProfileRepo(s, shop_scope=shop_a).get() is None
        got = await ShopProfileRepo(s, shop_scope=shop_b).get()
        assert got is not None and got.persona_md == "giọng của shop B"


@pytest.mark.asyncio
async def test_upsert_cannot_write_to_another_shop(fresh_db) -> None:
    """(d-bis) `shop_id` BAKED từ `shop_scope` — repo không nhận nó làm tham số.

    Không có tham số để bẻ thì caller sai cũng không ghi lệch shop được. Test đọc CHỮ KÝ
    hàm chứ không tin docstring: một `shop_id=` lọt vào signature là hồi quy thật.
    """
    import inspect

    params = inspect.signature(ShopProfileRepo.upsert).parameters
    assert "shop_id" not in params, (
        f"`upsert` KHÔNG được nhận shop_id — nó phải baked từ shop_scope. Params: {list(params)}"
    )


@pytest.mark.asyncio
async def test_published_at_defaults_null(fresh_db) -> None:
    """(e) `published_at` mặc định NULL = chưa phát hành (PRE-1102, Wyatt ký).

    Cố ý KHÔNG có `profile_status`/`approved_by`: không có người duyệt thứ hai nào tồn tại,
    nên một cột tên "approved" sẽ mô tả một quy trình không có thật — và về sau sẽ có người
    đọc nó như bằng chứng đã qua kiểm duyệt.
    """
    engine, sf = await fresh_db()
    shop = _uid("shop")
    async with engine.begin() as c:
        await _seed_shop(c, shop)
    async with sf() as s:
        await ShopProfileRepo(s, shop_scope=shop).upsert(persona_md="draft", knowledge={})
        got = await ShopProfileRepo(s, shop_scope=shop).get()
    assert got is not None and got.published_at is None


# =====================================================================================
# S1 — onboard shop thật → JWT mang `shop_id` ĐÃ ĐỐI CHIẾU `shops`.
# Viết TRƯỚC endpoint + đối chiếu DB ⇒ expected RED. RISK:high (Wyatt ký 2026-07-20).
#
# **Lỗ đang đóng.** S0 dựng bảng cha, nhưng `auth/identity.py` vẫn tin `shop_id` chỉ vì
# chữ ký JWT hợp lệ. Nghĩa là một token ký đúng mang `shop_id` là chuỗi BẤT KỲ vẫn đi
# thẳng vào mọi tầng dưới — bảng `shops` có tồn tại cũng không ai hỏi tới nó.
#
# **Hai quyết định Wyatt chốt trong phiên (2026-07-20), test đóng băng cả hai:**
#   - `status != 'active'` ⇒ TỪ CHỐI (fail-closed). Suspend một shop phải cắt được truy cập
#     NGAY, không chờ token hết hạn 24h. Nếu chỉ kiểm tồn tại thì `status` là cột trang trí —
#     loại dễ bị đọc nhầm là có tác dụng.
#   - CHƯA cache. Mỗi request thêm một PK lookup. Cache sai key là cross-tenant leak KHÔNG đi
#     qua SQL nên FK không cứu được (R2, §4). Chưa có shop thật để đo tải ⇒ đo trước, cache sau.
# =====================================================================================

import jwt as _pyjwt  # noqa: E402
import pytest as _pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from auth.identity import SESSION_COOKIE_NAME, get_jwt_secret  # noqa: E402


@_pytest.fixture
def dev_client(monkeypatch: _pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("OHANA_ENV", "dev")
    from app.main import app

    return TestClient(app)


def _mint(shop_id: str, *, role: str = "seller") -> str:
    """Token ký ĐÚNG, chỉ khác `shop_id`. Đây là mô hình mối đe doạ thật của phase này:
    không phải chữ ký giả, mà là một `shop_id` chưa từng tồn tại (hoặc đã bị treo) đi kèm
    chữ ký hợp lệ — token dev fixture lọt sang prod là ca cụ thể nhất."""
    return _pyjwt.encode(
        {"sub": "u_test", "shop_id": shop_id, "role": role},
        get_jwt_secret(),
        algorithm="HS256",
    )


async def _seed_shop_row(shop_id: str, *, status: str) -> None:
    from sqlalchemy import text as _text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from db.session import make_engine

    engine = make_engine()
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            await s.execute(
                _text(
                    "insert into shops (id, name, status) values (:i, :n, :st) "
                    "on conflict (id) do update set status = excluded.status"
                ),
                {"i": shop_id, "n": f"Shop {shop_id}", "st": status},
            )
            await s.commit()
    finally:
        await engine.dispose()


# --- onboard endpoint ----------------------------------------------------------------


def test_onboard_requires_admin(dev_client: TestClient) -> None:
    """Seller cookie hợp lệ vẫn KHÔNG tạo được shop — 403.

    `POST /admin/shops` là đường SINH RA tenant. Nếu seller gọi được, một tenant tự tạo
    được tenant khác và toàn bộ mô hình cách ly mất nghĩa ngay từ gốc.
    """
    resp = dev_client.post("/api/mock/authorize?role=seller")
    assert resp.status_code == 200
    csrf = dev_client.cookies.get("ohana_csrf")
    r = dev_client.post(
        "/api/admin/shops",
        json={"name": "Shop lén"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert r.status_code == 403, f"seller tạo được shop — {r.status_code} {r.text}"


def test_onboard_creates_real_shop(dev_client: TestClient) -> None:
    """Admin onboard ⇒ row `shops` THẬT, `id` do server sinh.

    `id` KHÔNG được nhận từ client: cho client chọn `shop_id` nghĩa là cho họ chọn danh
    tính tenant, tức mở lại đúng lỗ mà phase này đóng.
    """
    resp = dev_client.post("/api/mock/authorize?role=admin")
    assert resp.status_code == 200
    csrf = dev_client.cookies.get("ohana_csrf")
    r = dev_client.post(
        "/api/admin/shops",
        json={"name": "Shop Áo Thun 24h"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert r.status_code in (200, 201), r.text
    body = r.json()
    assert body.get("shop_id"), f"onboard phải trả shop_id, nhận {body}"
    assert body["shop_id"] != "Shop Áo Thun 24h"


def test_onboard_ignores_client_supplied_id(dev_client: TestClient) -> None:
    """Client khai `id`/`shop_id` trong body ⇒ BỊ BỎ QUA, không được dùng.

    Cùng bất biến với `ChatIn(extra="ignore")` của spec 07: thân request không bao giờ
    quyết định tenancy.
    """
    dev_client.post("/api/mock/authorize?role=admin")
    csrf = dev_client.cookies.get("ohana_csrf")
    r = dev_client.post(
        "/api/admin/shops",
        json={"name": "Shop X", "id": "shop_tu_khai", "shop_id": "shop_tu_khai"},
        headers={"X-CSRF-Token": csrf or ""},
    )
    assert r.status_code in (200, 201, 422), r.text
    if r.status_code != 422:
        assert r.json()["shop_id"] != "shop_tu_khai", "client tự chọn được shop_id — lỗ tenancy"


# --- đối chiếu shops ở đường VERIFY ---------------------------------------------------


def test_jwt_with_unknown_shop_is_rejected(dev_client: TestClient) -> None:
    """Token ký ĐÚNG nhưng `shop_id` không có trong `shops` ⇒ 401.

    Đây là mệnh đề trung tâm của S1. Trước phase này, token như vậy đi lọt hoàn toàn.
    """
    dev_client.cookies.set(SESSION_COOKIE_NAME, _mint("shop_khong_bao_gio_ton_tai"))
    r = dev_client.get("/api/inbox")
    assert r.status_code == 401, f"shop không tồn tại vẫn qua — {r.status_code} {r.text}"


@_pytest.mark.asyncio
async def test_jwt_with_inactive_shop_is_rejected(dev_client: TestClient) -> None:
    """Shop TỒN TẠI nhưng `status='suspended'` ⇒ 401 (Wyatt ký fail-closed).

    Không có test này thì `status` là cột trang trí: suspend một shop sẽ không có hiệu lực
    nào cho tới khi token hết hạn 24h, trong khi người bấm nút suspend tin rằng nó đã cắt.
    """
    await _seed_shop_row("shop_bi_treo", status="suspended")
    dev_client.cookies.set(SESSION_COOKIE_NAME, _mint("shop_bi_treo"))
    r = dev_client.get("/api/inbox")
    assert r.status_code == 401, f"shop suspended vẫn qua — {r.status_code} {r.text}"


@_pytest.mark.asyncio
async def test_jwt_with_active_shop_passes(dev_client: TestClient) -> None:
    """(đối chứng) Shop active thì PHẢI qua.

    Thiếu test này thì một implementation từ chối MỌI thứ vẫn làm hai test trên xanh —
    và "khoá sạch" cũng là một cách hỏng.
    """
    await _seed_shop_row("shop_dang_hoat_dong", status="active")
    dev_client.cookies.set(SESSION_COOKIE_NAME, _mint("shop_dang_hoat_dong"))
    r = dev_client.get("/api/inbox")
    assert r.status_code == 200, f"shop active bị chặn — {r.status_code} {r.text}"


def test_dev_fixture_shop_exists_so_mock_flow_still_works(dev_client: TestClient) -> None:
    """`OHANA_ENV=dev` ⇒ fixture shop của `mock_auth` phải TỒN TẠI trong `shops`.

    Nếu không seed, S1 sẽ làm vỡ toàn bộ luồng dev một cách im lặng: `mock/authorize` vẫn
    trả 200 (nó chỉ mint token), rồi mọi route sau đó 401 vì `fixture-shop-001` không có
    trong bảng. Đó là kiểu hỏng tệ nhất — tầng mint nói OK, tầng dùng nói không.
    """
    resp = dev_client.post("/api/mock/authorize?role=seller")
    assert resp.status_code == 200
    r = dev_client.get("/api/inbox")
    assert r.status_code == 200, (
        f"luồng dev vỡ sau S1 — fixture shop chưa được seed vào `shops`? {r.status_code} {r.text}"
    )


# --- cả BA cửa, không chỉ /api/inbox -------------------------------------------------
#
# Wire dependency mới là việc thủ công ở `app/main.py`: sót MỘT call site nghĩa là còn một
# cửa không kiểm shop, và nó KHÔNG đỏ test nào nếu test chỉ probe cửa khác. Cửa dễ sót nhất
# lại là `admin` — nó có dependency RIÊNG (`build_admin_dep`), nên đổi `_identity_dep` mà
# quên `_admin_dep` sẽ để hở đúng cửa quyền cao nhất.


@_pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("get", "/api/inbox", None),
        ("post", "/api/chat", {"message": "xin chào"}),
        ("post", "/api/admin/wiki/ingest", {"text": "x", "source_ref": "r"}),
    ],
)
def test_every_door_rejects_unknown_shop(
    dev_client: TestClient, method: str, path: str, body: dict[str, str] | None
) -> None:
    """Token ký đúng + `shop_id` không tồn tại ⇒ 401 ở MỌI cửa.

    **Phải vượt CSRF TRƯỚC, nếu không test xanh vì lý do sai.** Bản đầu của test này không
    gửi `X-CSRF-Token`, nên hai route POST bị middleware chặn ở 403 `csrf_check_failed`
    trước khi chạm tầng kiểm shop — tức nó đo CSRF chứ không đo cái nó tuyên bố. Một biến
    thể "chấp nhận cả 401 lẫn 403" sẽ càng tệ: nó XANH kể cả khi tầng kiểm shop bị gỡ sạch.

    Nên: authorize để lấy CSRF hợp lệ, rồi ĐÈ cookie phiên bằng token giả mạo. Giờ request
    đi qua được CSRF và dừng đúng ở tầng ta muốn đo.
    """
    # 1. Lấy CSRF thật (đồng thời set cookie phiên hợp lệ — sẽ bị đè ở bước 2).
    assert dev_client.post("/api/mock/authorize?role=admin").status_code == 200
    csrf = dev_client.cookies.get("ohana_csrf")
    assert csrf

    # 2. Đè phiên bằng token ký ĐÚNG nhưng trỏ shop không tồn tại. `role=admin` để cửa admin
    #    không bị 403 vì role — ta muốn nó dừng ở tầng shop, không ở tầng role.
    dev_client.cookies.set(SESSION_COOKIE_NAME, _mint("shop_khong_ton_tai", role="admin"))

    resp = dev_client.request(method, path, json=body, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 401, (
        f"{method.upper()} {path} KHÔNG kiểm shop — {resp.status_code} {resp.text}"
    )


# =====================================================================================
# S2 — hai hàm tra cứu TẤT ĐỊNH + ráp persona vào prompt. Viết TRƯỚC `tools/shop_kb.py`
# ⇒ expected RED.
#
# **Vì sao size chart KHÔNG đi RAG** (D9, đã ký): (a) `parsing/chunk.py` cắt ở 800 ký tự sẽ
# cắt GIỮA bảng, chunk mất cột; (b) cosine similarity giữa "1m6 50kg" và một chunk toàn số
# gần như vô nghĩa; (c) quan trọng nhất — **RAG không bao giờ nói "không biết"**, nó luôn trả
# k chunk gần nhất. Hàm tất định trả `not_found` dứt khoát, và chính tín hiệu đó là thứ
# confidence-gated escalation cần. Vì vậy `not_found` ở đây là YÊU CẦU an toàn, không phải
# chi tiết API.
# =====================================================================================

from agent.persona import build_persona_prompt  # noqa: E402


def _knowledge() -> dict[str, object]:
    return {
        "size_chart": [
            {
                "size": "S",
                "height_min_cm": 145,
                "height_max_cm": 154,
                "weight_min_kg": 35,
                "weight_max_kg": 44,
            },
            {
                "size": "M",
                "height_min_cm": 155,
                "height_max_cm": 165,
                "weight_min_kg": 45,
                "weight_max_kg": 55,
            },
            {
                "size": "L",
                "height_min_cm": 166,
                "height_max_cm": 175,
                "weight_min_kg": 56,
                "weight_max_kg": 68,
            },
        ],
        "shipping_zones": [
            {"zone": "Q7", "fee_vnd": 25000, "eta_days": 2},
            {"zone": "Hà Nội", "fee_vnd": 35000, "eta_days": 4},
        ],
    }


async def _shop_with_knowledge(engine, sf, shop: str) -> str:
    async with engine.begin() as c:
        await _seed_shop(c, shop)
    async with sf() as s:
        await ShopProfileRepo(s, shop_scope=shop).upsert(
            persona_md="Shop áo thun nữ, xưng 'shop'.", knowledge=_knowledge()
        )
    return shop


@pytest.mark.asyncio
async def test_lookup_size_exact_case_from_spec(fresh_db) -> None:
    """(a) `lookup_size(160, 50) == "M"` — assert TẤT ĐỊNH, không cần LLM-as-judge.

    Đây là acceptance viết nguyên văn trong L1 cho `GD0-SHOPS`. Một hàm thuần thì kiểm được
    bằng đúng một dòng như thế; một đường RAG thì phải chấm bằng model, và chấm bằng model
    nghĩa là gate của bạn có phương sai.
    """
    from tools.shop_kb import lookup_size

    engine, sf = await fresh_db()
    shop = await _shop_with_knowledge(engine, sf, _uid("shop"))

    async with sf() as s:
        out = await lookup_size(s, shop_id=shop, height_cm=160, weight_kg=50)
    assert out["result"] == "M", out


@pytest.mark.asyncio
async def test_lookup_size_out_of_range_is_explicit_not_found(fresh_db) -> None:
    """(b) Ngoài bảng ⇒ `not_found` TƯỜNG MINH, và `success` vẫn True.

    Thiếu data KHÔNG phải lỗi hệ thống — nó là một câu trả lời hợp lệ ("shop chưa khai").
    Trả `success: False` sẽ trộn nó với lỗi hạ tầng, và tầng escalation sẽ không phân biệt
    được "chưa biết" với "hỏng".
    """
    from tools.shop_kb import lookup_size

    engine, sf = await fresh_db()
    shop = await _shop_with_knowledge(engine, sf, _uid("shop"))

    async with sf() as s:
        out = await lookup_size(s, shop_id=shop, height_cm=200, weight_kg=120)
    assert out["result"] == "not_found", out
    assert out["success"] is True, "thiếu data không phải lỗi hệ thống"


@pytest.mark.asyncio
async def test_lookup_on_shop_without_knowledge_does_not_explode(fresh_db) -> None:
    """(c) Shop chưa khai gì ⇒ `not_found`, KHÔNG nổ.

    Mọi shop mới onboard đều ở trạng thái này. Nếu nó raise thì shop đầu tiên nhắn tin sau
    khi onboard sẽ gặp lỗi 500 — ca phổ biến nhất, không phải ca biên.
    """
    from tools.shop_kb import lookup_shipping, lookup_size

    engine, sf = await fresh_db()
    shop = _uid("shop_trong")
    async with engine.begin() as c:
        await _seed_shop(c, shop)

    async with sf() as s:
        assert (await lookup_size(s, shop_id=shop, height_cm=160, weight_kg=50))[
            "result"
        ] == "not_found"
        assert (await lookup_shipping(s, shop_id=shop, zone="Q7"))["result"] == "not_found"


@pytest.mark.asyncio
async def test_lookup_shipping_returns_fee_and_eta(fresh_db) -> None:
    """(a-bis) `lookup_shipping` trả phí + thời gian giao, cùng hình dạng tất định."""
    from tools.shop_kb import lookup_shipping

    engine, sf = await fresh_db()
    shop = await _shop_with_knowledge(engine, sf, _uid("shop"))

    async with sf() as s:
        out = await lookup_shipping(s, shop_id=shop, zone="Q7")
    assert out["result"] == "found", out
    assert out["fee_vnd"] == 25000 and out["eta_days"] == 2, out


@pytest.mark.asyncio
async def test_lookup_is_scoped_to_shop(fresh_db) -> None:
    """(e) Tool của shop A KHÔNG đọc được knowledge shop B.

    `shop_id` tới từ tham số HÀM (do orchestrator truyền từ Identity đã verify), không từ
    `parameters` mà LLM điền — xem test (d).
    """
    from tools.shop_kb import lookup_size

    engine, sf = await fresh_db()
    shop_a, shop_b = _uid("shopA"), _uid("shopB")
    async with engine.begin() as c:
        await _seed_shop(c, shop_a)
    await _shop_with_knowledge(engine, sf, shop_b)

    async with sf() as s:
        out = await lookup_size(s, shop_id=shop_a, height_cm=160, weight_kg=50)
    assert out["result"] == "not_found", f"shop A đọc được bảng size của shop B: {out}"


def test_shop_id_is_not_an_llm_parameter() -> None:
    """(d) `shop_id` KHÔNG xuất hiện trong `Tool.parameters` của cả hai tool.

    Đọc thẳng `Tool.parameters`, không tin docstring. Đây là bất biến của `tools/registry`:
    handler nhận `(user_id, shop_id, args)` từ orchestrator; nếu `shop_id` lọt vào schema mà
    LLM điền được thì model có thể chĩa tool sang shop khác — và nó sẽ làm vậy một cách vô
    tình khi khách nhắc tên shop khác trong câu hỏi.
    """
    from tools.shop_kb import build_shipping_tool, build_size_tool

    for tool in (build_size_tool(None), build_shipping_tool(None)):  # type: ignore[arg-type]
        props = tool.parameters.get("properties", {})
        assert "shop_id" not in props, f"{tool.name}: shop_id lọt vào parameters — {props}"
        assert tool.parameters.get("additionalProperties") is False, (
            f"{tool.name}: thiếu additionalProperties=False ⇒ LLM nhét được field lạ"
        )


# --- build_persona_prompt -------------------------------------------------------------


def test_persona_prompt_respects_cap() -> None:
    """(f) Persona vượt cap ⇒ raise, KHÔNG cắt âm thầm.

    Cắt lúc build sẽ làm mất phần nội dung seller tưởng đã lưu, và không ai thấy. Chặn ở
    tầng ghi (S0) là đúng chỗ; ở đây chỉ là chốt chặn cuối.
    """
    with pytest.raises(ValueError):
        build_persona_prompt("x" * (PERSONA_MAX_CHARS + 1), shop_display_name="Shop")


def test_persona_prompt_template_never_names_the_platform() -> None:
    """(g) Phần TEMPLATE của ta không bao giờ nhắc "Ohana".

    ⚠️ Đây KHÔNG phải "prompt không bao giờ chứa chữ Ohana": nếu shop tên là "Ohana Boutique"
    thì tên đó PHẢI vào prompt — kiểm duyệt chữ của chính shop là sai. Bất biến đúng là: văn
    bản DO TA sinh ra không mang tên nền tảng sang đường nói với khách.

    Và kể cả thế, đây vẫn chỉ là phần tất định. AI có tuân chỉ dẫn hay không là câu hỏi hành
    vi, đo bằng `-m live` + eval trên output THẬT (`GD0-DRAFTER`). Đừng đọc test này rộng hơn.
    """
    out = build_persona_prompt("Shop bán áo thun.", shop_display_name="Shop Áo Thun")
    assert "Ohana" not in out, f"template của ta nhắc tên nền tảng: {out}"

    # Đối chứng: tên shop có chữ Ohana thì PHẢI được giữ nguyên.
    out2 = build_persona_prompt("Shop bán áo.", shop_display_name="Ohana Boutique")
    assert "Ohana Boutique" in out2, "tên riêng của shop bị kiểm duyệt — sai"
