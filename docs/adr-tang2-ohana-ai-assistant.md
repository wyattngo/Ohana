---
doc: adr-tang2-ohana-ai-assistant
status: accepted
date: 2026-07-31
decides: [D1, D2, D3, D4, D5, D6, D7]
open: []
ratified_by: Wyatt
contract: tang2-ohana-ai-assistant-system-design.md
track: pivot-buoc2-tang2.md
relates: ohana-be-design.md  # Tầng 3 — bất biến I1/I2/I14 áp cho Tầng 2
read_before:
  - any migration tạo schema `assistant`
  - any change under agent/, api/ luồng A cho Tầng 2
  - any change under auth/ liên quan role `user` / tier claim
---

# ADR — Tầng 2 · Ohana AI Assistant

## §0 · Bối cảnh

Super-app 3 tầng: Tầng 1 (mạng xã hội — identity/billing/feed/video, chưa có), Tầng 2 (trợ lý AI cho user: hỏi-đáp kiến thức chung + nhớ hội thoại + memory per-user + tính token + gate freemium — doc này), Tầng 3 (AI Seller copilot — §11, đã code-complete). Tầng 2 = luồng A (`svc_ohana_ai`), port từ DrNickv4 (bản dựng đang chạy của đúng mô hình này), reconcile vào ràng buộc I1/I2 của Ohana. ADR này chốt kiến trúc để Phase 2.1+ derive.

## §1 · Quyết định đã ký (D1–D6)

**D1 · Process — mở rộng `main_ohana_ai`.** Tầng 2 sống trong service luồng A sẵn có (`svc_ohana_ai`), thêm router chat/conversations/memory. Hệ quả: không đẻ process thứ tư; `main` phình — chấp nhận.

**D2 · Memory ghi ở luồng A — grant WRITE có phạm vi.** `svc_ohana_ai` được `USAGE` + `SELECT/INSERT/UPDATE/DELETE` trong schema `assistant` (và CHỈ đó). Dữ liệu shop (`public`/`platform`) role này vẫn DENIED; `svc_seller` DENIED trên `assistant`. Hệ quả: mở WRITE luồng A lần đầu — nhưng I2 giữ nguyên cho dữ liệu shop; isolation gate hai chiều (Phase 2.1) là bằng chứng bắt buộc trước khi port logic.

**D3 · Redis — thêm vào compose.** Cost counter + rate-limit per-user cần Redis KV. Lưu ý: §10 cấm Redis-làm-queue (outbox đã là queue), KHÔNG cấm Redis-làm-counter. Hệ quả: +1 infra dep; thay thế (counter trên Postgres) thua ở hot-path contention.

**D4 · Redis down → rate-limit fail-open (v1).** Redis chớp ⇒ không chặn user (mất gate tạm). Hệ quả: an toàn trải nghiệm > an toàn cost khi Redis lỗi; revisit khi có abuse đo được.

**D5 · Grounding chat — pure-LLM Q&A (v1), chưa RAG.** Trợ lý trả lời bằng kiến thức LLM (như Claude/Grok), `grounded=false`. Hệ quả: khớp yêu cầu; knowbase RAG (reuse `retrieval`) để phase sau nếu cần grounding. (DrNick có RAG nhưng đó là vertical tài chính.)

**D6 · Billing — ủy thác nền tảng, Tầng 2 giữ GATE.** Tầng 2 sở hữu tier-gate (free/pro → rate + feature), không sở hữu payment. Nâng gói gọi hook `upgrade(user_id)` về nền tảng (mẫu DrNick→ONFA). Hệ quả: không tự-freemium-billed tới khi có billing provider; nhưng GATE chạy ngay bằng `tier` claim.

## §2 · Quyết định PHÁT SINH — D7 (ratified 2026-08-01)

**D7 · Identity issuer — Ohana tự phát (a).** Ohana mint JWT cho user Tầng 2 bằng cách mở rộng auth sẵn có (`auth/identity.py` HS256). Ohana = identity provider cho cả Tầng 3 (seller/admin) và Tầng 2 (user).

Ratified (Wyatt 2026-08-01) — hai lý do:
- Đơn giản: không couple ONFA identity, không chờ ONFA JWT verifier có sẵn.
- Ohana đã có infra JWT chạy production Tầng 3 (S1). Thêm role `user` + claim `tier` là increment, không phải greenfield.

**Hệ quả cho Phase 2.4:**

- **Token shape mới cho user Tầng 2**: `{sub: user_id, role: "user", tier: "free"|"pro"}` — **KHÔNG** có `shop_id` (Tầng 2 per-user, không có shop concept). Tách hoàn toàn khỏi seller token (`{sub, shop_id, role: seller|admin}`).
- **`UserIdentity(user_id: str, tier: str)`** dataclass tách khỏi `Identity` (Tầng 3): hai concept khác nhau — user (person) vs seller (shop role). Trộn = drift.
- **`verify_user_token`** riêng, KHÔNG chia SQL WHERE với `verify_token`. Same secret + same allowed algo, khác payload validator (`role == "user"`, `tier ∈ {free, pro}`).
- **`user_identity_from_cookie`** dependency mới; cùng cookie `ohana_session` để browser SPA share một transport, nhưng route Tầng 2 chỉ chấp nhận token role `user`.
- **Tier gate** đọc `tier` từ `UserIdentity`, tra bảng limit static (`free`/`pro` → qpm + daily token cap), gọi `assistant_rate_limit.try_acquire` + kiểm `assistant_cost.get_daily_tokens` trước LLM call.
- **Billing delegated (D6)** giữ nguyên: nâng `tier` = hook `upgrade(user_id)` về nền tảng (P2.5 hoặc sau); Phase 2.4 chỉ đọc `tier` claim, không cấp phát.

**Không chọn (b) — ONFA JWT:** ONFA hiện chưa có JWT surface user-facing; nếu chọn (b), Tầng 2 phải chờ Tầng 1 hoặc ONFA identity refactor. D7 không phải quyết định-không-quay-lại: nếu về sau ONFA cần SSO chung, port sang RS256 + JWKS + mint delegated là 1-2 phase riêng, không rewrite luồng A.

## §3 · Hệ quả kiến trúc (tổng)

- Schema `assistant` mới (tách `public` seller + `platform` corpus): `conversations`/`messages`/`user_memory`, key `user_id`.
- Memory = per-user semantic (namespace `mem:user:{id}`, append-only, vector recall), embedding e5 1024d dùng chung pgvector (`EMBED_DIM=1024`, xác nhận từ design doc).
- Cost per-user (khác Tầng 3 per-shop B6) + tier gate free/pro, qua Redis.
- Q&A pure-LLM v1; billing delegated; domain tổng quát (bỏ vertical tài chính của DrNick = GĐ4).

## §4 · Cưỡng chế / gate

- I1/I2 giữ: importlinter (luồng A không import luồng B) + isolation gate Phase 2.1 (test kiểu `test_i14`, hai chiều) chứng minh D2 không phá I2.
- I14: bảng mới trong schema mới ⇒ `ALTER DEFAULT PRIVILEGES` + grant tường minh.
- Contract thực thi = `tang2-...system-design.md`; migration = `ohana_migrator` (stop-tier).

## §5 · Phase (JIT — tạo issue khi tới, không BDUF)

2.0 ADR + track (doc này) · 2.1 schema + isolation gate — **DONE PR #6** · 2.2 cost/rate-limit/Redis — **DONE PR #9** · 2.3 memory (save + recall) — **DONE PR #10** · 2.4 chat/routers + tier gate (D7 ratified) · 2.5 generalize domain.
