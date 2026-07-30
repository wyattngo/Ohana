"""A8 — pending_reply.escalation_reasons + CHECK (adopt-plan §4 A8 · design §5.5 · C5, I10).

Cột output mới của `policy_gate` sau khi xoá nhánh AUTO_SEND: mọi draft đều park, gate
chỉ còn quyết seller nhìn cái gì trước — danh sách lý do escalate, sắp theo severity rank
(agent/policy_gate.py::SEVERITY_RANK).

CHECK `escalation_reasons_known` NGUYÊN VĂN design §5.5 — chặn typo ở TẦNG DB: label bẩn
sẽ đầu độc training set w§8.1, và CHECK là hàng rào mà raw SQL / script seed / data-fix
không lách được (cùng triết lý cap persona_md). Bảy giá trị khớp SEVERITY_RANK trong
policy_gate — lệch nhau là contract test C5 đỏ.

DEFAULT '{}' cho row cũ lẫn row mới: draft thường (không cờ nào bật) là mảng rỗng, vẫn
park như mọi draft — khác biệt chỉ ở thứ tự inbox (§7 sort ESCALATE lên đầu).

I14: chạy bằng `ohana_migrator`; cột mới thừa kế grant bảng có sẵn.
Reversible thật: DROP cột là mất dữ liệu reasons của draft đã ghi — chấp nhận, downgrade
A8 nghĩa là quay về gate cũ vốn không sinh dữ liệu này.

Revision ID: a8_escalation_reasons
Revises: a7_debounce
"""

from __future__ import annotations

from alembic import op

revision = "a8_escalation_reasons"
down_revision = "a7_debounce"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE pending_reply ADD COLUMN escalation_reasons TEXT[] NOT NULL DEFAULT '{}'"
    )
    op.execute("""
        ALTER TABLE pending_reply ADD CONSTRAINT escalation_reasons_known CHECK (
            escalation_reasons <@ ARRAY[
                'sensitive_intent','injection_attempt','data_unavailable',
                'media_content','window_closed','cost_cap','window_unknown'
            ]::text[]
        )
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE pending_reply DROP CONSTRAINT escalation_reasons_known")
    op.execute("ALTER TABLE pending_reply DROP COLUMN escalation_reasons")
