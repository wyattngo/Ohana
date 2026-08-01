# CC Brief · Bước 2 · Phase 2.4c — Conversations & Memories CRUD

**Frozen** · form `ohana-be-coder` · scope decision: 1 PR (2 repo + 1 router file mount 2 group endpoint).

## Bối cảnh & phạm vi

P2.4a (`8a52837`) shipped `UserIdentity` + tier gate. P2.4b (`04cdd6d`) shipped
`/api/assistant/chat` (stateless, consume 4 primitive). Bảng `assistant.conversations`
+ `assistant.messages` (P2.1) hiện đang trống — chat KHÔNG persist. Bảng
`assistant.user_memory` có save/recall primitive nhưng chưa có endpoint list/delete.

P2.4c ship **CRUD-only** endpoints để UI có thể:
- Người dùng liệt / tạo / đổi tên / xoá hội thoại (soft-delete).
- Người dùng list các memory đã save + forget (hard DELETE) memory nào không muốn giữ.

**Trong scope:**
- `agent/assistant_conversations.py` — repo class `AssistantConversations` (CRUD hội thoại,
  user_scope hard filter y hệt `AssistantMemory`).
- Mở rộng `agent/assistant_memory.py` — `list_memories(limit, cursor)` + `delete_memory(memory_id)`.
- `api/assistant_crud.py` — router mount 2 nhóm endpoint:
  - Conversations: POST, GET (list), GET/{id}, PATCH/{id} (title), DELETE/{id} (soft).
  - Memories: GET (list), DELETE/{id} (hard delete — ADR §3 quyết định P2.4c).
- Mount router trong `app/main_ohana_ai.py` (cùng session_factory, cùng tier gate).
- Tests: repo layer (Postgres) + endpoint (401 khi missing cookie, 404 khi cross-user).

**KHÔNG scope (dời P2.4d hoặc backlog):**
- Chat endpoint persist messages vào `assistant.messages` — vẫn stateless. Bảng messages
  trống là chủ đích tại P2.4c: message history là feature riêng, cần thay đổi contract
  `POST /api/assistant/chat` (in/out shape) và migration behavior. Tách để review được.
- Refine auto-save memory heuristic (P2.4b vẫn save-every-turn).
- Bulk delete memories / export.
- Restore soft-deleted conversation (out-of-scope; DB row còn, để P2.5).
- Rate-limit riêng cho CRUD endpoint (tier gate P2.4a chỉ áp `/chat`; CRUD không đốt LLM
  nên chưa cần cap — nếu abuse thấy sẽ thêm riêng ở P2.5).

## Hồ sơ ADR (đọc trước khi code)

`docs/adr-tang2-ohana-ai-assistant.md` §3 **Hệ quả kiến trúc**:
> Memory = per-user semantic (namespace `mem:user:{id}`, append-only, vector recall).

Comment trong `db/models.py::AssistantUserMemory`:
> Append-only tại Phase 2.1 — `forget` endpoint (Phase 2.4) quyết DELETE thật hay `forgotten_at`.

**Quyết định P2.4c**: **Hard DELETE**. Lý do:
1. Semantic memory dùng trong prompt → forgotten_at cũng phải WHERE `... AND forgotten_at IS NULL`
   ở mọi recall — 1 chỗ quên = leak forgotten thought vào prompt. Hard delete không có surface.
2. GDPR right-to-be-forgotten aligned — user click "quên" ⇒ dữ liệu biến mất khỏi DB.
3. Không có audit requirement cho user memory (khác pending_reply có §8.1 training).

Comment trong `db/models.py::AssistantConversation`:
> Soft-delete (`deleted_at IS NULL` cho row sống) — Phase 2.4 CRUD dùng. Partial index
> scope theo predicate đó.

**Quyết định P2.4c**: **Soft-delete cho conversations** (đúng comment). Lý do:
1. `messages` FK CASCADE — hard delete conversation ⇒ mất tất cả tin nhắn. UI có thể muốn
   "khôi phục" trong tương lai (P2.5). Soft giữ option đó.
2. Partial index `idx_assistant_conv_user_updated WHERE deleted_at IS NULL` đã sẵn — list
   query tự dùng.

## Bất biến chạm

- **I1c** — cả 2 file mới (`agent/assistant_conversations.py`, `api/assistant_crud.py`) đều
  chỉ dùng ở luồng A. `assistant_conversations` add vào `forbidden_modules` của contract I1c.
  `api/assistant_crud` không cần add (contract I1c cover `agent.assistant_*`; `api.*` không
  bị luồng B import theo I1 nguyên gốc).
- **I2** — bảng `assistant.*` đã có D2 grant cho `svc_ohana_ai`. CRUD dùng INSERT/UPDATE/
  DELETE — grant đã cover ở a12 `GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES`.
- **U-scope hard filter** — MỌI query CRUD phải `WHERE user_id = user_scope`, đứng TRƯỚC
  `ORDER/LIMIT`. Con đường 404-vs-403 (spec: 404 khi cross-user — không leak sự tồn tại của
  resource người khác). Repo `__init__(user_scope=...)` cùng bài `AssistantMemory`.
- **HTTP status contract**:
  - Missing cookie ⇒ 401 (từ `user_identity_from_cookie`).
  - Cross-user resource (id thuộc user khác) ⇒ 404 (KHÔNG 403 — 403 leak sự tồn tại).
  - Body invalid (title > 200 chars, empty) ⇒ 422 (Pydantic).
  - Success create ⇒ 201; success delete ⇒ 204; success get/list ⇒ 200.

## Design layer

### Repo: `AssistantConversations` (agent/assistant_conversations.py)

```python
class AssistantConversations:
    def __init__(self, session_factory, *, user_scope: str) -> None: ...
    async def create(self, title: str | None) -> ConversationRow: ...
    async def list_recent(self, limit: int, before_updated_at: datetime | None) -> list[ConversationRow]: ...
    async def get(self, conversation_id: int) -> ConversationRow | None: ...  # None khi cross-user hoặc deleted
    async def update_title(self, conversation_id: int, title: str) -> bool: ...  # False khi 404
    async def soft_delete(self, conversation_id: int) -> bool: ...  # False khi 404
```

- `list_recent` pagination bằng cursor (`before_updated_at`) không offset — tránh page-drift
  khi conversation mới xen kẽ. `limit` default 20, max 100.
- `get`/`update_title`/`soft_delete` đều WHERE `user_id = user_scope AND deleted_at IS NULL`
  — cross-user hoặc đã soft-delete đều 404.

### Repo: mở rộng `AssistantMemory`

```python
async def list_memories(self, limit: int, before_created_at: datetime | None) -> list[MemoryRow]: ...
async def delete_memory(self, memory_id: int) -> bool: ...  # False khi 404
```

- `list_memories` KHÔNG trả `embedding` (nặng, không hữu ích cho UI). Trả `memory_id, content, created_at`.
- `delete_memory` WHERE `user_id = user_scope AND memory_id = ?` — cross-user ⇒ rowcount=0 ⇒ False.

### Router: `api/assistant_crud.py`

Mount cùng prefix `/assistant`:
- `POST   /assistant/conversations`         → 201 `{conversation_id, title, created_at}`
- `GET    /assistant/conversations`         → 200 `{items, next_cursor}`
- `GET    /assistant/conversations/{id}`    → 200 `{conversation_id, title, created_at, updated_at}` | 404
- `PATCH  /assistant/conversations/{id}`    → 200 `{...}` | 404 (body: `{title: str}`)
- `DELETE /assistant/conversations/{id}`    → 204 | 404
- `GET    /assistant/memories`              → 200 `{items, next_cursor}`
- `DELETE /assistant/memories/{id}`         → 204 | 404

- KHÔNG trả list messages ở GET /conversations/{id} — P2.4c không persist messages, nên list
  luôn rỗng. Endpoint `GET /conversations/{id}/messages` để P2.4d cùng chat persistence.
- Body validation Pydantic: `title: str` min_length=1 max_length=200; `create` body optional
  `title` (nullable — user có thể tạo hội thoại rỗng, đặt tên sau).

## Test surface

### Repo (Postgres integration, cùng bài `test_assistant_memory.py`):
- `test_conversations_create_returns_row`
- `test_conversations_list_paginated_by_cursor`
- `test_conversations_list_user_scope_hard_filter` — user A list không thấy user B.
- `test_conversations_get_returns_none_cross_user` — user A get id của user B ⇒ None.
- `test_conversations_get_returns_none_soft_deleted`
- `test_conversations_update_title_returns_false_cross_user`
- `test_conversations_soft_delete_flips_deleted_at`
- `test_memory_list_paginated` (mở rộng test_assistant_memory)
- `test_memory_delete_returns_false_cross_user`
- `test_memory_delete_removes_row_hard`

### Endpoint (TestClient + fakeredis + FakeEmbedder + FastAPI dep override):
- `test_conversations_create_401_when_missing_cookie`
- `test_conversations_create_happy_returns_201`
- `test_conversations_list_returns_only_own`
- `test_conversations_patch_404_cross_user`
- `test_conversations_delete_204_then_get_404`
- `test_memories_delete_204_then_list_missing`

## Fail modes

- DB down ⇒ 500 (SQLAlchemy raise, không catch — cùng bài Tầng 3 CRUD).
- Body invalid ⇒ 422 (Pydantic tự).
- Cross-user ⇒ 404 (KHÔNG 403 — không leak existence).
- Cookie hết hạn / sai role ⇒ 401 (`user_identity_from_cookie` đã handle).

## Verify

```bash
alembic upgrade head  # Không migration mới ở P2.4c
ruff check . --no-cache && ruff format --check . --no-cache
mypy app agent retrieval parsing db bridge tools api auth
lint-imports
pytest -q  # phải giữ 451 → 460+ passed
```
