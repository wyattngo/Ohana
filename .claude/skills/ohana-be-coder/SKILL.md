---
name: ohana-be-coder
description: Code, sửa bug, refactor trong codebase Ohana BE (repo ohana-ai — Python/FastAPI/PostgreSQL/Alembic). Dùng khi Wyatt nói "code Ohana", "làm A1", "sửa webhook", "implement OHB-x", "fix bug Ohana", hoặc khi chạm file trong agent/, api/, db/, channels/, retrieval/, parsing/. KHÔNG dùng cho ONFA (CI3/PHP) hay DrNick.
---

# ohana-be-coder

## Thẩm quyền

`docs/ohana-be-design.md` là hợp đồng.

1. Doc và code mâu thuẫn ⇒ **sửa code**, không sửa doc.
2. `ohana-be-design.md` và `backend-workflow.md` mâu thuẫn ⇒ **dừng, hỏi người**.
3. Trước khi sửa file nào, đọc `§ref` tương ứng.

Tên bảng ở design `§5` là **trạng thái đích**. Code hiện dùng tên phẳng trong `public`
(`pending_reply`, `webhook_event_log`, `shops`). Ánh xạ: `docs/adopt-plan.md` §1.
Chỉ `embeddings` → `platform.corpus` đã di cư.

Linear (`OHB-*`) giữ **trạng thái**. Doc giữ **sự thật**. Không chép doc vào issue.

## Trước khi viết dòng đầu tiên

- [ ] File sẽ chạm thuộc tier nào (`.work-tiers`)?
- [ ] `stop` ⇒ **DỪNG**, mô tả kế hoạch, chờ duyệt. Không code trước.
- [ ] Đọc `§ref` liên quan
- [ ] Bất biến nào bị chạm (I1–I16)?

## 16 bất biến

| # | Bất biến | Cưỡng chế bởi |
|---|---|---|
| I1 | Luồng A và B không chung process | `importlinter` + entrypoint riêng |
| I2 | `svc_ohana_ai` không đọc được dữ liệu shop | Postgres role, zero grant trên `public` |
| I3 | Mọi payload lên LLM qua PII filter | type `Scrubbed` + mypy |
| I4 | Mọi user content được wrap | type `Wrapped` + mypy |
| I5 | SDK provider chỉ trong `agent/providers/` | `importlinter` forbidden |
| I6 | Hàm service không nhận `shop_id` trần | type `ShopContext` + mypy |
| I7 | `webhook_seen` + `outbox` ghi **một** câu | CTE §6.1 |
| I8 | Cost reserve atomic, điều kiện trong `WHERE` | §6.5 |
| I9 | Draft giữ snapshot tầng 1 + `persona_id` tại T0 | `NOT NULL` |
| I10 | Không có nhánh tự động gửi | không tồn tại code path |
| I11 | Embed đúng prefix `query:`/`passage:` | hai hàm riêng |
| I12 | Window FB/IG tính tại chỗ | §6.8 |
| I13 | Mọi claim có timeout, reaper gỡ được | R3 §6.9 · R4 §6.10 |
| I14 | Bảng mới phủ bởi default privileges | `ALTER DEFAULT PRIVILEGES FOR ROLE` |
| I15 | MCP nối bằng `mcp_readonly` | role riêng |
| I16 | Langfuse chỉ nhận `Scrubbed` | hook trong `agent/llm_client.py` |

## 10 câu SQL giữ NGUYÊN VĂN

`§6.1` `§6.2` `§6.3` `§6.4` `§6.5` `§6.5b` `§6.6` `§6.7` `§6.9` `§6.10`

Viết lại thành nhiều câu = **bug im lặng**:

- Tách §6.1 thành hai `INSERT` ⇒ draft đôi, mà `webhook_event_log` vẫn trông đúng
- Bỏ `IS NULL` khỏi §6.3 ⇒ hai draft cho một hội thoại
- Check-rồi-update ở §6.5 ⇒ race, vượt cost cap
- Thiếu §6.9 ⇒ conversation **im lặng vĩnh viễn**

Muốn refactor mấy câu này ⇒ **DỪNG, hỏi người**.

## Bẫy Alembic — đọc kỹ

**Alembic PHẢI chạy bằng role `ohana_migrator`.**

```
DATABASE_URL=postgresql+psycopg://ohana_migrator:PW@localhost:5432/ohana alembic upgrade head
```

`ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator` chỉ phủ bảng **do role đó tạo**. Chạy
bằng owner `ohana` một lần là mọi bảng sinh ra sau đó rơi ra ngoài I2 — **và không có gì
báo lỗi**. Đây là lỗi nguy hiểm nhất trong repo này.

`autogenerate` **không thấy** `GRANT`, `CREATE ROLE`, partial index, `CHECK`. Bốn thứ đó
viết tay bằng `op.execute()`, review bằng mắt trước khi commit.

Role tạo bằng `scripts/bootstrap_roles.py`, **không** bằng migration — role là cấp cluster.

## Cấm

- Redis / RabbitMQ / SQS — `outbox` là queue
- Vector store cho AI Seller
- `anthropic.embeddings` — **không tồn tại**, Anthropic không có embedding API
- `embed(texts, prefix=...)` — tham số tuỳ chọn là chỗ để quên
- `GRANT ON ALL TABLES` không kèm `ALTER DEFAULT PRIVILEGES`
- Hàm service nhận `shop_id` trần
- Claim nào không có reaper gỡ
- Chạy Alembic bằng role không phải `ohana_migrator`
- Lưu token trong `localStorage`
- Chunk corpus > 480 token
- Gọi API lấy messaging window cho FB/IG
- Khôi phục nhánh `AUTO_SEND` trong `policy_gate.py`

## Sau khi sửa — khuôn báo cáo

```
## Đã sửa
<file:line> — <thay đổi gì>

## Bất biến chạm
<I-số> — <vẫn đúng vì...>

## Verify
$ <lệnh đã chạy>
<kết quả THẬT>

## Chưa verify
<những gì chưa chứng minh được>
```

**Không** báo "xong" khi chưa chạy lệnh verify. **Không** dán kết quả kỳ vọng thay cho
kết quả thật.

## Verify tối thiểu

```bash
ruff check . --no-cache && ruff format --check . --no-cache
mypy app agent retrieval parsing db bridge tools api auth
lint-imports
pytest -q
```

Chạm `db/migrations/` ⇒ thêm `pytest tests/contract/ -q`.
Chạm `prompt|persona|rules|corpus` ⇒ thêm `pytest -m eval -q`.

## Threat model

**Cẩu thả, không phải ác ý.** Phòng thủ đúng là *structural* — tách quyền, type, boundary.
Không đề xuất HMAC, chữ ký, hay verify mật mã: actor là Wyatt + Claude Code trong session
Wyatt kiểm soát.
