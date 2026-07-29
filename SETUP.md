# Setup — Ohana BE

Thêm lớp cưỡng chế bất biến vào codebase đang chạy. Khoảng nửa ngày, không đụng logic nghiệp vụ.

Đọc trước: `docs/ohana-be-design.md` §1 (16 bất biến) · `docs/adopt-plan.md` §1 (đã có gì, thiếu gì).

---

## §0 · Đối chiếu HEAD — làm trước, chặn tất cả

Snapshot tôi audit có `docs/tasks/23-LintNamespacedIds`, còn spec 23 bạn gửi là `EngineTrustHarden`. **Worktree cũ hơn HEAD.** Danh sách "còn thiếu" có thể ngắn hơn thực tế.

```bash
cd <repo>
git log --oneline -20
ls db/migrations/versions/
alembic current

# Sáu lỗ hổng — cái nào đã có rồi?
grep -rnE "outbox|cost_budget|cost_reservation|debounce|trace_id|Scrubbed|ShopContext" \
     db/models.py agent/ api/ auth/ 2>/dev/null | grep -v test
```

Mỗi hit là một mục gạch khỏi `docs/adopt-plan.md` §1.

**Không chạy §2 trước khi xong §0.**

---

## §1 · Hạ tầng local — thêm, không thay

CI vẫn `pip install -e ".[dev]"` → `alembic upgrade head` → `pytest`. **Không đụng `.github/workflows/ci.yml`.**

App vẫn chạy trên host như hiện tại. Compose chỉ cấp **Postgres + Langfuse**.

```yaml
# docker-compose.yml  (mới)
name: ohana-dev

services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_USER: ohana
      POSTGRES_PASSWORD: ${POSTGRES_PW}
      POSTGRES_DB: ohana
    ports: ["5432:5432"]
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD","pg_isready","-U","ohana","-d","ohana"]
      interval: 5s
      retries: 10

  langfuse-db:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: ${LANGFUSE_DB_PW}
      POSTGRES_DB: langfuse
    volumes: [langfusedata:/var/lib/postgresql/data]

  langfuse:
    image: langfuse/langfuse:2          # KHÔNG dùng :3
    environment:
      DATABASE_URL: postgres://postgres:${LANGFUSE_DB_PW}@langfuse-db:5432/langfuse
      NEXTAUTH_URL: http://localhost:3000
      NEXTAUTH_SECRET: ${LANGFUSE_NEXTAUTH_SECRET}
      SALT: ${LANGFUSE_SALT}
      TELEMETRY_ENABLED: "false"
    ports: ["3000:3000"]
    depends_on: [langfuse-db]

volumes: {pgdata: {}, langfusedata: {}}
```

`langfuse:2` chứ không `:3` — v3 cần ClickHouse + Redis + MinIO, bốn service nữa cho dự án chưa có traffic.

Thêm vào `.env.example` (giữ nguyên format `[LIVE]`/`[PLANNED]` đang có):

```bash
# 8. LOCAL DEV INFRA  [LIVE]
POSTGRES_PW=
MIGRATOR_PW=
SVC_A_PW=
SVC_B_PW=
MCP_RO_PW=

# 9. LANGFUSE  [PLANNED — wire ở A3]  self-host, không dữ liệu nào rời hạ tầng
LANGFUSE_DB_PW=
LANGFUSE_NEXTAUTH_SECRET=
LANGFUSE_SALT=
LANGFUSE_HOST=http://localhost:3000
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
```

Sinh mật khẩu — **hex, không base64**:

```bash
for k in POSTGRES_PW MIGRATOR_PW SVC_A_PW SVC_B_PW MCP_RO_PW \
         LANGFUSE_DB_PW LANGFUSE_NEXTAUTH_SECRET LANGFUSE_SALT; do
  echo "$k=$(openssl rand -hex 24)"
done
```

base64 sinh `/` `+` `=` — cả ba vỡ connection URL. Hex thì không bao giờ.

```bash
docker compose up -d postgres
```

---

## §2 · Bootstrap role — script, KHÔNG phải migration

**Vì sao tách:** role là đối tượng cấp **cluster**, grant là cấp **database**. Nhét `CREATE ROLE` vào Alembic thì restore dump sang cluster mới là mất role, còn migration thì báo đã chạy rồi. Hai vòng đời khác nhau ⇒ hai chỗ.

```python
# scripts/bootstrap_roles.py
"""Tạo role cấp cluster. Chạy MỘT LẦN mỗi cluster, trước `alembic upgrade head`.

Không nằm trong Alembic vì role là cluster-level: restore dump sang cluster mới
sẽ mất role trong khi bảng schema_migration vẫn báo đã chạy.

Idempotent — chạy lại không sao.
"""

from __future__ import annotations

import os

import psycopg

ROLES = {
    "ohana_migrator": "MIGRATOR_PW",
    "svc_ohana_ai": "SVC_A_PW",
    "svc_seller": "SVC_B_PW",
    "mcp_readonly": "MCP_RO_PW",
}


def main() -> None:
    dsn = os.environ["SUPERUSER_DSN"]  # postgres://ohana:...@localhost:5432/ohana
    with psycopg.connect(dsn, autocommit=True) as conn:
        for role, env_key in ROLES.items():
            pw = os.environ[env_key]
            lit = "'" + pw.replace("'", "''") + "'"
            conn.execute(
                f"""
                DO $$ BEGIN
                  EXECUTE format('CREATE ROLE {role} LOGIN PASSWORD %L', {lit});
                EXCEPTION WHEN duplicate_object THEN
                  EXECUTE format('ALTER ROLE {role} LOGIN PASSWORD %L', {lit});
                END $$;
                """
            )
            print(f"role ok: {role}")
        conn.execute("GRANT CREATE ON SCHEMA public TO ohana_migrator")
        conn.execute("GRANT ohana_migrator TO CURRENT_USER")  # để migrator tạo được bảng


if __name__ == "__main__":
    main()
```

```bash
SUPERUSER_DSN="postgresql://ohana:$POSTGRES_PW@localhost:5432/ohana" \
  python scripts/bootstrap_roles.py
```

---

## §3 · A1 — revision Alembic: schema + grant

```bash
alembic revision -m "platform schema + role grants (A1)"
```

Nội dung `upgrade()`:

```python
def upgrade() -> None:
    # pgvector là extension cần superuser — đã có từ migration 0001, không tạo lại.

    op.execute("CREATE SCHEMA IF NOT EXISTS platform")
    op.execute("ALTER TABLE embeddings SET SCHEMA platform")

    # ── I2 · luồng A CHỈ thấy corpus ─────────────────────────────────
    # KHÔNG grant gì trên `public`. Đó là toàn bộ nội dung của I2:
    # `SELECT * FROM pending_reply` từ svc_ohana_ai ⇒ permission denied.
    op.execute("GRANT USAGE ON SCHEMA platform TO svc_ohana_ai")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA platform TO svc_ohana_ai")

    # ── luồng B ──────────────────────────────────────────────────────
    op.execute("GRANT USAGE ON SCHEMA public, platform TO svc_seller")
    op.execute("GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO svc_seller")
    op.execute("GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO svc_seller")

    # ── I15 · MCP đọc tất cả, ghi không gì ───────────────────────────
    op.execute("GRANT USAGE ON SCHEMA public, platform TO mcp_readonly")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public, platform TO mcp_readonly")

    # ── I14 · BẢNG TƯƠNG LAI ─────────────────────────────────────────
    # `ALL TABLES` ở trên chỉ phủ bảng ĐANG tồn tại. Thiếu khối này thì mọi
    # bảng của A5/A6 (outbox, cost_budget…) rơi ra ngoài — I2 hỏng im lặng
    # ở bảng thứ N+1, và không có gì báo.
    #
    # `FOR ROLE ohana_migrator` là bắt buộc: default privileges gắn với role
    # TẠO bảng. Chạy alembic bằng role khác ⇒ khối này không áp.
    for stmt in (
        "ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA platform "
        "GRANT SELECT ON TABLES TO svc_ohana_ai",
        "ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA public "
        "GRANT SELECT, INSERT, UPDATE ON TABLES TO svc_seller",
        "ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA public "
        "GRANT USAGE ON SEQUENCES TO svc_seller",
        "ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator IN SCHEMA public, platform "
        "GRANT SELECT ON TABLES TO mcp_readonly",
    ):
        op.execute(stmt)
```

Sửa model: `Embedding.__table_args__` thêm `{"schema": "platform"}`.

**Alembic PHẢI chạy bằng `ohana_migrator`:**

```bash
DATABASE_URL="postgresql+psycopg://ohana_migrator:$MIGRATOR_PW@localhost:5432/ohana" \
  alembic upgrade head
```

Chạy bằng `ohana` (owner) một lần là mọi bảng từ đó rơi ra ngoài I14, **và không có gì báo lỗi**. Đây là bẫy nguy hiểm nhất trong toàn bộ setup — thêm dòng này vào `CLAUDE.md`.

`downgrade()`: `op.execute("ALTER TABLE platform.embeddings SET SCHEMA public")` + `DROP SCHEMA platform`. Revoke thì bỏ qua — role vẫn còn ở cluster.

---

## §4 · Gate — `test_i14`

`tests/contract/test_i14_default_privileges.py`, dùng `psycopg` đồng bộ cho khớp stack hiện tại:

```python
PROBE = "public.probe_i14"   # bảng MỚI, tạo sau lần GRANT ở A1
```

Bốn assert:

| Test | Chứng minh |
|---|---|
| `svc_ohana_ai` đọc `probe_i14` ⇒ **denied** | I2 đúng với bảng tạo **sau** GRANT |
| `svc_seller` đọc `probe_i14` ⇒ **ok** | I14 — không cần GRANT tay |
| `mcp_readonly` đọc ok, ghi ⇒ **denied** | I15 |
| `svc_ohana_ai` đọc `pending_reply` ⇒ **denied** | I2 trên bảng đang có |

```bash
pytest tests/contract/test_i14_default_privileges.py -q
```

4 xanh ⇒ đóng `OHB-1`. Thêm 4 URL role vào `ci.yml` step Pytest.

---

## §5 · A2 — importlinter

Thêm vào `pyproject.toml`:

```ini
[tool.importlinter]
root_packages = ["api", "agent", "app", "channels", "db", "retrieval", "parsing", "tools", "bridge"]

[[tool.importlinter.contracts]]
name = "I1 · luồng A không thấy luồng B"
type = "forbidden"
source_modules = ["api.chat", "retrieval"]
forbidden_modules = ["api.webhook", "api.inbox", "agent.drafter", "channels", "bridge"]

[[tool.importlinter.contracts]]
name = "I5 · SDK provider chỉ trong agent.providers"
type = "forbidden"
source_modules = ["api", "app", "channels", "db", "retrieval", "parsing", "tools", "agent.drafter", "agent.pii", "agent.policy_gate"]
forbidden_modules = ["openai", "anthropic", "together", "langfuse"]
```

```bash
pip install import-linter && lint-imports
```

Đỏ ngay lần đầu là **bình thường và có ích** — nó chỉ đúng chỗ hai luồng đang dính nhau. Sửa từng cái, đó chính là nội dung A4.

Thêm step vào `ci.yml` sau bước Mypy.

---

## §6 · A3 — type gate

```python
# agent/types.py
Scrubbed = NewType("Scrubbed", str)
Wrapped  = NewType("Wrapped", str)
```

Đổi `agent/pii.py` → trả `Scrubbed`. Đổi `agent/llm_client.py` → nhận `Scrubbed`.

`auth/context.py` → `ShopContext(shop_id, account_id, trace_id)`, frozen dataclass.

Mypy strict đã bật sẵn trong CI ⇒ vi phạm là **build đỏ**, không cần thêm gì.

**Giới hạn phải bù bằng test:** `NewType` chặn *truyền sai*, không chặn *ép kiểu bừa* — `Scrubbed("raw")` gọi ở đâu cũng chạy. Thêm `tests/contract/test_construction_sites.py` bảo đảm `Scrubbed(` / `Wrapped(` chỉ xuất hiện trong `agent/pii.py`.

---

## §7 · Langfuse

```bash
docker compose up -d langfuse
open http://localhost:3000
```

Tạo account local → project `ohana-be` → Settings → API Keys → copy public + secret → `.env`.

Hook trace đặt **bên trong** `agent/llm_client.py` (I16). Vì hàm đó đã chỉ nhận `Scrubbed` sau A3, không có đường nào gửi payload thô sang Langfuse — đúng tự động, không cần ai nhớ.

Self-host là điều kiện để golden set C4 (≥200 tin PII **thật**) ở lại trong nước.

---

## §8 · Postgres MCP

Config Claude Code (stdio, không phải connector directory):

```json
{
  "mcpServers": {
    "ohana-pg": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-postgres",
               "postgres://mcp_readonly:PW@localhost:5432/ohana"]
    }
  }
}
```

**Phải là `mcp_readonly`.** Nối bằng `ohana` hoặc `svc_seller` thì agent có nhiều quyền hơn cả hai service — I2 vô hiệu ngay tại ghế agent.

Kiểm: hỏi *"liệt kê bảng trong public"* → thấy. Hỏi *"insert vào pending_reply"* → bị từ chối.

---

## §9 · Skill

Repo chưa có `.claude/skills/` (chỉ có `hooks/` và `tools/`). Thêm:

```
.claude/skills/ohana-be-coder/SKILL.md
```

File có sẵn trong gói này.

Kiểm skill — bảo Claude Code:

> *"Gộp §6.1 thành hai câu INSERT cho dễ đọc"*

Phải **từ chối** kèm lý do. Đồng ý làm ⇒ skill chưa load.

---

## §10 · Nhịp làm việc

```
1. Mở Linear OHB → chọn issue Todo
2. Copy gitBranchName → git checkout -b <tên đó>
3. Đọc §ref trong docs/ohana-be-design.md
4. Tier `stop`? → mô tả kế hoạch, chờ duyệt
5. Code
6. pre-commit: ruff → mypy → lint-imports → pytest
7. BẠN đọc diff, không đọc summary agent viết
8. Commit → PR → merge
```

Không mở issue thứ hai khi issue thứ nhất chưa đóng.

---

## §11 · Sự cố

**`alembic upgrade head` báo `permission denied for schema public`**
Đang chạy bằng `ohana_migrator` nhưng chưa `GRANT CREATE ON SCHEMA public`. Chạy lại §2.

**`test_i14` đỏ ở `svc_seller` không đọc được bảng mới**
Thiếu `ALTER DEFAULT PRIVILEGES FOR ROLE ohana_migrator`, **hoặc** alembic đã chạy bằng role khác. Kiểm: `\ddp` trong psql.

**`test_i14` đỏ ở `svc_ohana_ai` VẪN đọc được `public`**
Có `GRANT` rộng tay ở migration cũ. `\dp pending_reply` xem ai đang có quyền.

**`lint-imports` đỏ hàng loạt lần đầu**
Đúng như kỳ vọng — hai luồng đang dính nhau. Đó là danh sách việc của A4, không phải lỗi cấu hình.

**Kết nối fail dù mật khẩu đúng**
Mật khẩu chứa `/` `+` `=` ⇒ vỡ URL. Dùng `openssl rand -hex 24`.

**`embeddings` không tìm thấy sau A1**
Model chưa thêm `{"schema": "platform"}`. Alembic đã đổi DB nhưng SQLAlchemy vẫn trỏ `public`.

---

## §12 · Checklist đóng setup

- [ ] §0 đối chiếu HEAD xong, danh sách 6 lỗ hổng đã cập nhật
- [ ] `docker compose up -d` → postgres + langfuse `Up`
- [ ] `python scripts/bootstrap_roles.py` → 4 role
- [ ] `alembic upgrade head` **bằng `ohana_migrator`** → xanh
- [ ] `pytest tests/contract/test_i14_default_privileges.py` → **4 passed**
- [ ] `lint-imports` chạy được (đỏ cũng được — đã ghi lại danh sách vi phạm)
- [ ] `mypy` vẫn xanh sau khi thêm 3 type
- [ ] Langfuse có key trong `.env`
- [ ] MCP đọc được, **không** ghi được
- [ ] Skill từ chối yêu cầu tách `§6.1`
- [ ] `ci.yml` đã thêm bootstrap_roles + lint-imports
- [ ] `OHB-1` → **Done**

---

## §13 · Sau setup

| Việc | Chặn bởi |
|---|---|
| **A4** tách entrypoint A/B/worker | danh sách vi phạm `lint-imports` |
| **A5** `outbox` + `trace_id` + §6.1 CTE | `OHB-12` (retention NĐ13) |
| **A6** cost cap | — |
| **A7** debounce + reaper | — |
| **A8** `policy_gate` → `escalation_reasons` | — |

A4 rủi ro nhất — tách process trên code đang chạy. Làm sau khi `lint-imports` đã cho biết chính xác chỗ nào dính.
