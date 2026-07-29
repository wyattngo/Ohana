# Kế hoạch áp `ohana-be-design.md` vào repo

> Repo `ohana-ai` **chính là** Ohana BE.
> `ohana-be-design.md` là **trạng thái đích**; doc này là đường đi từ hiện tại tới đó.

---

## 1 · Đã có gì, thiếu gì

### Đã khớp v4 — không đụng

| Có sẵn | v4 | Ghi chú |
|---|---|---|
| `webhook_event_log` | `seller.webhook_seen` | đã là sổ idempotency, phạm vi nền tảng |
| `pending_reply` | `seller.draft` | đã có `snapshot`, `expires_at`, `label` + CHECK |
| `embeddings` | `platform.corpus` | đã 1024 chiều (migration 0004) |
| `channels/zalo/*` | B2 | signature canonical, replay window, constant-time |
| `agent/pii.py` | B3 | một-lượt alternation, neo dải số, token có nhãn |
| `tools/shop_kb.py` | tầng 2 | |
| composite FK ghim `shop_id` | — | **v4 không có** — cô lập tenant, bổ sung cho I2 |
| `zalo_oa_tokens` | — | **v4 thiếu bảng này**, phải thêm vào doc |

### Thật sự thiếu — đây là toàn bộ việc còn lại

| # | Thiếu | Bất biến | Ước lượng |
|---|---|---|---|
| G1 | `outbox` + `SKIP LOCKED` — **hiện không có queue nào** | I7 · §6.1 §6.2 | 1 revision + ~80 dòng |
| G2 | debounce claim + reaper R3/R4 | I13 · §6.3 §6.9 §6.10 | ~120 dòng |
| G3 | `cost_budget` + `cost_reservation` | I8 · §6.5 §6.5b | 1 revision + ~60 dòng |
| G4 | Tách quyền luồng A ↔ luồng B | **I1 I2 I14** | xem §3 |
| G5 | Type `Scrubbed` / `Wrapped` / `ShopContext` | I3 I4 I6 | ~40 dòng |
| G6 | `trace_id` xuyên webhook→outbox→draft→llm_turn | — | 1 revision |

Sáu mục. Không phải chín bước.

---

## 2 · Hai quyết định nền

**Alembic là công cụ migration.** Repo có 10 revision với lịch sử thật và CI xanh. `autogenerate` không thấy `GRANT`, `CREATE ROLE`, partial index, `CHECK` — nên bốn thứ đó viết tay bằng `op.execute()` và review bằng mắt trước khi commit. Không autogenerate cho DDL quyền.

**`policy_gate.py` giữ nguyên cấu trúc.** Bảng precedence trong đó chính là C5 severity rank — phần khó đã viết xong. Việc cần làm: xoá nhánh `AUTO_SEND` (I10 cấm auto-send ở phase 1), giữ `frozenset` blocklist, đổi output thành `escalation_reasons: list[str]` khớp `CHECK escalation_reasons_known`. Xoá luôn `DEFAULT_CONFIDENCE_THRESHOLD` — hằng số chết gây hiểu nhầm là còn nhánh tự động.

## 3 · G4 — cách rẻ để có I1/I2 mà không di cư 10 bảng

v4 nói ba schema `core` / `seller` / `platform`. Di cư 10 bảng đang có = đổi mọi model, mọi query, mọi test. Đắt và rủi ro.

**Quan sát:** luồng A (`api/chat.py`) chỉ cần **corpus**. Theo `w§1` nó không chạm dữ liệu shop.

Nên chỉ cần tách **một** bảng:

```python
# revision: platform schema + role
op.execute("CREATE SCHEMA IF NOT EXISTS platform")
op.execute("ALTER TABLE embeddings SET SCHEMA platform")

op.execute("CREATE ROLE svc_ohana_ai LOGIN PASSWORD :a_pw")
op.execute("CREATE ROLE svc_seller   LOGIN PASSWORD :b_pw")
op.execute("CREATE ROLE mcp_readonly LOGIN PASSWORD :ro_pw")

# I2 — luồng A CHỈ thấy platform. KHÔNG grant gì trên public.
op.execute("GRANT USAGE ON SCHEMA platform TO svc_ohana_ai")
op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA platform TO svc_ohana_ai")

op.execute("GRANT USAGE ON SCHEMA public, platform TO svc_seller")
op.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO svc_seller")

op.execute("GRANT USAGE ON SCHEMA public, platform TO mcp_readonly")
op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public, platform TO mcp_readonly")

# I14 — bảng TƯƠNG LAI. Thiếu khối này thì I2 hỏng im lặng ở bảng thứ N+1.
op.execute("ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA platform "
           "GRANT SELECT ON TABLES TO svc_ohana_ai")
op.execute("ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA public "
           "GRANT SELECT, INSERT, UPDATE ON TABLES TO svc_seller")
op.execute("ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA public, platform "
           "GRANT SELECT ON TABLES TO mcp_readonly")
```

Kết quả: `svc_ohana_ai` có **zero** quyền trên `public` ⇒ `SELECT * FROM pending_reply` là permission denied. **I2 đúng, một bảng di cư thay vì mười.**

`core` / `seller` để sau, khi có lý do độc lập. Đừng di cư chỉ để khớp tên trong doc.

### I1 — tách process

Hiện `app/main.py` mount cả `api/chat.py` (luồng A) lẫn `api/webhook.py`, `api/inbox.py` (luồng B). Cùng một process.

Việc: hai entrypoint, cùng codebase, khác `DATABASE_URL`.

```
app/main_ohana_ai.py   → chỉ mount api/chat        → DATABASE_URL=svc_ohana_ai
app/main_seller.py     → mount webhook + inbox     → DATABASE_URL=svc_seller
app/worker_seller.py   → 3 loop                     → DATABASE_URL=svc_seller
```

Cộng `importlinter` contract cấm `api.chat` import bất cứ gì thuộc luồng B. **Đây là chỗ I1 thật sự được cưỡng chế**, không phải ở tên thư mục.

---

## 4 · Thứ tự

| # | Việc | Gate |
|---|---|---|
| A1 | Revision: role + `platform` schema + default privileges | `test_i14` 4 xanh |
| A2 | `importlinter` contract I1 + I5 vào `pyproject.toml` + CI | `lint-imports` xanh |
| A3 | Type `Scrubbed`/`Wrapped`/`ShopContext` + bọc `agent/pii.py` | mypy strict xanh |
| A4 | Tách entrypoint A/B/worker | smoke: A query `pending_reply` ⇒ denied |
| A5 | Revision: `outbox` + `trace_id` · §6.1 CTE · §6.2 claim | C1 |
| A6 | Revision: `cost_budget` + `cost_reservation` · §6.5 §6.5b | — |
| A7 | debounce claim + reaper R3/R4 | C2 |
| A8 | `policy_gate` → `escalation_reasons` | C5 |

A1–A3 không đụng logic nghiệp vụ, làm được ngay. A4 là chỗ rủi ro nhất — tách process trên code đang chạy.

---

## 5 · Docker-compose — thêm, không thay

CI vẫn `pip install -e .[dev]` → `alembic upgrade head` → `pytest`. **Không đụng.**

`docker-compose.yml` chỉ để **local dev**:
- `postgres` (pgvector) — thay cho Postgres cài tay
- `langfuse` + `langfuse-db` — self-host, điều kiện để golden set C4 ở lại trong nước

Bỏ service `svc-*` và `worker-*` khỏi compose cho tới sau A4 — chưa có entrypoint riêng thì chưa có gì để chạy.

---

## 6 · Cần quyết

| # | Câu hỏi | Chặn |
|---|---|---|
| Q1 | Giữ **Python 3.11** hay nâng 3.12? CI đang pin 3.11. Nâng là PR riêng, không ghép vào A1. | — |
| Q2 | `adp-lint.sh` vẫn nằm trong CI step 10. Đã quyết cắt ADP — gỡ step này luôn hay để? | A2 |
| Q3 | Worktree tôi audit có `docs/tasks/23-LintNamespacedIds`, còn spec 23 bạn gửi là `EngineTrustHarden`. **Snapshot cũ hơn HEAD.** Đối chiếu lại trước khi áp kế hoạch này. | tất cả |
| Q4 | `zalo_oa_tokens` chưa có trong v4 — thêm vào design doc `§5`. | — |

**Q3 quan trọng nhất.** Toàn bộ đánh giá "còn thiếu gì" ở §1 dựa trên snapshot đó. Nếu HEAD đã có `outbox` hoặc cost cap thì danh sách G1–G6 ngắn hơn nữa.
