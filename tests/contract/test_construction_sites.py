"""Lớp 2 của type gate I3/I4 (A3 — xem docstring `agent/types.py`).

`NewType` chặn *truyền sai* nhưng không chặn *ép kiểu bừa*: `Scrubbed("raw")` gọi ở
đâu cũng chạy. Test này đảm bảo hai constructor chỉ xuất hiện trong `agent/pii.py`
— tức mọi giá trị mang dấu `Scrubbed`/`Wrapped` THẬT SỰ đã đi qua redact/escape.
Hai lớp (mypy + test này) cộng lại mới kín.

Quét text thô thay vì import graph: kẻ vi phạm điển hình là một dòng
`Scrubbed(raw_text)` chèn vội — nó hợp lệ với mypy và không đổi import nào.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]

# Mọi package production (khớp root_packages của import-linter trong pyproject).
_PACKAGES = (
    "agent",
    "api",
    "app",
    "auth",
    "bridge",
    "channels",
    "db",
    "parsing",
    "retrieval",
    "tools",
)

_ALLOWED = {
    _ROOT / "agent" / "pii.py",  # chỗ duy nhất được tạo Scrubbed/Wrapped
    _ROOT / "agent" / "types.py",  # file định nghĩa — docstring nhắc `Scrubbed("raw")`
}

_CONSTRUCTOR = re.compile(r"\b(?:Scrubbed|Wrapped)\(")


def test_scrubbed_wrapped_constructed_only_in_pii() -> None:
    offenders: list[str] = []
    for pkg in _PACKAGES:
        for path in sorted((_ROOT / pkg).rglob("*.py")):
            if path in _ALLOWED:
                continue
            if _CONSTRUCTOR.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(_ROOT)))
    assert offenders == [], (
        f"Scrubbed()/Wrapped() gọi ngoài agent/pii.py: {offenders} — "
        "đi qua scrub()/wrap() thay vì ép kiểu tay"
    )
