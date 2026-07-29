"""Type gate cho I3 và I4.

`llm_client.generate()` chỉ nhận `Scrubbed`. Truyền `str` thường ⇒ mypy fail.
CI đã bật mypy strict, nên I3/I4 là lỗi build chứ không phải mục review.

GIỚI HẠN — nói thẳng để không ai tưởng type là kín: `NewType` chặn *truyền sai*,
không chặn *ép kiểu bừa*. `Scrubbed("raw")` gọi ở đâu cũng chạy. Lớp thứ hai là
`tests/contract/test_construction_sites.py`, bảo đảm hai constructor này chỉ
xuất hiện trong `agent/pii.py`. Hai lớp cộng lại mới kín.
"""

from __future__ import annotations

from typing import NewType

# Payload đã qua PII redactor — điều kiện để rời máy lên LLM (workflow §5).
Scrubbed = NewType("Scrubbed", str)

# User-generated content đã bọc tag chống prompt injection (workflow §5).
Wrapped = NewType("Wrapped", str)
