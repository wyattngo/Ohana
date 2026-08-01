---
doc: adr-round2-api-streaming-bulk-search-feedback
status: proposed
date: 2026-08-01
decides: [R1, R2, R3, R4, R5]
open: [Q1, Q2, Q3]
ratified_by: (pending)
relates: adr-tang2-ohana-ai-assistant.md, ohana-be-design.md
scope: Tầng 2 (R1–R4) + Tầng 3 (R5)
---

# ADR — Round 2 API: streaming, bulk delete, search, feedback, send-worker

## §0 · Bối cảnh

Round 1 (Phase 2.1–2.4) đã ship: assistant chat (đơn phát), CRUD conversations + memory, freemium gate, F1 sidebar. Round 2 gom 5 API còn thiếu để trợ lý đạt "UX parity với Claude/ChatGPT web":

| # | Feature | Tầng | Lý do |
|---|---|---|---|
| R1 | Streaming chat | 2 | UX — 3s wait cho 500-token reply là chấp nhận, 10s wait cho 3k-token reply là bỏ dở |
| R2 | Bulk delete conversations | 2 | Cleanup — user có 100 hội thoại, xoá từng cái là friction |
| R3 | Search conversations + messages | 2 | Recall — không có ai nhớ "hội thoại 3 tuần trước bàn X" |
| R4 | Feedback endpoint | 2 | Signal — thumbs up/down để đo prompt quality trước khi RAG |
| R5 | Send-on-approve worker | 3 | **Đã tồn tại — §1.5 làm rõ delta** |

## §1 · Trạng thái hiện tại (grounded)

### §1.1 · Assistant chat (R1 source)
- `POST /assistant/chat` ở [api/assistant_chat.py:127](api/assistant_chat.py) — sync JSON, `AssistantChatResponse` body.
- Depend chain: `verify_user_token` → tier gate → `assistant_rate_limit.try_acquire` → `assistant_cost.reserve` → `llm_client.stream_wrapped` (đã có sẵn stream primitive) → `assistant_cost.record_actual` → `assistant_conversations.append_turn`.
- Đã tồn tại: LLM client streaming (Tầng 3 tái dùng). Chưa expose ra client.

### §1.2 · CRUD conversations (R2 source)
- `POST/GET/DELETE /assistant/conversations` ở [api/assistant_crud.py:131](api/assistant_crud.py) — single-object CRUD.
- Table `assistant.conversations` — cursor pagination bằng `(updated_at, id)` (P2.4c).
- Delete = hard delete (spec chốt: user data, không audit trail cần giữ).

### §1.3 · Search (R3 source)
- **Chưa có index full-text**. `assistant.messages` chỉ có PK + `(conversation_id, created_at, message_id)` cover pagination.
- `assistant.conversations.title` chưa index.
- pgvector 1024d đã có cho memory, chưa dùng cho message search.

### §1.4 · Feedback (R4 source)
- **Chưa có table nào cho feedback**. `assistant.messages` chưa có cột rating.
- Tầng 3 (`pending_reply`) có `decided_by` + status transition — pattern tham chiếu.

### §1.5 · Send-on-approve — **ĐÃ TỒN TẠI**
- [app/worker_seller.py:455](app/worker_seller.py) `run_send_loop` — OHB-23, loop 3 của 4 loop. Claim `pending_reply.status='approved'` → dedup `sent_log` (OHB-24) → `bridge.zalo_sender.send()` → mark 'sent'. Reaper R5 gỡ claim treo.
- [api/inbox.py:79](api/inbox.py) `POST /inbox/{reply_id}/approve` — flip status → 'approved'. Không enqueue tường minh, worker poll 500ms.
- **Delta thực sự cần build?** Ba khả năng — Wyatt chọn 1:
  - **(a) Nothing** — worker + approve đủ (khuyến nghị nếu không có triệu chứng cụ thể).
  - **(b) Push-notify khi send xong** — seller thấy "đã gửi" real-time (WS hoặc SSE trên `/inbox/events`).
  - **(c) Manual "resend"** — 1 lượt gửi lỗi retryable → cho seller nút bấm gửi lại (không đợi reaper).

→ [Q1] Wyatt clarify — hiện tôi giả định (a) và loại R5 khỏi scope Round 2.

## §2 · Quyết định — R1 … R4 (R5 = no-op nếu Q1=a)

### R1 · Streaming chat

**Chọn: SSE (Server-Sent Events)**, một endpoint mới `POST /assistant/chat/stream`, không sửa endpoint cũ.

| Option | Chọn | Lý do |
|---|---|---|
| A: SSE (`text/event-stream`) | ✅ | Một chiều server→client, HTTP/1.1, nginx pass-through OK, browser `EventSource` API sẵn. Reconnect có `Last-Event-ID` chuẩn. |
| B: WebSocket | ❌ | Hai chiều không cần cho chat (input là 1 request). Nginx cần config upgrade — +infra complexity. |
| C: Chunked HTTP + fetch reader | ⚠️ | Làm được, nhưng không có event framing chuẩn (client tự parse). SSE = chunked + framing chuẩn. |

**Wire format** (1 event/token hoặc gộp mỗi 32 token — LLM client control):
```
event: token
data: {"text":"Xin"}

event: token
data: {"text":" chào"}

event: done
data: {"conversation_id":"...","message_id":"...","tokens_in":45,"tokens_out":128}

event: error
data: {"code":"RATE_LIMITED","message":"..."}
```

**Cost gate tại RESERVE, không tại DONE.** `reserve` trước khi mở stream (theo max_output_tokens), `record_actual` khi close stream (compensate). Client disconnect giữa chừng ⇒ finally block ghi actual với tokens đã gửi. Không reserve = user free spam 100k tokens/turn trước khi gate kịp thấy.

**Rate limit token 1 lần trước khi mở stream.** Không token-bucket theo chunk (rate limit là per-request, không per-token).

**HTTPException KHÔNG thoát được sau khi headers gửi.** Rate limit / tier gate check TRƯỚC khi trả `StreamingResponse`. Lỗi giữa stream ⇒ `event: error` rồi `event: done` + close, không raise 5xx (client đã nhận 200).

**Persistence: append_turn CHỈ khi done event ghi.** Client disconnect + generation dở ⇒ vẫn lưu partial reply (đã tốn token thật). Không lưu = ma dữ liệu cost.

### R2 · Bulk delete conversations

**Chọn: `POST /assistant/conversations/bulk-delete` với body `{ids: [...]}`.** Không dùng `DELETE` với body — spec ambiguous, một số proxy strip.

| Option | Chọn | Lý do |
|---|---|---|
| A: `POST bulk-delete` với JSON body | ✅ | Ambiguous-free, dễ audit. |
| B: `DELETE /assistant/conversations?ids=a,b,c` | ❌ | URL length cap (~2k), IDs > 40 items vỡ. |
| C: `DELETE` với body | ❌ | RFC không cấm nhưng nginx / CDN đôi chỗ strip. |

**Cap N=100/request.** Vượt → 400. Rationale: DELETE trong 1 tx, > 100 UUID tăng lock window. Client cần xoá nhiều hơn → gọi lại (idempotent).

**Chỉ xoá conversation OWNED bởi user_id trong token — cross-user 404.** `DELETE FROM assistant.conversations WHERE id = ANY($1) AND user_id = $2 RETURNING id`. Return `{deleted: [...ids], skipped: [...ids-không-return]}`. Không phân biệt "không tồn tại" vs "của user khác" — I2 policy 404-not-403.

**CASCADE xử lý messages tự động** (spec P2.4c: `messages.conversation_id ON DELETE CASCADE`).

### R3 · Search

**Chọn: PostgreSQL FTS (`tsvector`) trên title + message body.** KHÔNG dùng vector cho v1.

| Option | Chọn | Lý do |
|---|---|---|
| A: FTS `tsvector` + `simple` config | ✅ | Việt hóa được sau (thay `simple` bằng `pg_search`). Zero-infra. |
| B: pgvector semantic search (reuse memory embed) | ❌ (v1) | Query embed cost — free user 100 search/day = 100 embed call, tăng cost 5-10× search. Sau khi có RAG (D5 revisit) mới hợp lý. |
| C: Trigram (`pg_trgm`) | ❌ | Prefix/typo tốt nhưng thua FTS cho query 3-4 từ. |
| D: External (Elastic/Meilisearch) | ❌ | Vi phạm §10 "tránh infra mới" trừ khi có nhu cầu đo được. |

**Endpoint: `GET /assistant/search?q=...&cursor=...&limit=20`.** 2 kết quả kind trong 1 response:
```json
{
  "conversations": [{"id":"...", "title":"...", "matched":"title", "updated_at":"..."}],
  "messages": [{"id":"...", "conversation_id":"...", "snippet":"...tin nhắn <em>match</em>...", "created_at":"..."}],
  "next_cursor": "..."
}
```
- `ts_headline` cho snippet (140 char, `<em>` highlight).
- **1 index nhất `assistant.messages`** GIN(`to_tsvector('simple', body)`) — cost migrate ~ N msg. Wyatt hiện có < 1M messages → migrate < 5s.
- **Rate limit riêng, gay hơn chat**: `search:{user_id}` → 30/min free, 120/min pro. Search cheap nhưng dễ spam bot scraping.
- **Query cost cap: 100ms.** `statement_timeout` local cho query search — vượt → 408 chứ không hold connection.

### R4 · Feedback endpoint

**Chọn: `POST /assistant/messages/{id}/feedback` với `{rating: -1|1, note?: string}`.** Idempotent (upsert per user+message).

| Option | Chọn | Lý do |
|---|---|---|
| A: Cột `rating` trên `assistant.messages` | ❌ | Feedback thay đổi độc lập với message. Race giữa append + rate. |
| B: Table riêng `assistant.message_feedback (message_id, user_id, rating, note, updated_at)` | ✅ | Append-only + upsert, đo được, mở rộng được (nhiều rating type sau). |
| C: Chỉ ghi vào Langfuse (không DB) | ❌ | Langfuse có Scrubbed-only (I16) — note chứa PII sẽ bị scrub trước khi ghi. Mất tín hiệu. |

**Chỉ user_id sở hữu conversation của message được rate.** Sub-query `messages ← conversations WHERE user_id = $token_user`. Cross-user 404.

**Rating chỉ áp cho `msg_role = 'assistant'`** — user không rate tin nhắn của chính họ (nonsense).

**Cast lên Langfuse (Scrubbed): user_id + rating + trace_id, KHÔNG kèm `note`.** Note chứa free text từ user → PII risk. Note đọc từ DB khi cần debug thủ công, không auto lên observability.

### R5 · Send-on-approve worker

**Chọn: no-op cho Round 2 nếu Q1=a.** Nếu Wyatt chọn (b) hoặc (c), open ADR riêng — pattern SSE (b) hoặc endpoint idempotent-resend (c) khác nhau đủ để không gộp.

## §3 · Trade-off tổng

**Tăng thêm:**
- 1 endpoint SSE, 1 endpoint bulk-delete, 1 endpoint search, 1 endpoint feedback → +4 API contract points.
- 2 index (FTS trên messages + title conversations) → +~50MB disk cho 1M rows.
- 1 bảng mới `assistant.message_feedback` (I2 phủ tự động qua ALTER DEFAULT PRIVILEGES).

**Giảm friction UX:**
- Chat < 3s TTFB (streaming) thay 8-12s wall (sync).
- Cleanup 50 hội thoại: 1 request thay 50.
- Recall hội thoại cũ: query thay scroll.

**Chưa xử lý (nên biết trước):**
- Search vietnamese analyzer — `simple` config không stem tiếng Việt ("đang chạy" vs "chạy" không match). v2 dùng `pg_search` hoặc `pgroonga` hoặc dictionary Vietnamese. Chấp nhận cho v1 vì hầu hết search là substring exact.
- SSE + nginx: cần `proxy_buffering off` cho location `/assistant/chat/stream`. Đã note trong deploy/nginx.conf mới.

## §4 · Câu hỏi mở

- **Q1** — R5 delta: (a) nothing / (b) push-notify send-done / (c) manual resend? → Wyatt chọn trước khi ratify.
- **Q2** — R3 search scope: hiện thiết kế bao gồm cả `user_memory`? Memory là recall-vector, không phải browse; đề xuất **loại** khỏi search endpoint (search chỉ conversations + messages, memory dùng `POST /assistant/memory/recall` sẵn có nếu cần).
- **Q3** — R4 feedback aggregate cho analytics: cần endpoint `GET /admin/feedback/summary` cho Wyatt xem daily-rate ratio? Hay đọc thẳng DB đủ ở scale hiện tại?

## §5 · Thứ tự implement (khi ratified)

1. **R2 (bulk-delete)** — 1 endpoint + 1 test. ~30 phút, không rủi ro.
2. **R4 (feedback)** — 1 migration + 1 endpoint + 3 test. ~1h, ít contract mới.
3. **R3 (search)** — 1 migration (FTS index) + 1 endpoint + rate limit key + 4 test. ~2h, cần verify search perf trên seed data.
4. **R1 (streaming)** — 1 endpoint SSE + nginx config + client contract + 5 test (cost gate + disconnect + partial persist). ~3-4h, rủi ro cao nhất (cost accounting đường disconnect).
5. **R5** — chỉ nếu Q1 ≠ (a).

Verify sau mỗi item: `pytest tests/assistant/ -q` + smoke qua Assistant sidebar (F1) đã ship.
