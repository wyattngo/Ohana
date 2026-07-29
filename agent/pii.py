"""Redactor PII — hàm THUẦN, chưa ai gọi (spec 16 A0 · `backend-workflow.md` §5).

Đây là **tầng sàn**, không phải bảo đảm. Regex bắt được cái nó biết mặt; thứ nó sót
thì sót im lặng. Vì vậy con số false-negative phải đo được trên dữ liệu thật (PRE-010
C4 → phase D0) — cho tới khi có con số đó, đọc "PII filter DONE" thành "PII đã an
toàn" là đọc sai.

**Vì sao thay bằng token có nhãn thay vì xoá trắng.** `[SĐT]` giữ hình dạng câu, nên
LLM vẫn hiểu khách vừa đưa số điện thoại và soạn nháp đúng ngữ cảnh ("shop ghi nhận
số của mình rồi ạ"). Xoá trắng làm câu vỡ nghĩa ⇒ nháp sai ⇒ seller mất niềm tin vào
công cụ — cái giá đó thuộc trục user-trust, không phải trục thẩm mỹ.

**Vì sao MỘT lượt quét, không phải năm lượt nối nhau.** Năm lượt `re.sub` chồng nhau
thì kết quả phụ thuộc thứ tự áp dụng, và thứ tự ấy sẽ trôi ở lần refactor kế tiếp.
Một alternation duy nhất, quét trái→phải một lần, biến thứ tự thành **thứ tự ưu tiên
khai báo tường minh** ngay trong pattern:

    EMAIL  >  SĐT  >  CCCD  >  STK  >  ĐỊA_CHỈ

Ưu tiên chỉ quyết **nhãn**, không quyết có redact hay không: mọi nhánh đều thay thế,
nên một dải số bị dán nhầm `[STK]` thay vì `[CCCD]` là sai lệch **telemetry**, không
phải rò rỉ. Ca thật của việc này là dải 9 số — CMND cũ và số tài khoản trùng hình dạng
nhau, không cách nào phân biệt bằng regex.

**Mọi pattern số đều neo hai đầu dải số** (`(?<!\d)` … `(?!\d)`). Không neo thì pattern
SĐT sẽ ăn 10 số đầu của một CCCD 12 số mở đầu `079` (mã tỉnh HCM, trùng prefix di động
`07`) và chừa lại 2 số — PII bị cắt đôi mà đầu ra vẫn *trông như* đã lọc. Gate:
`test_long_digit_run_not_cut_in_half`.

⚠️ **Khoảng sót đã biết, ghi ra để không tưởng là đã phủ:** GOAL của A0 nói "10-11 số"
nên bản này chỉ bắt dải số **liền nhau**. Khách gõ `0912 345 678` hoặc `0912.345.678`
— dạng rất phổ biến — sẽ LỌT. Nới pattern là đổi phạm vi một GOAL đã ký, nên nó cần
Wyatt quyết, không phải sửa lén ở đây. Đây là ứng viên số một cho con số FN của D0.

⚠️ **Mơ hồ NỘI TẠI, không phải lỗi chờ vá:** "350 Nguyễn Huệ" bị nhận là địa chỉ. Nhưng
"số + ≥2 danh từ riêng" CHÍNH LÀ chữ ký của địa chỉ — không regex nào tách được nó khỏi
"giá 350 + tên người", vì hai thứ đó là cùng một chuỗi ký tự. Các dạng giá THẬT trong tin
nhắn VN (`350k`, `350 đồng`, `350 triệu`) đều đi qua nguyên vẹn vì từ theo sau viết
thường. Phần dư lại chỉ giải được bằng filter model-based (§8.5), và ngưỡng mở nó là con
số FN của D0. Ghi ở đây để không ai coi nó là việc bỏ quên.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Chữ hoa tiếng Việt viết tường minh. `str.isupper()` xử lý Unicode đúng hơn, nhưng nó
# sống trong Python còn đây cần điều kiện nằm TRONG regex: nếu để callback loại bỏ sau,
# nhánh tên đường tham lam sẽ nuốt luôn các từ thường theo sau ("88 đường Lê Duẩn giúp
# em") rồi bị loại cả cụm — tức địa chỉ thật thoát lọc. Ràng buộc trong pattern làm phép
# lặp tự dừng đúng ở từ thường đầu tiên.
_VN_UPPER = "A-ZÀÁÂÃÈÉÊÌÍÒÓÔÕÙÚÝĂĐĨŨƠƯẢẠẰẮẲẴẶẦẤẨẪẬẺẼẸỀẾỂỄỆỈỊỎỌỒỐỔỖỘỜỚỞỠỢỦỤỪỨỬỮỰỲỶỸỴ"
_NAME_WORD = rf"[{_VN_UPPER}][^\W\d_]*"

# Một số 4 chữ số trong dải 1900-2099 đứng MỘT MÌNH gần như luôn là năm, không phải số nhà.
# Không loại nó ra thì "sinh năm 1999 Nguyễn Văn Nam" → "sinh năm [ĐỊA_CHỈ]": redactor nuốt
# nguyên vế sau và câu mất nghĩa. Đây đúng thứ `APPROACH` cấm ("số không-PII KHÔNG được đụng"),
# và nó tệ theo kiểu im lặng — không ai thấy cho tới khi đọc một nháp đã méo.
# Loại trừ này CHỈ áp cho nhánh không-từ-khoá: "2026 đường Nguyễn Huệ" vẫn là địa chỉ, vì
# `đường` là tín hiệu đủ mạnh để lấn át phỏng đoán năm.
_YEARISH = r"(?:19|20)\d{2}(?!\d)"

# Nhãn hiển thị + KHOÁ đếm. Khoá là ASCII vì `hits` đi thẳng vào destination-log; nhãn
# là tiếng Việt vì nó đi vào prompt, nơi model đọc nó như một phần câu tiếng Việt.
_TOKEN: dict[str, str] = {
    "email": "[EMAIL]",
    "phone": "[SĐT]",
    "national_id": "[CCCD]",
    "bank_account": "[STK]",
    "address": "[ĐỊA_CHỈ]",
}

_PATTERN = re.compile(
    rf"""
      (?P<email>[\w.+-]+@[\w-]+(?:\.[\w-]+)+)
    | (?P<phone>(?<!\d)(?:03|05|07|08|09)\d{{8,9}}(?!\d))
    | (?P<national_id>(?<!\d)(?:\d{{12}}|\d{{9}})(?!\d))
    | (?P<bank_account>(?<!\d)\d{{8,19}}(?!\d))
    | (?P<address>
          (?<![\d/])\d{{1,4}}(?:[/-]\d{{1,4}})*[A-Za-z]?(?!\d)
          \s+(?:đường|phố|ngõ|hẻm|ngách|tổ|ấp|thôn)\s+
          {_NAME_WORD}(?:\s+{_NAME_WORD}){{0,3}}
      | (?<![\d/])(?!{_YEARISH})\d{{1,4}}(?:[/-]\d{{1,4}})*[A-Za-z]?(?!\d)
          \s+{_NAME_WORD}(?:\s+{_NAME_WORD}){{1,3}}
      )
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class RedactionResult:
    """Kết quả lọc. `hits` đếm theo LOẠI và cố ý KHÔNG mang text.

    Destination-log tiêu thụ đúng object này. Log để audit mà lại chứa chính chuỗi PII
    vừa lọc thì bản thân cái log thành chỗ rò — nên ở đây không có đường nào để text
    gốc đi kèm con số ra ngoài.
    """

    text: str
    hits: dict[str, int]


def redact(text: str) -> RedactionResult:
    """Thay mọi PII nhận diện được bằng token có nhãn. Không đụng gì khác.

    Số KHÔNG phải PII phải đi qua nguyên vẹn — "2 cái", "350k", "150000". Lọc quá tay
    làm hỏng ngữ cảnh và nháp sẽ sai theo, nên nhánh địa chỉ đòi số nhà **kèm ≥2 từ
    viết hoa**: đó là thứ tách "123 Nguyễn Huệ" khỏi "2 cái nhé".

    Input sai kiểu ⇒ `TypeError`, KHÔNG trả về nguyên vẹn. Lớp bọc fail-closed ở B0 cần
    một exception để bắt; một giá trị trả về im lặng sẽ để payload chưa lọc đi tiếp và
    không ai biết.
    """
    if not isinstance(text, str):
        raise TypeError(f"redact() nhận str, không phải {type(text).__name__}")

    hits: dict[str, int] = {}

    def _sub(match: re.Match[str]) -> str:
        for kind in _TOKEN:
            if match.group(kind) is not None:
                hits[kind] = hits.get(kind, 0) + 1
                return _TOKEN[kind]
        return match.group(0)  # pragma: no cover — alternation luôn khớp đúng một nhánh

    return RedactionResult(text=_PATTERN.sub(_sub, text), hits=hits)
