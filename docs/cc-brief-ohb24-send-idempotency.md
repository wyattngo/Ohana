# CC Brief — OHB-24 · Send idempotency (chống double-send trước ZaloSender live)

> **Skill:** `ohana-be-coder`.
> **Contract:** `docs/ohana-be-design.md` (Tầng 3 — bất biến I2/I13/I14 áp).
> **Trạng thái:** Linear [OHB-24](https://linear.app/drnick/issue/OHB-24) Backlog, project **Ohana BE — Phase 1**, related [OHB-23](https://linear.app/drnick/issue/OHB-23).
> **Mục tiêu:** đóng gap exactly-once của send-worker (Bước 1 gap "Chưa verify" #1) — điều kiện CỨNG trước khi bật `ZaloSender` thật (PRE-004).

---

## §0 · Bối cảnh (verify on-disk main `8aba78b`)

`app/worker_seller.py::send_one` gọi `sender.send()` (network) → `mark_sent`. Nếu crash giữa hai bước: R5 (`_REAP_R5_STUCK_SEND_CLAIM`) NULL `sent_claimed_at`, worker khác re-claim → **gửi lại**. `SKIP LOCKED` chỉ chặn concurrent claim, không chặn crash-reap-resend.

Comment tự-thú đã có ở [app/worker_seller.py:399-407](app/worker_seller.py:399): _"có thể double-send khi lượt sau claim lại"_. Vô hại GĐ0 (MockZaloSender ghi vào list), nhưng khi `ZaloSender` HTTP thật live thì gửi 2 tin tới khách.

## §0.5 · Pre-flight

```bash
git log --oneline main -3                                                   # HEAD = 8aba78b
DATABASE_URL="postgresql+psycopg://ohana_migrator:$MIGRATOR_PW@localhost:5433/ohana" alembic current
                                                                            # = a12_assistant_schema (head)
DATABASE_URL=…ohana_test… pytest -q                                         # 375 passed baseline
```

- [ ] File chạm: `db/migrations/versions/a13_send_dedup_log.py` (NEW, **stop**), `db/models.py` (ask), `db/repos.py` (ask), `app/worker_seller.py` (ask), `tests/test_ohb24_send_idempotency.py` (NEW, ask), `tests/contract/conftest.py` (ask — thêm `sent_log` vào `wipe_tenant`).
- [ ] Đọc §ref: [OHB-23 send-worker impl](https://linear.app/drnick/issue/OHB-23) + `a2_flow_grants` (mẫu grant tường minh 1 bảng) + `webhook_event_log` (mẫu ON CONFLICT DO NOTHING RETURNING dedup — I7).
- [ ] Bất biến chạm: **I2, I13, I14** (§3 dưới).

---

## §1 · Quyết định (từ chọn của user)

- **Option A (sent_log table)** — structural dedup, không phụ thuộc Zalo API contract. Verify PRE-004 sau (nếu Zalo hỗ trợ idempotency-key → thêm defense-in-depth ở phase riêng).
- **Trade-off ký:** crash giữa reserve-and-send ⇒ khách không nhận (im lặng, at-most-once). Thà im lặng còn hơn double.
- **Reserve TRƯỚC send** — thứ tự bắt buộc. Reserve sau send là race window mất mát.

---

## §2 · Tasks — MATCH / OUT

### MATCH

| # | Việc | File | Tier | Gate |
|---|---|---|---|---|
| 1 | Migration `a13_send_dedup_log` | `db/migrations/versions/a13_send_dedup_log.py` (NEW) | **stop** (`ohana_migrator`) | `alembic current` = `a13_send_dedup_log` · svc_seller có DELETE trên `sent_log` |
| 2 | Model `SentLog` | `db/models.py` (append) | ask | mypy xanh |
| 3 | Repo `SentLogRepo` | `db/repos.py` (append) | ask | mypy xanh |
| 4 | Update `send_one` flow | `app/worker_seller.py` | ask | Comment double-send warning gỡ, docstring cập nhật |
| 5 | Test RED→GREEN double-send scenario | `tests/test_ohb24_send_idempotency.py` (NEW) | ask | send() gọi đúng 1 lần dù re-claim sau crash |
| 6 | Extend `conftest.wipe_tenant` cho `sent_log` | `tests/contract/conftest.py` | ask | Existing contract tests xanh |

### Chi tiết Task 1 — migration `a13_send_dedup_log.py`

**Revision:** `a13_send_dedup_log` · **down_revision:** `a12_assistant_schema` · chạy `ohana_migrator`.

```sql
-- Bảng dedup: PK = reply_id (unique cho mỗi draft đã sent thành công)
CREATE TABLE sent_log (
  reply_id     text        PRIMARY KEY,
  shop_id      text        NOT NULL,
  customer_id  text        NOT NULL,
  sent_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_sent_log_shop_sent ON sent_log (shop_id, sent_at DESC);

-- svc_seller a1 có SELECT/INSERT/UPDATE (mặc định public). CẦN thêm DELETE cho rollback
-- khi send() lỗi (repo `rollback` xoá reservation cho lượt kế retry). Grant tường minh
-- CHỈ bảng này, KHÔNG ALTER DEFAULT PRIVILEGES thêm DELETE cho public — mở default DELETE
-- là quyết định wider, không thuộc scope OHB-24.
GRANT DELETE ON TABLE sent_log TO svc_seller;
```

**KHÔNG FK về `pending_reply(reply_id)`:** retention khác (pending_reply có thể xoá cho training label, sent_log giữ audit lâu). FK gây CASCADE mất audit.

**KHÔNG grant `svc_ohana_ai`:** Tầng 3 (luồng B) scope, không phải Tầng 2.

**downgrade:** `DROP TABLE sent_log` (grant cascade dọn theo).

### Chi tiết Task 2 — model

```python
class SentLog(Base):
    """Dedup log cho send-worker (OHB-24 · I13-adjacent). Chống double-send khi crash-
    reap-resend: reserve_send TRƯỚC gọi sender ⇒ INSERT ON CONFLICT DO NOTHING RETURNING
    là atomic barrier — nếu row đã có (từ lần send thành công trước), sender KHÔNG được
    gọi lần hai. Rollback (DELETE) khi send() raise để lượt kế retry được.

    Trade-off: crash giữa reserve và send ⇒ im lặng (khách không nhận). Thà im lặng còn
    hơn double — reservation không rollback nghĩa là draft ĐÃ đăng ký gửi (bên ngoài
    không biết thành công hay chưa), retry không an toàn.
    """
    __tablename__ = "sent_log"
    __table_args__ = (Index("idx_sent_log_shop_sent", "shop_id", "sent_at"),)

    reply_id: Mapped[str] = mapped_column(Text, primary_key=True)
    shop_id: Mapped[str] = mapped_column(Text, nullable=False)
    customer_id: Mapped[str] = mapped_column(Text, nullable=False)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
```

### Chi tiết Task 3 — repo

```python
class SentLogRepo:
    """Send dedup log — unscoped (cùng biên nền-tảng với SendQueueRepo, worker chạy khắp
    mọi shop). Isolation ở caller: send_one truyền shop_id + customer_id gắn cứng từ
    SendJob (không từ input ngoài)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reserve_send(
        self, *, reply_id: str, shop_id: str, customer_id: str
    ) -> bool:
        """INSERT ON CONFLICT DO NOTHING RETURNING — atomic dedup barrier.

        Trả `True` = reservation mới (được phép gọi sender), `False` = đã dedup trước
        (skip send, mark_sent để clear claim). Cùng khuôn `webhook_event_log` dedup
        (I7). Race: N worker cùng bắn câu này ⇒ Postgres serialize INSERT, đúng 1 bên
        thắng, N-1 bên nhận rowcount=0."""
        stmt = text("""
            INSERT INTO sent_log (reply_id, shop_id, customer_id)
            VALUES (:reply_id, :shop_id, :customer_id)
            ON CONFLICT (reply_id) DO NOTHING
            RETURNING reply_id
        """)
        result = await self._session.execute(stmt, {
            "reply_id": reply_id, "shop_id": shop_id, "customer_id": customer_id,
        })
        row = result.first()
        await self._session.commit()
        return row is not None

    async def rollback(self, reply_id: str) -> None:
        """Xoá reservation khi send() raise — lượt kế retry được (reservation không
        rollback là im lặng vĩnh viễn cho draft đó). CHỈ gọi khi CHẮC CHẮN send() không
        thành công (exception path); mọi đường khác giữ reservation."""
        await self._session.execute(
            text("DELETE FROM sent_log WHERE reply_id = :reply_id"),
            {"reply_id": reply_id},
        )
        await self._session.commit()
```

### Chi tiết Task 4 — `send_one` flow mới

```python
async def send_one(job: SendJob, deps: WorkerDeps) -> None:
    """OHB-24 · dedup barrier trước send(). Flow ba pha:

    (1) reserve_send (INSERT ON CONFLICT DO NOTHING RETURNING) — atomic dedup.
        `False` ⇒ đã sent trước đó (crash-reap-resend), mark_sent clear claim và về,
        KHÔNG gọi sender lần hai.
    (2) sender.send() — network call. Exception ⇒ rollback dedup + re-raise cho caller.
    (3) mark_sent (echo :claimed_at). rowcount=0 ⇒ R5 gỡ claim trước mark; sender ĐÃ
        gửi (dedup log lock reply_id ⇒ lượt sau không double-send).
    """
    # (1) Dedup barrier
    async with deps.session_factory() as session:
        reserved = await SentLogRepo(session).reserve_send(
            reply_id=job.reply_id, shop_id=job.shop_id, customer_id=job.customer_id,
        )
    if not reserved:
        # Đã sent trước đó — clear claim, KHÔNG gọi sender (chống double-send).
        logger.warning(
            "send dedup hit: reply %s đã sent trước (crash-reap-resend?) — skip sender",
            job.reply_id,
        )
        async with deps.session_factory() as session:
            await PendingReplyRepo(session, shop_scope=job.shop_id).mark_sent(
                job.reply_id, claimed_at=job.claimed_at
            )
        return

    # (2) Network — exception ⇒ rollback dedup để retry được
    try:
        await deps.sender.send(
            shop_id=job.shop_id, customer_id=job.customer_id, text=job.draft_text
        )
    except Exception:
        try:
            async with deps.session_factory() as session:
                await SentLogRepo(session).rollback(job.reply_id)
        except Exception:
            # Rollback fail = reservation kẹt = draft im lặng vĩnh viễn. Log to,
            # vận hành xử tay (DELETE sent_log WHERE reply_id = ...).
            logger.exception(
                "sent_log rollback lỗi reply=%s — reservation kẹt, gỡ tay!", job.reply_id
            )
        raise

    # (3) Mark sent (echo claimed_at)
    async with deps.session_factory() as session:
        n = await PendingReplyRepo(session, shop_scope=job.shop_id).mark_sent(
            job.reply_id, claimed_at=job.claimed_at
        )
    if n == 0:
        # R5 gỡ claim trước mark_sent — sender ĐÃ gửi, dedup log lock reply_id ⇒
        # lượt sau `reserve_send` fail (row đã có) ⇒ skip sender ⇒ KHÔNG double-send.
        # Đây khác pre-OHB-24 (cảnh báo có thể double-send) — giờ log info-level.
        logger.info(
            "mark_sent no-op: reply %s bị R5/worker khác soán quyền — dedup log chống double-send",
            job.reply_id,
        )
```

### Chi tiết Task 5 — test RED

**File NEW `tests/test_ohb24_send_idempotency.py`:**

```python
"""OHB-24 · Send idempotency — chống double-send khi crash-reap-resend.

Scenario: (1) send() thành công (2) mark_sent CRASH (mock throw) → lượt kế claim lại
→ `reserve_send` returns False (dedup log lock) → skip sender → mark_sent OK. Sender
được gọi ĐÚNG 1 lần.

Gate exactly-once (well, at-most-once + dedup) của send-worker Tầng 3."""

@pytest.mark.asyncio
async def test_double_send_prevented_by_dedup_log(fresh_db):
    _, session_factory = await fresh_db()
    await _seed(session_factory)
    reply_id = await _park_approved(session_factory)

    counting_sender = _CountingSender()

    # Lần 1: send OK, mark_sent throw giả lập crash
    class _CrashMarkPendingReplyRepo:
        # Monkey-patch mark_sent để raise sau khi sender.send() xong
        ...

    deps = WorkerDeps(session_factory=..., drafter=_NoDraft(), sender=counting_sender)
    ... # run_send_loop lần 1 → sender.send gọi 1 lần, mark_sent crash

    # R5 reap giả lập (backdate sent_claimed_at)
    ...

    # Lần 2: sender lẽ ra gọi lần hai nếu không có dedup, nhưng reserve_send fail
    await run_send_loop(deps, run_once=True)

    assert counting_sender.call_count == 1, "dedup log phải chặn send lần thứ hai"
    # Status = 'sent' vì mark_sent thứ hai (đường dedup-hit) thành công
    async with session_factory() as s:
        row = await s.get(PendingReply, reply_id)
    assert row.status == "sent"
```

### Chi tiết Task 6 — conftest wipe

Thêm `DELETE FROM sent_log WHERE shop_id = %s` vào `wipe_tenant` (tests/contract/conftest.py) — cho contract tests dùng sạch giữa run.

### OUT

- Zalo idempotency-key (defense-in-depth) — chờ PRE-004 verify Zalo API contract.
- Sent_log retention/archive policy — sau khi có traffic đo được.
- Metric/alert cho "reservation kẹt" (rollback fail) — Phase Ops sau.

---

## §3 · Bất biến chạm

| I | Task | Vẫn đúng vì |
|---|---|---|
| **I2** | 1 | Bảng `sent_log` ở `public`, `svc_ohana_ai` KHÔNG đụng (grant chỉ svc_seller — a1 default + DELETE tường minh). Test i14 hiện có vẫn xanh (probe table mới trong public vẫn theo default privileges). |
| **I13** | 4 | Dedup log CỘNG với claim/R5 hiện có, không thay thế. R5 vẫn gỡ claim treo — chỉ khác là lượt claim kế sau R5 gỡ sẽ hit dedup và skip sender thay vì gửi lại. Claim mesh + reap giữ nguyên. |
| **I14** | 1 | Bảng mới trong `public` thừa kế default privileges của a1 (SELECT/INSERT/UPDATE cho svc_seller). DELETE mở tường minh CHỈ bảng này — không đè ALTER DEFAULT PRIVILEGES của a1. |

## §3b · Cấm

Chạy Alembic bằng role ≠ `ohana_migrator` · `ALTER DEFAULT PRIVILEGES ... GRANT DELETE ... TO svc_seller` cho toàn public (over-broad, chỉ scope bảng này) · Bỏ RETURNING clause của INSERT (không phân biệt được duplicate) · Reserve SAU send (race window mất mát) · FK `sent_log.reply_id → pending_reply.reply_id` (CASCADE mất audit khi training pipeline xoá pending_reply cũ).

---

## §4 · Verify / DoD

**Verify tối thiểu:**

```bash
# 1. Migration lên DB dev
DATABASE_URL="postgresql+psycopg://ohana_migrator:$MIGRATOR_PW@localhost:5433/ohana" alembic upgrade head
# → head = a13_send_dedup_log

# 2. Verify grants + column
python3.11 -c "
import psycopg, os
with psycopg.connect(...) as c:
    print('grants:', c.execute(\"SELECT grantee, privilege_type FROM information_schema.role_table_grants WHERE table_name='sent_log' ORDER BY grantee, privilege_type\").fetchall())
    # svc_seller: SELECT/INSERT/UPDATE/DELETE (a1 default + a13 tường minh)
    # svc_ohana_ai: KHÔNG có (I2 giữ)
"

# 3. Static
ruff check . --no-cache && ruff format --check . --no-cache
mypy app agent retrieval parsing db bridge tools api auth
lint-imports          # 4 contracts kept

# 4. Suite Python
DATABASE_URL=…ohana_test… pytest -q    # baseline 375 + N test OHB-24

# 5. Test gate riêng OHB-24
pytest tests/test_ohb24_send_idempotency.py -v    # scenario double-send prevented
```

**Gate của bước (bắt buộc xanh):**

- Migration a13 land, alembic current = `a13_send_dedup_log`.
- Grants: `svc_seller` có DELETE trên `sent_log`; `svc_ohana_ai` KHÔNG có quyền nào.
- Test OHB-24: `sender.send()` gọi ĐÚNG 1 lần dù re-claim sau crash mark_sent.
- Full suite Python + ruff/mypy/lint-imports xanh.
- Send_one comment double-send warning gỡ; docstring cập nhật.

**Báo cáo theo khuôn ohana-be-coder.**

**Checkpoint đạt khi:** OHB-24 → Done · double-send scenario được dedup log chặn thật · pending-external PRE-004 (Zalo creds) không blocker nữa cho phần "gửi duplicate".

---

## §5 · Chưa quyết (defer)

- **Sent_log retention** — append vô hạn per-shop. Retention/archive khi có traffic đo được.
- **Reservation kẹt (rollback fail)** — hiện log-only. Alert + auto-repair sau khi có Prometheus.
- **Zalo idempotency-key** — defense-in-depth phase riêng, verify Zalo API v3.0 hỗ trợ hay không.
