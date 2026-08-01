# CC Brief · Bước 2 · Phase 2.4d — Chat persistence (`assistant.messages`)

**Frozen** · form `ohana-be-coder` · scope decision: 1 PR (repo + endpoint changes + tests).

## Bối cảnh & phạm vi

P2.4b (`04cdd6d`) shipped `/api/assistant/chat` stateless. P2.4c (`662fd4b`) shipped
CRUD conversations + memories. Bảng `assistant.messages` đã có schema (P2.1) nhưng
KHÔNG có row nào — chat không persist. P2.4d nối hai đầu: chat endpoint persist cặp
(user_msg, assistant_msg) vào `messages` scoped theo `conversation_id`.

**Trong scope:**
- Mở rộng `agent/assistant_conversations.py`:
  - `append_pair(conversation_id, user_content, assistant_content) -> tuple[int, int]`
    — 1 transaction, INSERT 2 row (`role=user` rồi `role=assistant`) + bump
    `conversations.updated_at` để item nhảy lên đầu list.
  - `list_messages(conversation_id, limit, before_created_at) -> list[MessageRow]` —
    cursor pagination `created_at ASC` (khớp thứ tự đọc hội thoại).
- Sửa `api/assistant_chat.py`:
  - Body add `conversation_id: int | None = None`.
  - `None` ⇒ auto-create conversation (title = user_msg[:40] trimmed, hoặc None nếu
    empty sau trim).
  - Non-None ⇒ ownership check qua `AssistantConversations.get()`; None ⇒ 404
    `conversation_not_found` (không leak existence).
  - Sau LLM success: gọi `append_pair()` trong 1 transaction.
  - Response body add `conversation_id`.
- Sửa `api/assistant_crud.py`:
  - `GET /assistant/conversations/{id}/messages?limit=&before=` — ownership check
    trước, list_messages, cursor pagination.

**KHÔNG scope (dời P2.5+ hoặc backlog):**
- Bulk delete messages / export.
- Edit / redo message (không có contract UX rõ).
- System message riêng (P2.4d chỉ persist `user` + `assistant`; ENUM `msg_role` có
  `system` — dùng sau nếu cần).
- Streaming persist (chat endpoint hiện non-stream `.step()` — nếu ai đó thêm stream,
  persist ở `StreamDone` chứ không giữa dòng).

## Bất biến chạm

- **U-scope hard filter** — cả `append_pair`/`list_messages` đều WHERE `user_id =
  user_scope` (redundant với FK conversation → user, nhưng schema đã lock:
  `messages.user_id NOT NULL` là chủ đích ở P2.1 để lock scope repo layer).
- **1 transaction cho append_pair** — 2 INSERT trong cùng `async with session()`,
  commit cuối. Crash giữa ⇒ ROLLBACK, không có "user message không có assistant reply"
  trong DB (mất consistency history).
- **HTTP 404 cross-user** — cùng bài P2.4c: conversation_id thuộc user khác ⇒ 404,
  không 403.
- **Chat endpoint contract**: LLM fail ⇒ 502 (đã có), KHÔNG persist gì (transaction
  chưa run). Redis fail ⇒ D4 fail-open (đã có), persist vẫn chạy.
- **Auto-create title**: `user_msg[:40].strip()` — nếu whitespace-only trim ra rỗng
  thì `title=None` (UI hiển thị "Untitled"). KHÔNG lưu title `""`.

## Design layer

### Repo: mở rộng `AssistantConversations`

```python
@dataclass(frozen=True)
class MessageRow:
    message_id: int
    role: str  # "user" | "assistant" | "system"
    content: str
    created_at: datetime

async def append_pair(
    self,
    conversation_id: int,
    *,
    user_content: str,
    assistant_content: str,
) -> tuple[int, int]:
    """1 transaction: INSERT user + INSERT assistant + UPDATE conversations.updated_at.
    Trả (user_message_id, assistant_message_id). Cross-user hoặc soft-deleted
    conversation ⇒ endpoint đã 404 trước; repo không recheck (tin caller đã get())."""

async def list_messages(
    self,
    conversation_id: int,
    limit: int,
    before_created_at: datetime | None = None,
) -> list[MessageRow]:
    """ASC by created_at (đọc từ đầu hội thoại). Cursor = created_at của item cuối
    page trước. Cross-user check ở endpoint layer (get() first)."""
```

### Endpoint: sửa `api/assistant_chat.py`

```python
class AssistantChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    conversation_id: int | None = None

class AssistantChatOut(BaseModel):
    reply: str
    model: str
    grounded: bool = False
    usage: dict[str, int] = Field(default_factory=dict)
    tier: str
    daily_tokens_used: int
    conversation_id: int  # ← ADDED
```

Flow:
1. Tier gate (unchanged).
2. Nếu `payload.conversation_id`: `conv = await conversations.get(id)`; None ⇒ 404.
3. Nếu None: `conv = await conversations.create(title=payload.message[:40].strip() or None)`.
4. Memory recall (unchanged, dùng cùng identity.user_id).
5. LLM step (unchanged).
6. `await conversations.append_pair(conv.conversation_id, user_content=payload.message, assistant_content=reply)`.
7. Response include `conversation_id=conv.conversation_id`.

Persistence ĐỨNG SAU LLM success — LLM fail (502) ⇒ 0 message persist (đúng bài
"không tạo bong bóng rỗng").

### Endpoint: add `GET /conversations/{id}/messages`

```python
@router.get("/conversations/{conversation_id}/messages", response_model=MessageListOut)
async def list_conversation_messages(
    conversation_id: int,
    identity: UserIdentity = Depends(user_identity_from_cookie),
    limit: int = Query(50, ge=1, le=200),
    before: datetime | None = Query(None),
) -> MessageListOut:
    repo = AssistantConversations(session_factory, user_scope=identity.user_id)
    # Ownership check TRƯỚC list.
    if await repo.get(conversation_id) is None:
        raise HTTPException(status_code=404, detail="conversation_not_found")
    rows = await repo.list_messages(conversation_id, limit=limit, before_created_at=before)
    next_cursor = rows[-1].created_at if len(rows) == limit else None
    return MessageListOut(items=[...], next_cursor=next_cursor)
```

## Test surface

### Repo (Postgres integration):
- `test_append_pair_inserts_two_rows_in_order` — role user rồi assistant, message_id user < assistant
- `test_append_pair_bumps_conversation_updated_at` — updated_at > lúc create
- `test_append_pair_transaction_rollback_on_failure` — patch commit raise ⇒ 0 row (best effort test)
- `test_list_messages_ascending` — role/content đúng thứ tự chronological
- `test_list_messages_cursor_pagination` — page 2 dùng cursor
- `test_list_messages_empty_for_new_conversation` — trả []

### Endpoint chat (fakeredis + FakeLLM + FakeEmbedder):
- `test_chat_creates_conversation_when_id_none` — response.conversation_id > 0, GET /messages có 2 row
- `test_chat_uses_provided_conversation_id_and_appends` — chat 2 lần cùng id ⇒ 4 messages
- `test_chat_404_when_conversation_id_cross_user` — user A gửi id của user B ⇒ 404
- `test_chat_404_when_conversation_id_missing` — id không tồn tại ⇒ 404
- `test_chat_llm_fail_persists_nothing` — FakeLLM raise ⇒ 502 + list_messages = []
- `test_chat_auto_title_from_first_message` — conversation.title = message[:40]

### Endpoint list messages:
- `test_list_messages_401_no_cookie`
- `test_list_messages_404_cross_user`
- `test_list_messages_returns_ordered_asc`

## Fail modes

- LLM fail ⇒ 502, transaction chưa chạy ⇒ 0 row persist ✓
- Redis fail (rate/cost) ⇒ D4 fail-open (allow) ⇒ chat vẫn 200 ⇒ persist ✓
- Memory recall fail ⇒ log warning + tiếp (existing) ⇒ persist ✓
- append_pair fail giữa transaction ⇒ ROLLBACK, endpoint 500 (không catch — data integrity > UX)
- Cross-user conversation_id ⇒ 404, không leak existence
- Race: user_id đổi cookie giữa 2 request cùng conversation_id ⇒ get() ở token thứ 2
  check ownership ⇒ 404 lần 2

## Verify

```bash
ruff check . --no-cache && ruff format --check . --no-cache
mypy app agent retrieval parsing db bridge tools api auth
lint-imports
pytest -q  # 470 → 485+ passed
```
