"""Zalo webhook envelope shapes (spec 17 P3, `GD0-ZALO`).

`TypedDict` chứ KHÔNG Pydantic model — Pydantic validate chặt sẽ raise trên field lạ, mà Zalo
thêm field bất cứ lúc nào (vd `attachments` cho photo/gif/file, `user_id_by_app` cho anonymous
event). TypedDict + `dict.get()` ở call-site mềm hơn: đọc field cần, bỏ qua field thừa.

`total=False` vì không field nào chắc chắn có mọi event — parse_inbound validate thủ công đúng
field cho đúng event. Shape lock từ docs Zalo canonical 2026-07-24 (screenshot Wyatt).

⚠️ KHÔNG có `oa_id` top-level — chỉ `app_id`. `oa_id` suy từ sender.id/recipient.id tùy event.
"""

from __future__ import annotations

from typing import TypedDict


class ZaloParty(TypedDict, total=False):
    id: str


class ZaloMessage(TypedDict, total=False):
    text: str
    msg_id: str
    # `attachments` + field media khác cố ý KHÔNG khai — spec 17 chỉ text; TypedDict total=False
    # cho phép chúng tồn tại mà không phải liệt kê (chống drift khi Zalo thêm loại tin).


class ZaloWebhookPayload(TypedDict, total=False):
    app_id: str
    sender: ZaloParty
    recipient: ZaloParty
    event_name: str
    message: ZaloMessage
    timestamp: str
