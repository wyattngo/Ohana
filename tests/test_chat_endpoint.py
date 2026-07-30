"""General Chat endpoint gate — spec 07 Phase G1.

Viết TRƯỚC `api/chat.py` — kỳ vọng RED.

Mọi test chạy trên **`app.main.app` THẬT**, không dựng app riêng. Lý do: hai thứ quan trọng
nhất của phase này là *thứ tự mount* (router phải đứng TRƯỚC `StaticFiles` catch-all, nếu
không `/api/chat` bị nuốt thành 404 HTML) và *middleware CSRF* — cả hai chỉ tồn tại trong
`app/main.py`. Một app tự dựng trong test sẽ xanh trong khi production 404. LLM tiêm qua
`dependency_overrides` nên vẫn không có request mạng nào bay đi.

Trục nguy hiểm nhất KHÔNG phải endpoint hỏng — mà là **trượt ranh giới**: general chat không
grounded (không Wiki-RAG, không tồn kho thật) mà lại chạm được đường gửi khách thì AI sẽ bịa
giá/tồn kho tới khách hàng THẬT. Roadmap §3.0 nói thẳng "không được trượt ranh giới này".
`test_chat_module_cannot_reach_the_customer_send_path` ép điều đó bằng import-graph, vì một
comment "nhớ đừng import sender" không sống sót qua sáu tháng và bốn người sửa file.
"""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agent.llm_client import AssistantStep

_FIXTURE_SHOP_ID = "fixture-shop-001"  # api/mock_auth.py mints this
_REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeLLM:
    """Ghi lại messages nhận được, trả reply cố định. Không mạng, không key."""

    def __init__(self, reply: str = "Chào anh, em có thể giúp gì?") -> None:
        self.reply = reply
        self.seen: list[list[dict[str, Any]]] = []

    async def step(self, messages: list[dict[str, Any]], **kwargs: Any) -> AssistantStep:
        self.seen.append(messages)
        return AssistantStep(
            content=self.reply,
            tool_calls=[],
            usage={"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        )

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> str:
        return self.reply

    def stream(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:  # pragma: no cover
        raise NotImplementedError("G1 dùng response 1 lần; streaming để phase sau")


@pytest.fixture
def chat_client(monkeypatch: pytest.MonkeyPatch):
    """Real app + fake LLM. Trả `(client, fake)` để test soi được cả HTTP lẫn cái LLM nhận."""
    monkeypatch.setenv("OHANA_ENV", "dev")
    import api.chat as chat_mod
    from api.chat import get_llm_client
    from app.main import app

    fake = _FakeLLM()
    app.dependency_overrides[get_llm_client] = lambda: fake
    # `api.chat._client_cache` là global module-level. Hôm nay dependency-override khiến
    # `get_llm_client()` không bao giờ chạy nên cache luôn rỗng — nhưng đó là điều kiện dễ vỡ:
    # một test sau này gọi hàm thật sẽ nhét client (kèm key lúc đó) vào global, và mọi test
    # chạy SAU nó nhận lại client cũ dù đã đổi env. Dọn ở cả hai đầu để thứ tự test không bao
    # giờ trở thành một biến ẩn.
    chat_mod._client_cache = None
    try:
        yield TestClient(app), fake
    finally:
        app.dependency_overrides.pop(get_llm_client, None)
        chat_mod._client_cache = None


def _authorize(client: TestClient) -> dict[str, str]:
    resp = client.post("/api/mock/authorize")
    assert resp.status_code == 200
    csrf = client.cookies.get("ohana_csrf")
    assert csrf, "mock authorize phải mint cookie ohana_csrf"
    return {"X-CSRF-Token": csrf}


# ── happy path ────────────────────────────────────────────────────────────────────────────


def test_authenticated_seller_gets_a_reply(chat_client) -> None:
    client, fake = chat_client
    headers = _authorize(client)

    resp = client.post("/api/chat", json={"message": "Cách tính phí ship?"}, headers=headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["reply"] == fake.reply
    assert body["model"], "response phải nói rõ model nào trả lời (audit + so sánh cost)"
    assert len(fake.seen) == 1, "phải gọi LLM đúng 1 lần"
    assert any("Cách tính phí ship?" in str(m) for m in fake.seen[0]), (
        "câu hỏi của seller phải tới được LLM"
    )


def test_response_flags_itself_as_not_grounded(chat_client) -> None:
    """`grounded: false` là cờ tường minh, KHÔNG phải chi tiết trang trí.

    General chat không có Wiki-RAG, không có tồn kho thật — nó CÓ THỂ bịa. Consumer sau này
    (màn Chat G2, hoặc bất cứ thứ gì đọc endpoint này) phải phân biệt được câu trả lời có căn
    cứ và câu trả lời tự do. Nếu cờ này thiếu hoặc là `true`, một phase sau rất dễ nối nhầm
    output này vào luồng soạn reply cho khách.
    """
    client, _ = chat_client
    headers = _authorize(client)

    body = client.post("/api/chat", json={"message": "xin chào"}, headers=headers).json()

    assert "grounded" in body, "thiếu cờ grounded"
    assert body["grounded"] is False, f"general chat KHÔNG grounded, nhận {body['grounded']!r}"


def test_usage_is_reported_for_cost_telemetry(chat_client) -> None:
    """Token in/out phải về tới caller — không đo thì không biết General Chat tốn bao nhiêu,
    và PRE-008 (credit metering) sau này không có gì để dựa vào."""
    client, _ = chat_client
    headers = _authorize(client)

    body = client.post("/api/chat", json={"message": "xin chào"}, headers=headers).json()

    assert body["usage"]["prompt_tokens"] == 11
    assert body["usage"]["completion_tokens"] == 7


def test_empty_llm_reply_is_an_error_not_a_blank_bubble(chat_client, caplog) -> None:
    """LLM trả content rỗng ⇒ 502, KHÔNG phải 200 với `reply: ""`.

    Phát hiện lúc dò model (G1, 2026-07-19): model **reasoning** đẩy hết output sang kênh suy
    luận và trả `content` rỗng nếu `max_tokens` không đủ — Kimi-K2.6 đốt sạch 300 token vẫn
    rỗng, không exception nào. Nếu endpoint trả 200, seller nhìn thấy một bong bóng chat trống
    và không ai biết token đã bị đốt. Hỏng-âm-thầm tệ hơn hỏng-ồn-ào.

    Default hiện tại (`Llama-3.3-70B`) là non-reasoning nên không dính. Nhưng `TOGETHER_MODEL`
    đổi được bằng env — hàng rào phải ở code, không ở việc nhớ chọn đúng model.
    """
    client, fake = chat_client
    fake.reply = "   "  # rỗng sau strip
    headers = _authorize(client)

    with caplog.at_level(logging.WARNING):
        resp = client.post("/api/chat", json={"message": "xin chào"}, headers=headers)

    assert resp.status_code == 502, f"content rỗng phải là lỗi, nhận {resp.status_code}"
    assert "empty_content" in caplog.text, "phải log được sự kiện này để điều tra"


# ── auth / CSRF ───────────────────────────────────────────────────────────────────────────


def test_no_credentials_at_all_is_403_from_csrf(chat_client) -> None:
    """Không cookie gì cả ⇒ CSRF chặn TRƯỚC, 403 — không phải 401.

    Trước đây test này tên là `..._is_401` và assert `in (401, 403)`. Cả tên lẫn assertion
    đều sai: middleware CSRF chạy trước dependency auth nên đường này LUÔN là 403, và một
    assertion nhận cả hai mã sẽ không phát hiện được khi thứ tự đó đổi. Tệ hơn: nó tạo cảm
    giác đã phủ nhánh 401 trong khi nhánh đó chưa từng được chạy — xem test ngay dưới.
    """
    client, fake = chat_client
    resp = client.post(
        "/api/chat", json={"message": "hi"}, headers={"X-CSRF-Token": "bat-ky-gia-tri-nao"}
    )
    assert resp.status_code == 403, resp.text
    assert fake.seen == [], "request chưa xác thực KHÔNG được chạm tới LLM (đốt tiền + rò key)"


def test_valid_csrf_but_no_session_cookie_is_401(chat_client) -> None:
    """Nhánh 401 THẬT (spec §7 G1 mục b) — trước đây chưa test nào chạm tới.

    Để tới được `identity_from_cookie` thì phải qua cửa CSRF trước. Nên dựng đúng trạng thái
    đó: authorize để lấy CSRF cookie hợp lệ, rồi XOÁ RIÊNG cookie phiên. CSRF pass, auth fail
    ⇒ 401. Đây là kịch bản thật khi phiên hết hạn giữa lúc seller đang mở tab.
    """
    client, fake = chat_client
    headers = _authorize(client)
    client.cookies.delete("ohana_session")  # chỉ phiên hết hạn, CSRF còn nguyên

    resp = client.post("/api/chat", json={"message": "hi"}, headers=headers)

    assert resp.status_code == 401, f"phiên hết hạn phải là 401, nhận {resp.status_code}"
    assert fake.seen == [], "request chưa xác thực KHÔNG được chạm tới LLM"


def test_session_cookie_without_csrf_header_is_403(chat_client) -> None:
    """Double-submit: có cookie phiên vẫn chưa đủ. Đây chính là điều CSRF mua được — một
    trang web độc gửi POST kèm cookie của seller sẽ không có header này."""
    client, fake = chat_client
    client.post("/api/mock/authorize")  # có cookie phiên, CỐ Ý không echo CSRF

    resp = client.post("/api/chat", json={"message": "hi"})

    assert resp.status_code == 403, resp.text
    assert fake.seen == [], "request thiếu CSRF KHÔNG được chạm tới LLM"


# ── adversarial: shop_id ──────────────────────────────────────────────────────────────────


def test_shop_id_comes_from_jwt_not_body(chat_client, caplog) -> None:
    """Body khai `shop_id` khác JWT ⇒ JWT thắng, tuyệt đối.

    Đây là R1.22 ở tầng endpoint. Nếu `shop_id` đọc được từ body thì mọi seller đọc/ghi được
    dữ liệu shop khác chỉ bằng cách sửa một dòng JSON — cross-tenant, thứ nghiêm trọng nhất
    trong sản phẩm multi-tenant. Kiểm qua log vì đó là nơi `shop_id` quan sát được.
    """
    client, _ = chat_client
    headers = _authorize(client)

    with caplog.at_level(logging.INFO):
        resp = client.post(
            "/api/chat",
            json={"message": "hi", "shop_id": "shop-cua-ke-tan-cong"},
            headers=headers,
        )

    assert resp.status_code == 200, resp.text
    assert "shop-cua-ke-tan-cong" not in caplog.text, (
        "shop_id từ body đã lọt vào xử lý — cross-tenant"
    )
    assert _FIXTURE_SHOP_ID in caplog.text, "phải dùng shop_id từ JWT đã verify"


def test_observability_fields_are_logged(chat_client, caplog) -> None:
    """Spec §7 G1: log `model_id`/`token_in`/`token_out`/`latency_ms`/`shop_id`."""
    client, _ = chat_client
    headers = _authorize(client)

    with caplog.at_level(logging.INFO):
        client.post("/api/chat", json={"message": "xin chào"}, headers=headers)

    blob = caplog.text
    for field in ("model", "token_in", "token_out", "token_cached", "latency_ms", "shop_id"):
        assert field in blob, f"thiếu trường observability {field!r} trong log"


def test_cached_tokens_is_logged_from_provider_usage(chat_client, caplog) -> None:
    """`token_cached` phải lấy từ usage của provider, KHÔNG hardcode 0.

    `cached_tokens` được `_extract_cache_hit_tokens` trích sẵn từ lúc port DrNick nhưng chưa
    ai đọc. Log nó để có SỐ LIỆU trả lời "Together có tự cache prompt không / tỷ lệ bao nhiêu"
    trước khi bàn xây cache — thay vì đoán.

    Test đưa `cached_tokens` KHÁC 0 để phân biệt "đọc thật từ usage" với "in hằng số 0".
    Nếu ai đó hardcode `token_cached=0` cho tiện, test này đỏ.
    """
    client, fake = chat_client
    fake_usage = {
        "prompt_tokens": 900,
        "completion_tokens": 20,
        "total_tokens": 920,
        "cached_tokens": 768,
    }

    async def step_with_cache(messages, **kwargs):  # type: ignore[no-untyped-def]
        return AssistantStep(content="ok", tool_calls=[], usage=fake_usage)

    fake.step = step_with_cache  # type: ignore[method-assign]
    headers = _authorize(client)

    with caplog.at_level(logging.INFO):
        resp = client.post("/api/chat", json={"message": "xin chào"}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert "token_cached=768" in caplog.text, (
        f"token_cached không đọc từ usage của provider — log: {caplog.text[-300:]}"
    )
    assert resp.json()["usage"]["cached_tokens"] == 768, "response cũng phải mang cached_tokens"


def test_app_logging_is_configured_so_info_actually_reaches_output() -> None:
    """`logger.info` của app phải THẬT SỰ được ghi khi chạy dưới uvicorn — không chỉ dưới caplog.

    Đây là test bổ sung sau một defect có thật (2026-07-19). `test_observability_fields_are_logged`
    ở trên dùng `caplog.at_level(logging.INFO)`, tức **pytest tự ép mức lên INFO**. Nó chứng
    minh "code có gọi logger", KHÔNG chứng minh "dòng log xuất hiện ở production".

    Thực tế: uvicorn cấu hình logger của riêng nó rồi để root KHÔNG handler, mức WARNING ⇒
    `api.chat` có effective level WARNING ⇒ mọi dòng observability của G1 bị nuốt im lặng.
    Phát hiện bằng cách mở trình duyệt bấm thử rồi grep log server, không phải bằng test.

    Test này dựng ĐÚNG thứ tự thật (uvicorn dictConfig trước, import app sau) và đòi INFO sống
    sót — nên nếu ai gỡ `logging.basicConfig(force=True)` trong `app/main.py`, nó đỏ.

    Chạy trong SUBPROCESS, không trong process pytest: bản đầu làm inline và tiền đề của nó
    sập ngay — vì test khác đã import `app.main` từ trước nên `basicConfig` đã chạy, root đã
    ở INFO, và phép thử trở thành phụ thuộc thứ tự test. Interpreter sạch mới phản ánh đúng
    cái mà uvicorn thực sự làm lúc khởi động.
    """
    import subprocess
    import sys

    probe = """
import logging, logging.config, uvicorn.config, json
# 1. uvicorn cấu hình logging TRƯỚC khi import app (đúng thứ tự thật).
logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
before = logging.getLogger("api.chat").isEnabledFor(logging.INFO)
# 2. rồi mới import app.
import app.main  # noqa: F401
after = logging.getLogger("api.chat").isEnabledFor(logging.INFO)
print(json.dumps({"before": before, "after": after,
                  "root_handlers": len(logging.getLogger().handlers)}))
"""
    # S603 ở đây là báo nhầm, và tắt tại CHỖ NÀY chứ không tắt toàn repo — S603 vẫn phải
    # kêu ở mọi subprocess khác. `probe` là literal viết ngay trong file này, không nhận
    # input ngoài, không qua shell (`shell=False`, argv dạng list). Subprocess là CỐ Ý:
    # thứ cần đo — thứ tự `dictConfig` vs `import app.main` — chỉ quan sát được ở một tiến
    # trình sạch; đo in-process thì logging đã bị chính test runner cấu hình trước rồi.
    out = subprocess.run(  # noqa: S603
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
    )
    assert out.returncode == 0, f"probe lỗi:\n{out.stderr}"
    result = json.loads(out.stdout.strip().splitlines()[-1])

    assert result["before"] is False, (
        "tiền đề sai — uvicorn mặc định lẽ ra CHẶN INFO của app; nếu không thì test này không "
        "chứng minh được gì (chính là lỗ đã để defect lọt)"
    )
    assert result["after"] is True, (
        "sau khi import app, INFO vẫn bị chặn — observability của G1 bị nuốt im lặng ở server "
        "thật, đúng defect 2026-07-19"
    )
    assert result["root_handlers"] > 0, "root không có handler — log không đi đâu cả"


def test_chat_log_does_not_contain_the_message_body(chat_client, caplog) -> None:
    """Log đo lường KHÔNG được chứa nội dung tin nhắn.

    Cùng nguyên tắc đã áp ở G0 với message lỗi của hook: đo lường là số đếm, không phải bản
    sao nội dung. Tin nhắn seller có thể chứa thông tin khách hàng; PDPL không cho ta rải nó
    vào log ứng dụng chỉ vì tiện debug.
    """
    client, _ = chat_client
    headers = _authorize(client)
    customer_phone = "SO-DIEN-THOAI-KHACH-0909123456"

    with caplog.at_level(logging.DEBUG):
        client.post("/api/chat", json={"message": customer_phone}, headers=headers)

    assert customer_phone not in caplog.text, "nội dung tin nhắn đã lọt vào log"


# ── ranh giới an toàn (gate quan trọng nhất của phase) ────────────────────────────────────


def _first_party_imports(path: Path) -> set[str]:
    """Tên module first-party mà file này import (kể cả `from x import y`)."""
    roots = {
        "api",
        "app",
        "agent",
        "auth",
        "bridge",
        "channels",
        "db",
        "parsing",
        "retrieval",
        "storage",
        "tools",
    }
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in roots:
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in roots:
                found.add(node.module)
                for alias in node.names:
                    found.add(f"{node.module}.{alias.name}")
    return found


def _module_path(dotted: str) -> Path | None:
    p = _REPO_ROOT / (dotted.replace(".", "/") + ".py")
    if p.is_file():
        return p
    pkg = _REPO_ROOT / dotted.replace(".", "/") / "__init__.py"
    return pkg if pkg.is_file() else None


def _transitive_first_party(entry: str) -> set[str]:
    """Bao đóng import first-party từ `entry`, đi qua AST (không import thật — tránh việc
    chính hành động import lại kéo theo side effect)."""
    seen: set[str] = set()
    queue = [entry]
    while queue:
        mod = queue.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = _module_path(mod)
        if path is None:
            continue
        for child in _first_party_imports(path):
            if child not in seen:
                queue.append(child)
    return seen


def test_chat_module_cannot_reach_the_customer_send_path() -> None:
    """**Gate ranh giới — điều kiện DONE của G1.**

    General chat không grounded. Nếu nó với tới được đường gửi khách, AI sẽ bịa giá/tồn kho
    tới khách hàng THẬT — hỏng đúng thứ sản phẩm này tồn tại để bảo vệ. Ranh giới này phải là
    thuộc tính CẤU TRÚC (không có đường nào để đi), không phải kỷ luật của người viết code.

    Đi theo bao đóng import chứ không chỉ dòng import trực tiếp: `api/chat.py` import một
    helper mà helper đó import sender thì đường đi vẫn tồn tại. Đây là loại lỗ mà review bằng
    mắt bỏ sót — không ai mở hết cây import khi duyệt PR.
    """
    reachable = _transitive_first_party("api.chat")

    # Nếu `api/chat.py` không tồn tại (chưa viết, đổi tên, chuyển chỗ) thì bao đóng chỉ có
    # đúng chính nó và MỌI assertion `not in` bên dưới xanh một cách vô nghĩa. Chốt chặn này
    # biến "file biến mất" từ gate-xanh-âm-thầm thành lỗi ồn ào.
    assert _module_path("api.chat") is not None, "không tìm thấy api/chat.py — gate ranh giới rỗng"
    assert len(reachable) > 1, "api/chat.py không import gì cả — đáng ngờ, gate có thể vô nghĩa"

    forbidden = {
        "agent.policy_gate": "policy_gate là cổng duyệt reply GỬI KHÁCH — general chat không "
        "được đứng cùng đường đó",
        "agent.orchestrator": "orchestrator là luồng park/gửi khách",
        "channels.zalo": "adapter kênh = đường ra tới khách",
        "bridge.zalo_sender": "sender = đường ra tới khách",
    }
    hits = [f"{m} ({why})" for m, why in forbidden.items() if m in reachable]
    assert not hits, "api/chat.py với tới đường gửi khách qua: " + "; ".join(hits)

    # `PendingReply` là hàng đợi duyệt-rồi-gửi. Chạm vào nó nghĩa là chat đang park thứ gì đó
    # để gửi đi — chính xác cái G1 cấm.
    assert not any("PendingReply" in m for m in reachable), (
        "api/chat.py chạm PendingReply — hàng đợi gửi khách"
    )
    # Bắt cả sender đặt tên khác trong tương lai.
    assert not any("sender" in m.lower() for m in reachable), (
        f"api/chat.py với tới module có tên giống sender: {sorted(reachable)}"
    )


def test_boundary_gate_would_actually_catch_a_violation() -> None:
    """Chứng minh gate ở trên KHÔNG vô nghĩa.

    Một test grep-kiểu-này rất dễ trở thành bù nhìn: nếu `_transitive_first_party` âm thầm trả
    tập rỗng (sai path, sai tên module, AST parse hỏng) thì mọi assertion `not in` đều xanh
    vĩnh viễn và ranh giới an toàn thực chất KHÔNG được canh. Nên: chạy chính bộ máy đó trên
    một module ĐÃ BIẾT là có chạm sender, và đòi nó phát hiện ra.
    """
    reachable = _transitive_first_party("api.webhook")
    assert reachable, "bao đóng import rỗng — máy dò hỏng, mọi gate ranh giới đều là bù nhìn"
    assert any("sender" in m.lower() or "channels" in m for m in reachable), (
        "api.webhook được biết là chạm channel/sender; máy dò không thấy ⇒ nó không hoạt động"
    )
