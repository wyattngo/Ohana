"""Type gate cho I6.

`shop_id: int` là kiểu CẤM trong signature tầng service và repo. Chỉ middleware
dựng được `ShopContext`, và chỉ sau khi kiểm quyền sở hữu shop.

Vì sao kiểm ở middleware là chưa đủ: kiểm đúng một lần không ràng buộc được
call-site thứ hai. Hàm nhận `int` thì ai gọi từ chỗ khác cũng không có gì chặn.
Hàm nhận `ShopContext` thì mypy chặn.

Endpoint `/v1/drafts/{id}` KHÔNG mang shop_id: middleware nạp draft, đọc
`draft.shop_id`, kiểm quyền, rồi mới dựng context. Để client truyền cả draft_id
lẫn shop_id là hai nguồn sự thật và một đường IDOR.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ShopContext:
    """Dựng bởi middleware auth, sau khi xác thực quyền sở hữu shop."""

    shop_id: str
    account_id: str
    trace_id: UUID
