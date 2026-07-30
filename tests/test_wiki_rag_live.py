"""ISSUE-016 live acceptance — spec 08 Phase E2 (viết lại từ bản spec 05 P1).

Đây là test DUY NHẤT trong repo chứng minh F1 wiki-RAG chạy end-to-end với embedding THẬT.
`tests/test_embedder_wiring.py` (chạy trong mọi lần gate) chỉ chứng minh factory chọn đúng
class và adapter gọi đúng shape với client giả — nó KHÔNG bao giờ gọi mạng, nên KHÔNG chứng
minh được chất lượng retrieval. Lẫn hai thứ đó chính là sai lầm gốc của ISSUE-016: F1 từng
ship "DONE" trong khi gate chỉ có `FakeEmbedder`. Đừng làm mờ ranh giới này — ngày nào gate
tất định bắt đầu khẳng định về *chất lượng* retrieval là ngày lặp lại đúng sai lầm đó.

**ĐỔI BẢN CHẤT 2026-07-19 (ADR PRE-007 + spec 08):** provider chuyển OpenAI → Together e5.
Bản cũ dùng `OpenAIEmbedder` (1536); cột DB giờ là `Vector(1024)` nên bản cũ KHÔNG chạy được
nữa — nó sẽ bị Postgres từ chối vì sai chiều. Mọi kết quả live cũ trên OpenAI KHÔNG áp dụng.

**Hai thay đổi về CHẤT so với bản cũ, không chỉ đổi tên class:**

1. **Assert THỨ HẠNG, không phải "có mặt".** Bản cũ assert `any("return" in h.chunk ...)` —
   tức chỉ cần chunk đúng nằm đâu đó trong kết quả là xanh, kể cả khi nó xếp CUỐI dưới ba
   chunk lạc đề. Đó không phải điều F1 hứa với seller. F1 hứa: hỏi về đổi trả thì đoạn nói về
   đổi trả phải lên ĐẦU. Test này assert `hits[0]`, và assert chunk lạc đề xếp DƯỚI.

2. **Tiếng Việt.** e5 là model multilingual và sản phẩm phục vụ seller Việt. Test bằng tiếng
   Anh sẽ đo một thứ khác với thứ đang bán. Nội dung dưới đây là văn bán hàng thật, không
   phải lorem ipsum.

Skip SẠCH khi thiếu key — không FAIL giả. Test này không chạy trong CI (không có key) và
không chạy trong gate mặc định (`addopts = -m 'not live'`). Chạy tay:

    TOGETHER_API_KEY=... .venv/bin/python -m pytest tests/test_wiki_rag_live.py -m live -x -q

PASS ở đây là thứ đóng ISSUE-016. Gate tất định xanh KHÔNG đóng được nó.
"""

from __future__ import annotations

import os

import pytest

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg://ohana:ohana@localhost:5432/ohana"
)

# Văn bản wiki mẫu: BA chủ đề tách bạch. Cần ít nhất hai chủ đề để đo được thứ hạng — một
# chủ đề thì "chunk đúng xếp đầu" là điều hiển nhiên, không chứng minh gì.
WIKI_TEXT = """\
Chính sách đổi trả: khách được đổi hoặc trả hàng trong vòng 7 ngày kể từ ngày nhận,
với điều kiện sản phẩm còn nguyên tem mác và bao bì chưa bị rách.

Phí vận chuyển: nội thành 25.000đ, ngoại thành 35.000đ. Đơn từ 500.000đ trở lên
được miễn phí giao hàng toàn quốc.

Giờ mở cửa: cửa hàng hoạt động từ 8h đến 21h các ngày trong tuần, riêng chủ nhật
đóng cửa lúc 18h. Nghỉ Tết từ 28 tháng Chạp đến mùng 5.
"""


def _require_key() -> str:
    key = os.environ.get("TOGETHER_API_KEY", "").strip()
    if not key:
        pytest.skip("TOGETHER_API_KEY chưa set — live test skip sạch, KHÔNG fail giả")
    return key


# Ba truy vấn XOAY VÒNG, mỗi cái phải kéo một chunk KHÁC lên đầu. Một truy vấn chứng minh ít
# hơn nhiều: nó có thể xanh vì một chunk tình cờ "hút" mọi câu hỏi (ca hỏng thật của retrieval
# kém — luôn trả cùng một đoạn bất kể hỏi gì), và test một câu không phân biệt được ca đó với
# retrieval tốt. Ba câu, ba đáp án khác nhau, thì không tình cờ được.
_RANKING_CASES = [
    ("phí ship nội thành bao nhiêu tiền", ("vận chuyển", "25.000")),
    ("đổi hàng được không", ("đổi trả", "7 ngày")),
    ("mấy giờ đóng cửa chủ nhật", ("mở cửa", "21h")),
]


@pytest.mark.live
@pytest.mark.asyncio
@pytest.mark.parametrize(("query", "expected_markers"), _RANKING_CASES)
async def test_wiki_rag_ranks_correct_chunk_first_with_real_e5(
    query: str, expected_markers: tuple[str, ...]
) -> None:
    """`TogetherEmbedder` thật + mạng thật + Postgres thật: chunk ĐÚNG CHỦ ĐỀ phải xếp ĐẦU.

    Đây là phát biểu mà F1 bán cho seller. "Có trả về gì đó" không phải phát biểu đó — bản test
    cũ assert `any(...)`, tức chunk đúng nằm cuối danh sách dưới ba đoạn lạc đề vẫn xanh.
    """
    _require_key()

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from agent.providers.together_embedder import TogetherEmbedder
    from db.models import Base
    from parsing.ingest import ingest_wiki
    from tools.wiki import search_wiki

    engine = create_async_engine(DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    embedder = TogetherEmbedder()  # client thật, key từ Settings

    n = await ingest_wiki(
        session_factory, embedder, text=WIKI_TEXT, source_ref="wiki:chinh-sach", max_chars=200
    )
    assert n >= 3, f"cần ≥3 chunk để đo được thứ hạng, ingest trả {n}"

    hits = await search_wiki(query, embedder=embedder, session_factory=session_factory)
    await engine.dispose()

    assert len(hits) >= 2, "cần ≥2 hit để so thứ hạng"

    top = hits[0].chunk.lower()
    assert any(m in top for m in expected_markers), (
        f"Hỏi {query!r} nhưng chunk ĐẦU không phải đoạn đúng chủ đề.\n"
        f"  hits[0] = {hits[0].chunk.strip()[:120]!r}\n"
        f"  chờ một trong: {expected_markers}\n"
        "Đây đúng ca mà bản test cũ (assert `any(...)` trên cả danh sách) sẽ bỏ qua."
    )

    for h in hits:
        assert h.namespace == "platform_wiki", (
            f"search_wiki phải ở trong namespace platform_wiki, nhận {h.namespace!r}"
        )


@pytest.mark.live
@pytest.mark.asyncio
async def test_live_embedding_dim_matches_db_column() -> None:
    """Vector e5 THẬT phải dài đúng `EMBED_DIM` — tức ingest được vào cột hiện tại.

    E0 đã đo dim=1024 bằng gọi thật, E1 đã đổi cột sang 1024. Test này nối hai đầu: nếu Together
    đổi model sau lưng (hoặc ai đó đổi `TOGETHER_EMBED_MODEL` sang model khác chiều), triệu
    chứng sẽ là ingest hỏng ở production. Ở đây nó hỏng trong một live test chạy tay — rẻ hơn
    nhiều.
    """
    _require_key()

    from agent.providers.together_embedder import TogetherEmbedder
    from app.config import EMBED_DIM

    vec = await TogetherEmbedder().embed_query("phí ship bao nhiêu")

    assert len(vec) == EMBED_DIM, (
        f"e5 trả vector {len(vec)} chiều nhưng cột DB là {EMBED_DIM} — ingest sẽ bị Postgres "
        "từ chối. Kiểm TOGETHER_EMBED_MODEL có bị đổi không."
    )
