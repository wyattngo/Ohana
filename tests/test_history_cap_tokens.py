"""Live measurement — đo tỉ lệ ký tự→token THẬT cho tiếng Việt (ISSUE-023, ISSUE-022).

`@pytest.mark.live` ⇒ deselect khỏi `pytest -m 'not live'`, tức khỏi CI và mọi GATE_FULL.
Chạy tay khi có key:

    .venv/bin/python -m pytest tests/test_history_cap_tokens.py -m live -q -s

**Vì sao file này tồn tại.** `HISTORY_MAX_CHARS = 4000` và cap persona 2000 được suy từ ước
lượng "ký tự tiếng Việt → token ≈ 3.3", CHƯA CHẠY tokenizer thật lần nào (ISSUE-023/022). Con
số 3.3 là giấy tờ; nếu tỉ lệ thật lệch đáng kể thì hoặc ngân sách token vỡ (cap quá rộng) hoặc
AI mất ngữ cảnh sớm (cap quá hẹp) — cả hai KHÔNG có triệu chứng, chỉ làm trả lời tệ đi âm thầm.
Đúng họ silent-wrong với `_DeterministicDevEmbedder`: không crash, không đỏ test thường.

**Cái test này đo, và cái nó KHÔNG đo.**
  * ĐO (Q1): tỉ lệ ký tự→token trên tokenizer Llama-3.3 THẬT, qua `usage.prompt_tokens` của
    provider — thứ tokenizer offline không lấy được (Llama-3.3 tokenizer là gated HF repo).
  * KHÔNG ĐO (Q2): phân phối token/message trên hội thoại Zalo THẬT. Cần ≥50 hội thoại thật;
    hiện 0 shop, PRE-004 blocked. ISSUE-023 CHỈ đóng được khi có Q2 — test này de-risk Q1, KHÔNG
    tự đóng issue. Đừng trích số ở đây như "đã chốt cap".

Phương pháp: delta. Đo `prompt_tokens` ở hai độ dài history khác nhau, TRỪ nhau để khử phần
overhead cố định (BOS, role marker, chat template, system prompt) — phần còn lại là token do
chính khối ký tự tiếng Việt thêm vào. Một phép đo tuyệt đối sẽ lẫn overhead vào tỉ lệ.
"""

from __future__ import annotations

import os

import pytest

from agent.orchestrator import HISTORY_MAX_CHARS, HISTORY_MAX_MESSAGES

pytestmark = pytest.mark.live

# Câu hội thoại thương mại tiếng Việt CÓ DẤU, giống tin nhắn Zalo thật (nhiều dấu = nhiều
# token hơn ASCII — đúng trường hợp xấu ta cần đo). Nguyên gốc, không trích nguồn nào.
_VN_SENTENCE = (
    "Chào shop, cho em hỏi mẫu áo khoác này còn size M màu xanh rêu không ạ, "
    "em ở Đà Nẵng thì phí ship khoảng bao nhiêu và mấy ngày tới nơi vậy shop. "
)


def _require_key() -> str:
    key = os.environ.get("TOGETHER_API_KEY", "").strip()
    if not key:
        pytest.skip("TOGETHER_API_KEY chưa set — live measurement bỏ qua (không phải lỗi)")
    return key


def _text_of_length(n: int) -> str:
    """Chuỗi tiếng Việt có dấu dài đúng `n` ký tự (lặp câu mẫu rồi cắt)."""
    reps = (n // len(_VN_SENTENCE)) + 1
    return (_VN_SENTENCE * reps)[:n]


@pytest.mark.asyncio
async def test_measure_vietnamese_chars_per_token() -> None:
    """Đo tỉ lệ ký tự→token thật, đối chiếu với giả định 3.3 đứng sau cap history/persona.

    Không assert một "con số đúng" — đó là quyết định của Wyatt sau khi có Q2. Chỉ:
      1. IN tỉ lệ đo được + token ngụ ý của cap 4000 ký tự, để ghi vào ISSUE-023.
      2. FAIL nếu tỉ lệ lệch tới mức phá giả định (band rộng 1.5–6.0 ký tự/token) — bắt lỗi thô
         kiểu tokenizer đếm theo byte, hoặc tiếng Việt tệ hơn nhiều so với 3.3.
    """
    _require_key()
    from agent.providers.together_client import TogetherClient

    client = TogetherClient()
    short_len, long_len = 400, 2000

    async def prompt_tokens(history_chars: int) -> int:
        step = await client.step(
            [
                {"role": "system", "content": "Trả lời cực ngắn bằng tiếng Việt."},
                {"role": "user", "content": _text_of_length(history_chars)},
            ],
            max_tokens=1,  # rẻ nhất: ta chỉ cần prompt_tokens, không cần câu trả lời
        )
        assert step.usage, "thiếu usage — provider không trả token count, không đo được"
        pt = step.usage.get("prompt_tokens", 0)
        assert pt > 0, "prompt_tokens = 0, response shape sai"
        return pt

    tok_short = await prompt_tokens(short_len)
    tok_long = await prompt_tokens(long_len)

    delta_chars = long_len - short_len
    delta_tokens = tok_long - tok_short
    assert delta_tokens > 0, f"token không tăng theo ký tự (short={tok_short} long={tok_long})"

    chars_per_token = delta_chars / delta_tokens
    implied_cap_tokens = round(HISTORY_MAX_CHARS / chars_per_token)

    print(
        "\n--- ISSUE-023 measurement (Q1: ratio only; Q2 distribution vẫn blocked) ---\n"
        f"  delta {delta_chars} ký tự → {delta_tokens} token\n"
        f"  ĐO ĐƯỢC: {chars_per_token:.2f} ký tự/token  (giả định cũ: 3.3)\n"
        f"  HISTORY_MAX_CHARS={HISTORY_MAX_CHARS} ⇒ ~{implied_cap_tokens} token thật "
        f"(giấy tờ cũ nói ~1800)\n"
        f"  HISTORY_MAX_MESSAGES={HISTORY_MAX_MESSAGES}\n"
        "  → ghi số này vào ISSUE-023; CHƯA đóng: còn cần phân phối token/message trên hội "
        "thoại thật (Q2).\n"
    )

    assert 1.5 <= chars_per_token <= 6.0, (
        f"tỉ lệ {chars_per_token:.2f} ký tự/token nằm ngoài band hợp lý 1.5–6.0 — "
        f"giả định 3.3 đứng sau cap history/persona có thể sai nghiêm trọng, "
        f"ngân sách token đang dựa trên số không đúng"
    )
