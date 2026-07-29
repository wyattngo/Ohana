"""Tra cứu `shops` — module RIÊNG, cố ý tách khỏi `db/repos.py` (spec 11 S1).

**Vì sao không nằm chung `db/repos.py`.** Gate ranh giới của spec 07
(`tests/test_chat_endpoint.py::test_chat_module_cannot_reach_the_customer_send_path`) cấm
`api/chat.py` với tới đường gửi khách, và nó đi theo BAO ĐÓNG import chứ không chỉ import
trực tiếp. `auth/identity.py` cần tra `shops`; nếu nó import `db.repos` thì bao đóng thành
`api.chat → auth.identity → db.repos → db.models.PendingReply` — tức đường chat nội bộ nối
tới module sở hữu hàng đợi gửi khách, và gate ĐỎ.

Gate đúng, không phải nhiễu: tầng auth không có lý do gì để phụ thuộc vào module chứa hàng
đợi duyệt-gửi. Tách ra làm phụ thuộc hẹp lại đúng bằng thứ nó thật sự cần (`Shop`).

⚠️ Đừng "dọn dẹp" bằng cách gộp file này ngược vào `db/repos.py`. Nó sẽ lại đỏ gate, và lần
đó người sửa sẽ không có ngữ cảnh này.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Shop


class ShopRepo:
    """Repo DUY NHẤT không nhận `shop_scope`, và đó là đúng.

    Mọi repo khác scope theo một shop đã biết; repo này trả lời câu hỏi ĐỨNG TRƯỚC điều đó —
    "shop này có thật và còn hoạt động không?". Không thể scope theo chính thứ đang cần xác
    thực.

    Vì vậy nó KHÔNG được dùng để đọc dữ liệu nghiệp vụ. Thêm ở đây một method trả row của
    bảng khác là mở đường vòng qua tenant scope — đúng loại lỗ mà các repo `shop_scope` sinh
    ra để chặn.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_active(self, shop_id: str) -> Shop | None:
        """Shop ĐANG HOẠT ĐỘNG, hoặc None.

        Gộp "không tồn tại" và "tồn tại nhưng bị treo" thành CÙNG một None là có chủ đích:
        caller (đường auth) chỉ cần biết "token này còn dùng được không", và phân biệt hai ca
        đó ở tầng HTTP sẽ nói cho kẻ tấn công biết `shop_id` nào là thật.

        `status != 'active'` ⇒ None (Wyatt ký fail-closed 2026-07-20). Nếu chỉ kiểm tồn tại,
        nút suspend không có hiệu lực nào cho tới khi token hết hạn 24h — trong khi người bấm
        nút tin rằng nó đã cắt ngay.
        """
        stmt = select(Shop).where(Shop.id == shop_id).where(Shop.status == "active")
        return (await self._session.execute(stmt)).scalar_one_or_none()
