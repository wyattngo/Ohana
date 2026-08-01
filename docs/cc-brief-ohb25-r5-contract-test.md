# CC Brief · OHB-25 — R5 contract test (song song `test_c2_scheduler`)

**Frozen** · form `ohana-be-coder`.

## Bối cảnh

R5 (`_REAP_R5_STUCK_SEND_CLAIM`) đã ship trong PR #5 (OHB-23/22/7 land `4b4583f`): NULL
`sent_claimed_at` khi kẹt >5', quét qua partial index `idx_pending_reply_send_claim`.

**Gap:** contract test `tests/contract/test_c2_scheduler.py` canh HÌNH DẠNG R1–R4 bằng
psycopg thô (SQL nguyên văn) — R5 chưa có counterpart. Refactor sau đụng
`pending_reply` hoặc reaper loop sẽ **không thấy R5 regress**.

**OHB-25 (Backlog):** thêm contract test cho R5 cùng khuôn — seed `pending_reply` với
`sent_claimed_at` cũ, chạy reap, assert NULL + `status='approved'` (row quay lại claimable).
Bonus: 2-worker SKIP LOCKED (C2 send-side) — hai send-worker cùng claim ⇒ đúng một thắng.

## Chạm bất biến

* **I13** (mọi claim có timeout, reaper gỡ được) — gate hoá bằng contract test song song
  R1–R4. Đây là mục đích chính của task.
* **KHÔNG** chạm code sản xuất (repos/models/workers/migrations). Chỉ thêm 1 file test.

## Việc

Tạo `tests/contract/test_c3_send_worker.py` — cùng khuôn `test_c2_scheduler.py`:

1. **SQL nguyên văn** — copy `_REAP_R5_STUCK_SEND_CLAIM` và `_CLAIM_SEND` từ
   `db/repos.py` (KHÔNG import). Cùng lý do test_c2: contract test canh **hình dạng**
   câu lệnh — import từ code sản xuất là mất khả năng bắt drift SQL.
2. **Fixture reuse** `requires_dsn`, `wipe_tenant`, `seed_tenant` từ `conftest.py`.
   `SHOP`/`CHANNEL` mới (ví dụ `c3test_shop`/`c3test`) để không đụng test_c2.
3. **Test 1 · `test_r5_reaper_frees_stuck_send_claim`** (main gate):
   - Seed 1 `pending_reply` với `status='approved'`, `sent_claimed_at=NULL`.
   - `svc_b` chạy `_CLAIM_SEND` → verify `sent_claimed_at IS NOT NULL`.
   - `svc_b` chạy R5 (chưa quá 5') → verify claim vẫn còn (R5 không đụng worker sống).
   - `migrator` backdate `sent_claimed_at = now() - interval '6 minutes'`.
   - `svc_b` chạy R5 → verify `sent_claimed_at IS NULL` + `status='approved'` (KHÔNG đổi).
   - Re-claim: `svc_b` chạy `_CLAIM_SEND` → verify claim lại được (hệ tự hồi).
4. **Test 2 · `test_c2_send_side_two_workers_one_claim`** (bonus SKIP LOCKED):
   - Seed 1 `pending_reply` `status='approved'`.
   - 2 connection `SVC_B_DSN` cùng chạy `_CLAIM_SEND` — bên thắng RETURNING 1 row,
     bên thua 0 row (SKIP LOCKED + `sent_claimed_at IS NULL` trong WHERE).

## Chống drift

* **KHÔNG** import từ `db/repos.py` — copy SQL nguyên văn (contract test canh shape).
* **KHÔNG** thêm/sửa `wipe_tenant` — `sent_log` đã xoá từ OHB-24; `pending_reply` cũng
  đã có. Không thêm bảng mới ⇒ không đụng conftest.
* **KHÔNG** đổi `_CLAIM_SEND` / `_REAP_R5_STUCK_SEND_CLAIM` trong `db/repos.py`. Test
  này là bản sao câm — sai lệch ⇒ test đỏ ⇒ ép sửa cho khớp (chủ đích).
* Thứ tự chạy: `_CLAIM_SEND` thắng cả 1 row RETURNING (dùng `.fetchone()` không phải
  `.fetchall()` — LIMIT 20 nhưng seed 1 ⇒ tối đa 1 row; bên thua fetchone → None).

## Verify

```bash
export MIGRATOR_DSN=postgresql://ohana_migrator:PW@localhost:5433/ohana
export SVC_A_DSN=postgresql://svc_ohana_ai:PW@localhost:5433/ohana
export SVC_B_DSN=postgresql://svc_seller:PW@localhost:5433/ohana
export MCP_RO_DSN=postgresql://mcp_readonly:PW@localhost:5433/ohana

pytest tests/contract/test_c3_send_worker.py -q
pytest tests/contract/ -q
ruff check tests/contract/test_c3_send_worker.py
mypy tests/contract/test_c3_send_worker.py
```

Kỳ vọng: 2 test GREEN, full contract suite không regress.

## Rollback

Không risk sản xuất (test-only). Rollback = `git revert`. Không migration, không schema
change, không grants.
