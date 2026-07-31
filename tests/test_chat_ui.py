"""Chat UI contract gate — spec 07 Phase G2.

Viết TRƯỚC `web/src/screens/Chat.tsx` — kỳ vọng RED.

Repo này không có Playwright, nên không test được render thật. Điều test được — và là thứ
thật sự hay hỏng — là **hợp đồng giữa hai bên**: `ChatOut` (pydantic, `api/chat.py`) và
interface TypeScript mà `api.ts` bind vào. Hai file, hai ngôn ngữ, không compiler nào bắc
cầu giữa chúng; đổi tên một field ở backend thì frontend im lặng nhận `undefined` và render
ra chuỗi rỗng. Test này bắc cầu đó.

Hai yêu cầu còn lại đến từ phát hiện thật ở G1, không phải từ giấy:

  1. **Disclaimer phải hiện thường trực.** `grounded: false` là sự thật kỹ thuật; seller cần
     nó bằng tiếng Việt. G1 đo được: model VẪN hứa "2-3 ngày" giao hàng cho shop chưa cấu
     hình vận chuyển. Nếu seller tưởng đây là câu trả lời có căn cứ và copy nguyên cho khách,
     lời hứa bịa đó tới khách thật.

  2. **Loading state bắt buộc.** Đo thật ở G1: cold start **24.8 giây**. Một form không phản
     hồi thị giác trong 25 giây thì seller sẽ bấm gửi lại nhiều lần — mỗi lần là một request
     LLM tính tiền.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parent.parent / "web" / "src"
_API_TS = _WEB / "lib" / "api.ts"
_CHAT_TSX = _WEB / "screens" / "Chat.tsx"
_APP_TSX = _WEB / "App.tsx"

# Frontend `web/` CHƯA adopt sang repo OHANA (app/main.py mount web/dist CÓ ĐIỀU KIỆN,
# backend chạy được không cần nó). Skip theo sự tồn tại của thư mục — web/ sang là
# module tự sống lại. Đừng xoá file: các test này là spec của Chat UI.
pytestmark = pytest.mark.skipif(
    not _WEB.is_dir(),
    reason="web/ chưa adopt vào repo OHANA — tech-debt T6",
)


def _read(p: Path) -> str:
    if not p.is_file():
        pytest.fail(f"thiếu file {p.relative_to(_WEB.parent.parent)}")
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """Bỏ comment `/* */` và `//` — chỉ giữ code THỰC THI.

    Cần thật, không phải cho đẹp: `App.tsx` có docstring giải thích *vì sao KHÔNG dùng*
    `react-router-dom`, nên grep thô trên nguyên file sẽ báo vi phạm ở đúng câu văn nói rằng
    mình không vi phạm. Cùng lớp lỗi đã dính ở G0 (comment nhắc tên import bị chính test bắt).
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def _function_body(src: str, name: str) -> str:
    """Cắt ĐÚNG thân một hàm export, không lấy lấn sang hàm sau.

    Bản đầu cắt từ `name` tới hết file, nên `postChat` nuốt luôn docstring của
    `postWikiIngest` (có nhắc `shop_id`) và test báo sai. Cân ngoặc để dừng đúng chỗ.
    """
    i = src.index(f"function {name}")
    open_brace = src.index("{", i)
    depth = 0
    for j in range(open_brace, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise AssertionError(f"không cắt được thân hàm {name} — ngoặc lệch")


def test_api_ts_exposes_post_chat_bound_to_the_real_route() -> None:
    """`api.ts` có `postChat` POST tới đúng `/api/chat`, đi qua `apiFetch`.

    Bắt buộc qua `apiFetch`: đó là nơi CSRF + `credentials: include` được tập trung. Một
    `fetch()` trần trong màn Chat sẽ thiếu header CSRF ⇒ 403, hoặc tệ hơn là ai đó "sửa cho
    chạy" bằng cách nới middleware.
    """
    src = _strip_comments(_read(_API_TS))
    assert "postChat" in src, "api.ts chưa có postChat"

    block = _function_body(src, "postChat")
    assert '"/api/chat"' in block or "'/api/chat'" in block, "postChat không trỏ /api/chat"
    assert "apiFetch(" in block, "postChat phải đi qua apiFetch (CSRF tập trung ở đó)"
    assert 'method: "POST"' in block, "postChat phải là POST"


def test_typescript_interface_matches_the_python_response_model() -> None:
    """Field của interface TS PHẢI khớp `ChatOut` — đây là lý do chính test này tồn tại.

    Không có compiler nào kiểm chéo Python ↔ TypeScript. Đổi `reply` thành `text` ở backend
    thì frontend vẫn build xanh, vẫn chạy, và render `undefined` thành ô trống. Lấy field từ
    `ChatOut` bằng introspection chứ không chép tay danh sách — chép tay thì test sẽ mốc đúng
    lúc model đổi.
    """
    from api.chat import ChatOut

    expected = set(ChatOut.model_fields)
    assert expected, "ChatOut không có field nào — introspection hỏng, test vô nghĩa"

    src = _read(_API_TS)
    m = re.search(r"interface\s+ChatResult\s*\{(.*?)\}", src, re.S)
    assert m, "api.ts thiếu `interface ChatResult`"
    declared = set(re.findall(r"^\s*(\w+)\s*[?]?:", m.group(1), re.M))

    missing = expected - declared
    assert not missing, f"interface TS thiếu field backend trả về: {sorted(missing)}"
    extra = declared - expected
    assert not extra, f"interface TS khai field backend KHÔNG trả về: {sorted(extra)}"


def test_inbox_interface_matches_the_python_response_model() -> None:
    """Mirror của gate ChatOut phía trên cho `PendingReplyOut` — thêm SAU khi lớp lỗi này
    xảy ra thật: A8 thêm `escalation_reasons` vào backend mà TS không khai, build vẫn xanh
    (JSON dư field bị TS bỏ qua), nhưng draft ESCALATE render y hệt draft FAQ — đúng kịch
    bản `db/repos.py` cảnh báo. Introspect model, không chép tay danh sách field.
    """
    from api.inbox import PendingReplyOut

    expected = set(PendingReplyOut.model_fields)
    assert expected, "PendingReplyOut không có field nào — introspection hỏng, test vô nghĩa"

    src = _read(_API_TS)
    m = re.search(r"interface\s+PendingReplyOut\s*\{(.*?)\n\}", src, re.S)
    assert m, "api.ts thiếu `interface PendingReplyOut`"
    declared = set(re.findall(r"^\s*(\w+)\s*[?]?:", _strip_comments(m.group(1)), re.M))

    missing = expected - declared
    assert not missing, f"interface TS thiếu field backend trả về: {sorted(missing)}"
    extra = declared - expected
    assert not extra, f"interface TS khai field backend KHÔNG trả về: {sorted(extra)}"


def test_inbox_and_review_render_escalation_chips() -> None:
    """A8 end-to-end: cả hai màn seller nhìn thấy draft đều phải đọc `escalation_reasons`
    qua `escalationMeta` (nhãn VN tập trung ở intent.ts, không hardcode trong screen).
    Thiếu một màn là seller mở đúng màn đó và mất tín hiệu escalate.
    """
    for screen in ("Inbox.tsx", "ReviewCard.tsx"):
        src = _strip_comments(_read(_WEB / "screens" / screen))
        assert "escalation_reasons" in src, f"{screen} không đọc escalation_reasons"
        assert "escalationMeta(" in src, f"{screen} không dịch reason qua escalationMeta"


def test_escalation_labels_cover_every_severity_rank_value() -> None:
    """`ESCALATION_META` phải phủ ĐỦ SEVERITY_RANK — thiếu key nào thì reason đó render
    bằng key thô tiếng Anh (fallback cố ý, nhưng chỉ dành cho drift chưa kịp vá, không
    phải trạng thái bình thường). Introspect từ policy_gate, cùng nguồn với CHECK của DB.
    """
    from agent.policy_gate import SEVERITY_RANK

    src = _strip_comments(_read(_WEB / "lib" / "intent.ts"))
    m = re.search(r"ESCALATION_META[^=]*=\s*\{(.*?)\n\};", src, re.S)
    assert m, "intent.ts thiếu ESCALATION_META"
    declared = set(re.findall(r"^\s*(\w+)\s*:\s*\{", m.group(1), re.M))
    missing = set(SEVERITY_RANK) - declared
    assert not missing, f"ESCALATION_META thiếu nhãn cho: {sorted(missing)}"
    extra = declared - set(SEVERITY_RANK)
    assert not extra, f"ESCALATION_META có key ngoài SEVERITY_RANK: {sorted(extra)}"


def test_shop_onboard_interface_matches_the_python_response_model() -> None:
    """Gate contract thứ ba cùng họ ChatOut/PendingReplyOut — cho `ShopOnboardResponse`.
    Thêm cùng lượt với màn onboard (gap 2 audit T6) thay vì chờ drift xảy ra như A8."""
    from api.admin import ShopOnboardResponse

    expected = set(ShopOnboardResponse.model_fields)
    assert expected, "ShopOnboardResponse không có field nào — introspection hỏng"

    src = _read(_API_TS)
    m = re.search(r"interface\s+ShopOnboardResult\s*\{(.*?)\n\}", src, re.S)
    assert m, "api.ts thiếu `interface ShopOnboardResult`"
    declared = set(re.findall(r"^\s*(\w+)\s*[?]?:", _strip_comments(m.group(1)), re.M))

    missing = expected - declared
    assert not missing, f"interface TS thiếu field backend trả về: {sorted(missing)}"
    extra = declared - expected
    assert not extra, f"interface TS khai field backend KHÔNG trả về: {sorted(extra)}"


def test_api_ts_shop_onboard_never_sends_shop_id() -> None:
    """`postShopOnboard` POST đúng `/api/admin/shops` qua `apiFetch`, body CHỈ mang `name` —
    `shop_id` do server sinh (uuid4). Client mà gửi được id là chọn được danh tính tenant,
    toàn bộ verify của S1 thành vô nghĩa (docstring `api/admin.py::onboard_shop`)."""
    src = _strip_comments(_read(_API_TS))
    assert "postShopOnboard" in src, "api.ts chưa có postShopOnboard"

    block = _function_body(src, "postShopOnboard")
    assert '"/api/admin/shops"' in block, "postShopOnboard không trỏ /api/admin/shops"
    assert "apiFetch(" in block, "postShopOnboard phải đi qua apiFetch (CSRF tập trung ở đó)"
    assert 'method: "POST"' in block, "postShopOnboard phải là POST"
    assert "shop_id" not in block, "client không bao giờ được đề xuất shop_id"


def test_ui_reaches_both_admin_surfaces() -> None:
    """Gap 2+3 audit T6 đóng cùng nhau và phải SỐNG cùng nhau: ChannelPicker có đường mint
    `role=admin` (trước đây mọi màn admin chết 403 vì UI chỉ mint seller), và shell App
    route được tới màn onboard đang gọi `postShopOnboard`. Thiếu mảnh nào thì surface admin
    quay lại trạng thái màn-chết mà audit mô tả."""
    picker = _strip_comments(_read(_WEB / "screens" / "ChannelPicker.tsx"))
    assert 'mockAuthorize("admin")' in picker, "ChannelPicker mất đường mint role=admin"

    onboard = _strip_comments(_read(_WEB / "screens" / "AdminShopOnboard.tsx"))
    assert "postShopOnboard(" in onboard, "AdminShopOnboard không gọi postShopOnboard"

    app = _strip_comments(_read(_APP_TSX))
    assert "AdminShopOnboard" in app, "App.tsx không route tới màn onboard shop"


def test_chat_screen_shows_a_persistent_not_grounded_disclaimer() -> None:
    """Disclaimer hiện thường trực, KHÔNG nấp trong tooltip/title/aria.

    Spec §7 G2 nói "hiện thường trực, không phải tooltip ẩn". Kiểm cả hai chiều: có chữ cảnh
    báo, VÀ chữ đó nằm trong nội dung render được chứ không chỉ trong thuộc tính.
    """
    src = _strip_comments(_read(_CHAT_TSX))

    # PHẢI strip comment trước. Bản đầu của test này grep nguyên file và **xanh cả khi
    # disclaimer đã bị xoá**, vì cụm "AI tổng quát" còn nằm trong docstring của chính
    # `Chat.tsx`. Mutation test (M10) lôi ra được; đọc code thường thì không. Đây đúng là lớp
    # lỗi tôi vẫn bắt ở chỗ khác: docstring không phải bằng chứng, kể cả khi nó là của mình.
    element = re.search(r'className="chat-disclaimer"(.*?)</p>', src, re.S)
    assert element, "Chat.tsx thiếu phần tử .chat-disclaimer hiện thường trực"
    body = element.group(1)

    signals = ["không có căn cứ", "không tra cứu", "chưa kết nối dữ liệu", "tham khảo"]
    assert any(s.lower() in body.lower() for s in signals), (
        f"phần tử disclaimer không mang nội dung cảnh báo nào khớp {signals}"
    )

    # Nội dung cảnh báo phải nằm trong text render được, KHÔNG chỉ trong title=/aria-label=.
    attrs = " ".join(re.findall(r'(?:title|aria-label)="([^"]*)"', body))
    rendered = re.sub(r'(?:title|aria-label)="[^"]*"', "", body)
    assert any(s.lower() in rendered.lower() for s in signals), (
        f"cảnh báo chỉ nằm trong thuộc tính ẩn ({attrs!r}) — spec §7 G2 cấm tooltip ẩn"
    )

    # Và nó phải được render VÔ ĐIỀU KIỆN — không nấp sau `{showWarning && ...}`.
    # Regex nới hơn bản đầu (reviewer nêu): bản cũ chỉ khớp `{tên &&`, nên `{(cờ) &&` hay
    # `{a && b && <p ...` sẽ lọt. Giờ chấp mọi biểu thức trước `&&`, và cả toán tử ba ngôi.
    guarded = re.search(
        r"\{[^{}]*(?:&&|\?)\s*\(?\s*<p[^>]*className=\"chat-disclaimer\"", src, re.S
    )
    assert not guarded, "disclaimer bị bọc trong điều kiện — phải hiện thường trực"


def test_chat_screen_has_a_loading_state() -> None:
    """Phải có phản hồi thị giác lúc chờ. Đo thật ở G1: cold start 24.8s.

    Không có nó, seller nhìn màn hình bất động 25 giây rồi bấm gửi lại — mỗi lần một request
    LLM tính tiền, và các câu trả lời về không theo thứ tự.
    """
    # `_strip_comments` ở đây không phải thừa: bản đầu dùng `_read` thô, nên một comment kiểu
    # "TODO: thêm loading state" cũng đủ làm test xanh trong khi state đó không tồn tại. Đúng
    # cái bẫy đã làm test disclaimer thành vô nghĩa (M10). Reviewer nêu; sửa cho nhất quán chứ
    # không đợi nó cháy lần nữa.
    src = _strip_comments(_read(_CHAT_TSX))
    assert re.search(r"\b(sending|loading|submitting|isBusy|pending)\b", src), (
        "Chat.tsx không có state chờ — 25 giây im lặng sẽ khiến seller bấm gửi lại"
    )
    # Phải disable THẬT trong lúc gửi, không chỉ có chuỗi `disabled=` ở đâu đó: kiểm nó gắn
    # với chính state chờ.
    assert re.search(r"disabled=\{[^}]*sending", src), (
        "input/nút không bị disable theo state gửi — seller bấm được nhiều lần, mỗi lần tốn tiền"
    )


def test_chat_screen_is_reachable_from_the_shell() -> None:
    """Màn Chat được nối vào routing state-based của `App.tsx` (KHÔNG thêm react-router).

    Một màn không ai tới được thì bằng chưa làm.
    """
    app = _strip_comments(_read(_APP_TSX))
    assert "ChatScreen" in app, "App.tsx chưa import/render màn Chat"
    assert 'name: "chat"' in app or "name: 'chat'" in app, (
        "Chat chưa được thêm vào union Screen của App.tsx"
    )
    # Kiểm IMPORT thật, không kiểm sự có mặt của chuỗi: docstring App.tsx giải thích vì sao
    # KHÔNG dùng react-router, và câu văn đó không phải vi phạm.
    assert not re.search(r"""^\s*import\s.*['"]react-router""", app, re.M), (
        "KHÔNG thêm react-router (spec §7 G2)"
    )


def test_chat_css_keeps_the_two_load_bearing_overrides() -> None:
    """Canh 2 override CSS đã phải trả giá bằng bug thật (tìm ra bằng cách NHÌN, không phải test).

    Đây là guard YẾU — nó đọc chuỗi trong file CSS, không dựng layout. Repo chưa có công cụ
    kiểm layout thật (không Playwright), nên đây là mức tốt nhất hiện có. Ghi ra để không ai
    tưởng có test này là layout đã được bảo chứng.

    Hai bug đã xảy ra, cả hai đều do kế thừa từ style dùng chung:

      1. `.btn-primary` mang `width: 100%` (đúng cho 4 màn P1 xếp nút theo cột). Trong hàng
         flex của composer, nút đòi trọn chiều rộng và **bóp ô nhập còn một sợi**. `min-width`
         không chống lại được `width: 100%` — phải `width: auto`.

      2. `.screen` mang `flex: 1` ⇒ `flex-basis: 0%`, mà trong flex container dọc flex-basis
         THẮNG `height`. Nên đặt `height: 100dvh` thôi thì bị bỏ qua: transcript không cuộn
         nội bộ, cả trang dài ra, **ô nhập bị đẩy khỏi màn hình** khi hội thoại dài (đo ở
         375×812: trang cao 1247, đáy composer 1216). Phải kèm `flex: 0 0 auto`.
    """
    css = (_WEB / "screens" / "Chat.css").read_text(encoding="utf-8")

    send = re.search(r"\.chat-send\s*\{(.*?)\}", css, re.S)
    assert send and re.search(r"width:\s*auto", send.group(1)), (
        ".chat-send thiếu `width: auto` — sẽ kế thừa `width: 100%` của .btn-primary và bóp ô nhập"
    )

    screen = re.search(r"\.chat-screen\s*\{(.*?)\}", css, re.S)
    assert screen, "thiếu .chat-screen"
    assert re.search(r"height:\s*100dvh", screen.group(1)), (
        ".chat-screen thiếu chiều cao xác định — transcript sẽ không cuộn nội bộ"
    )
    assert re.search(r"flex:\s*0\s+0\s+auto", screen.group(1)), (
        ".chat-screen thiếu `flex: 0 0 auto` — `flex: 1` của .screen sẽ vô hiệu hoá height"
    )


def test_chat_ui_never_sends_shop_id() -> None:
    """Frontend KHÔNG bao giờ gửi `shop_id`.

    Backend đã bỏ qua field đó (`ChatIn extra=ignore`, gate ở G1), nên đây là lớp thứ hai —
    nhưng là lớp đáng có: nếu FE bắt đầu gửi `shop_id`, đó là dấu hiệu ai đó đang tư duy sai
    về nguồn của tenant scope, và lần sau họ sẽ đọc nó ở backend.
    """
    src = _strip_comments(_read(_CHAT_TSX))
    api = _strip_comments(_read(_API_TS))
    assert "shop_id" not in src, "Chat.tsx nhắc shop_id — tenant scope CHỈ từ JWT"
    assert "shop_id" not in _function_body(api, "postChat"), (
        "postChat gửi shop_id — tenant scope CHỈ từ JWT"
    )
