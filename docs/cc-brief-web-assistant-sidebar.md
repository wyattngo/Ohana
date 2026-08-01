# CC Brief · Web · Assistant sidebar (Tầng 2 FE — F1)

**Trạng thái:** DRAFT — chưa code. Chốt scope trước khi phát lệnh.
**Form:** `ohana-web-coder` (khi ship). Scope 1 PR.

## Bối cảnh

BE Tầng 2 đã đủ endpoint cho một chat có state:

| Ship | Endpoint | Đã có |
|---|---|---|
| P2.4b | `POST /api/assistant/chat` | ✓ |
| P2.4c | `POST/GET/PATCH/DELETE /api/assistant/conversations[…]` | ✓ |
| P2.4c | `GET/DELETE /api/assistant/memories[/id]` | ✓ |
| P2.4d | `GET /api/assistant/conversations/{id}/messages` + `append_pair` | ✓ |

FE hiện chỉ có [Chat.tsx](web/src/screens/Chat.tsx) → gọi `/api/chat` **luồng B seller (Tầng 3)** — stateless, không history, không memory. Tầng 2 chưa có bất kỳ màn hình nào.

F1 = mount lối vào Tầng 2 đầu tiên: một chat có **sidebar list conversations** + **history của một conversation** (2 GET + 1 POST + 1 DELETE). Đủ để trở thành sản phẩm demo được cho người thứ 3, không mở rộng ra memory UI/search/edit title.

## Trong scope

- Tạo màn mới `web/src/screens/Assistant.tsx` — 2 pane (desktop) / drawer + main (mobile):
  - **Sidebar left**: list conversations, "+ Cuộc mới", nút delete per row (native confirm).
  - **Main right**: transcript của conversation đang mở, composer y hệt `Chat.tsx` (Enter gửi, Shift+Enter xuống dòng, disable khi sending).
- Mở rộng [lib/api.ts](web/src/lib/api.ts): 4 hàm mới (`listConversations`, `fetchMessages`, `postAssistantChat`, `deleteConversation`).
- Chrome nav [App.tsx](web/src/App.tsx): thêm 1 nút "**Trợ lý AI**" (đặt trước "Hỏi AI" để dominant hơn). Cùng chỗ 3 nút hiện tại.
- Toast reuse (cùng level App.tsx như 6 màn cũ).
- Playwright e2e file mới `assistant-sidebar.spec.ts`.

## Ngoài scope (dời F2+ hoặc backlog)

- **Router URL** — refresh giữa chat mất state, quay về `channel`. Parity với hành vi cả app hiện tại; router (hash-router 30 dòng) tách PR riêng khi cần deep-link.
- **Edit conversation title** — endpoint `PATCH` có sẵn, UI để sau (không có UX contract rõ: inline edit vs. modal).
- **Memory UI** — không list/xoá memory từ FE, dù BE có `GET/DELETE /memories`. Người dùng chưa cần cửa sổ này ở MVP.
- **Search conversations** — cursor pagination có sẵn, nhưng search full-text chưa có endpoint.
- **Streaming (SSE)** — chat endpoint hiện non-stream. Nếu sau thêm, persist ở `StreamDone` (đã note P2.4d).
- **Tier badge / daily token counter** — response chat có `tier` + `daily_tokens_used`, F1 không hiển thị (thêm ở F2 khi có UX cho quota).
- **Cost cap 429 UX riêng** — hiện dùng chung fallback error toast, F2 mới tách.

## Prerequisite (chưa có, phải giải quyết trước khi ship)

- **Mock user auth cho Tầng 2.** Cookie `ohana_session` share với seller/admin, nhưng token phải `role="user"` + `tier` field. [api/mock_auth.py](api/mock_auth.py) hiện chỉ mint `seller`/`admin`. Cần thêm nhánh `role=user` mint token với `tier="free"|"pro"` (default free).
  - **Scope quyết định trước khi code F1:** làm mock user auth trong PR F1 luôn, hay tách PR BE riêng? Đề xuất: tách PR nhỏ BE trước (thêm 1 nhánh trong `mock_authorize`, ~10 dòng + 2 test), F1 mount lên.
  - Không có prerequisite này ⇒ `postAssistantChat` return 401 vì cookie chỉ có seller token.

## Bất biến chạm

- **Không sửa `Chat.tsx`.** Tầng 3 (seller-facing, gọi `/api/chat`) và Tầng 2 (per-user, `/api/assistant/chat`) là hai product surface khác nhau. Đè Tầng 2 lên Chat.tsx = trộn hai contract khác nhau (Tầng 3 không có memory/conversation, Tầng 2 có), UX + type cùng loạn.
- **Không gửi `user_id` từ FE.** Cùng luật với Tầng 3: identity từ cookie server-verify, `AssistantChatIn`/`ConversationCreateIn` không nhận user_id. Sending sẽ silent-ignore ở BE (`extra="ignore"`) — cấm chủ động gửi để không cấy mental model sai.
- **CSRF double-submit** — tất cả POST/PATCH/DELETE đi qua `apiFetch` sẵn có (đọc cookie `ohana_csrf`, set header `X-CSRF-Token`). Không tạo `fetch` mới ngoài `apiFetch`.
- **Ownership hard filter là job của BE** — repo layer đã WHERE `user_id`, endpoint đã 404 cross-user. FE chỉ pass id, không guard client-side (không có "user_id của tôi" trong context — cookie mới là ground truth).
- **Auto-title do BE tạo** (message[:40] khi `conversation_id=null`). FE KHÔNG tự sinh title trước rồi gửi — nếu FE optimistic set title từ đầu, BE trả lại title khác (VD trim khoảng trắng khác) sẽ desync silent. Chờ response.

## Design layer

### Data flow

**Load list (mount + sau send):**
```
GET /api/assistant/conversations?limit=20
→ items ordered by updated_at DESC (BE đã sort)
→ render sidebar
```

**Open conversation (click sidebar row):**
```
GET /api/assistant/conversations/{id}/messages?limit=50
→ items ASC by created_at (BE đã sort, tie-break message_id)
→ render turns (role=user | assistant)
```

**Send message:**
```
POST /api/assistant/chat
  body: { message, conversation_id: activeConvId ?? null }
→ response: { reply, conversation_id, tier, ... }

FE:
- Optimistic append turn user vào transcript trước khi await (parity Chat.tsx).
- Sau response OK: append turn assistant, set activeConvId = response.conversation_id.
- Nếu tạo mới (activeConvId was null): refetch list conversations (title BE tự tạo). Bring row lên đầu.
- Nếu đã có: optimistic bump row lên đầu, keep title.
- Error: rollback user turn, restore draft (parity Chat.tsx).
```

**Delete conversation:**
```
window.confirm("Xoá cuộc này? Không hoàn tác được.") → OK
DELETE /api/assistant/conversations/{id}
→ 204 → remove row khỏi list; nếu là activeConv, clear main pane về empty state.
```

**Đổi conversation active:** không load lại list, chỉ set state + fetch messages.

### Component tree

```
Assistant.tsx
├── AssistantSidebar (list + new + delete)
│   ├── "+ Cuộc mới" button → setActiveConvId(null) + clear transcript
│   ├── ConversationRow[] → title | fallback "Untitled" | timestamp
│   └── DeleteButton per row (icon-only, tap area 44px)
└── AssistantMain
    ├── Empty state (khi activeConvId=null AND turns=[]): copy hướng dẫn
    ├── Transcript (reuse .chat-transcript, .chat-turn CSS từ Chat.css — extract sang shared? Không, copy vào Assistant.css để không chạm Chat.tsx)
    └── Composer (textarea + Send, y hệt Chat.tsx nhưng gọi postAssistantChat)
```

### State shape (local, không cần TanStack Query cho MVP)

```ts
interface AssistantState {
  conversations: ConversationRow[] | null;  // null = loading, [] = empty
  activeConvId: number | null;              // null = new conversation
  turns: Turn[];                            // active conv's messages
  turnsLoading: boolean;
  sending: boolean;
  draft: string;
  loadError: string | null;
}
```

3 side-effect boundary: mount → load list; activeConvId change → load messages; send → post + reload list.

### Responsive layout (mobile 430px là baseline)

- **Mobile (<768px):** sidebar là drawer trượt từ trái, mặc định đóng. Nút hamburger góc trái top của main. Full-screen main.
- **Desktop (≥768px):** sidebar cố định 280px trái, main flex-1. Không drawer.

Không dùng `react-router-dom`. Không dùng thư viện drawer — CSS transform + state boolean.

### API client (thêm vào [lib/api.ts](web/src/lib/api.ts))

```ts
export interface ConversationRow {
  conversation_id: number;
  title: string | null;
  created_at: string;     // ISO
  updated_at: string;
}

export interface ConversationListResult {
  items: ConversationRow[];
  next_cursor: string | null;
}

export interface MessageRow {
  message_id: number;
  role: "user" | "assistant" | "system";
  content: string;
  created_at: string;
}

export interface AssistantChatResult {
  reply: string;
  model: string;
  grounded: boolean;
  usage: Record<string, number>;
  tier: string;
  daily_tokens_used: number;
  conversation_id: number;
}

// F1: chỉ dùng 4 hàm này. list_conversations không dùng cursor (limit 20 đủ cho MVP).
export async function listConversations(): Promise<ConversationListResult>;
export async function fetchMessages(id: number): Promise<MessageRow[]>;
export async function postAssistantChat(message: string, conversationId: number | null): Promise<AssistantChatResult>;
export async function deleteConversation(id: number): Promise<void>;
```

### Empty / error states

| Trạng thái | Copy VN | Action |
|---|---|---|
| Loading list | Spinner + "Đang tải cuộc hội thoại…" | — |
| List load fail | TriangleAlert + "Không tải được danh sách." | Nút "Thử lại" |
| List empty | Icon MessageCircle + "Chưa có cuộc nào. Gõ câu hỏi để bắt đầu." | Focus composer |
| Active=null, turns=[] | Icon Sparkles + "Chào — trợ lý cá nhân của bạn." | — |
| Messages load fail | Toast error, main clear | — |
| Send fail 401 | Toast "Phiên đăng nhập hết hạn." | — |
| Send fail 429 | Toast "Đã đạt giới hạn hôm nay." | — (F2 sẽ tách) |
| Send fail 502 | Toast "Model không trả lời — thử lại." | draft khôi phục |

## Test surface

### Playwright e2e ([web/e2e/assistant-sidebar.spec.ts](web/e2e/assistant-sidebar.spec.ts))

Mock endpoints qua `page.route(**/api/…)` giống pattern `helpers.ts`.

- `test_sidebar_renders_conversations_from_api` — mock list 3 conv → thấy 3 row với title (fallback "Untitled" khi title=null).
- `test_click_row_loads_messages` — mock messages 4 turn → thấy 4 bong bóng đúng thứ tự role.
- `test_new_conversation_button_clears_active` — click "+ Cuộc mới" → transcript trống, composer focus.
- `test_send_creates_conversation_when_active_null` — mock POST /assistant/chat trả conversation_id=99 → sidebar refetch, row mới lên đầu, activeConvId=99.
- `test_send_appends_when_active_set` — pre-set active=1, send → 2 turn append (user + assistant).
- `test_send_error_502_restores_draft_and_removes_user_turn` — mock 502 → toast, draft khôi phục, transcript rollback.
- `test_delete_conversation_removes_row` — click delete + confirm → row biến mất; nếu là active, main về empty state.
- `test_mobile_drawer_opens_and_closes` — resize viewport 375, click hamburger → drawer visible; click backdrop → đóng.

### Không phải test target ở FE (BE đã cover):

- Cross-user 404 → BE test.
- Ownership check → BE test.
- Auto-title từ first message → BE test.
- CSRF enforcement → BE test (FE chỉ kiểm test-oxlint cấm gọi trực tiếp `fetch`).

## Fail modes

- **Race send + delete cùng conversation**: user gửi + xoá tab khác. POST trả 200 (conv đã bị soft-delete? BE hiện tại chưa recheck trong `append_pair` — kiểm tra: nếu `soft_delete` set flag, `create()` skip, nhưng `append_pair` được gọi khi có `conv_id` valid tại thời điểm resolve). ⇒ **Cần verify BE behaviour trước khi ship**, không phải fix ở FE.
- **Optimistic sidebar sort race**: 2 conversation cùng bump gần đồng thời, refetch list sẽ ghi đè optimistic. Chấp nhận (server order là ground truth).
- **Cookie hết hạn giữa chat**: turn user hiện, POST 401, rollback user turn + toast + KHÔNG redirect (parity với Chat.tsx hiện tại).
- **Reload page**: mất `activeConvId` + `turns`, load lại list. Chấp nhận (không router).

## Files change hint

```
web/src/App.tsx                        — thêm 1 nút chrome + Screen variant "assistant"
web/src/lib/api.ts                     — 4 hàm mới + 3 interface
web/src/screens/Assistant.tsx          — new (~300 LOC)
web/src/screens/Assistant.css          — new (2-pane + drawer + reuse .chat-turn)
web/e2e/assistant-sidebar.spec.ts      — new (~200 LOC, 8 test)
web/e2e/helpers.ts                     — thêm mockAssistantRoutes helper
```

BE PR nhỏ tách trước (prerequisite):
```
api/mock_auth.py                       — thêm nhánh role=user + tier claim
auth/user_identity.py                  — (chỉ verify) already ready
tests/test_mock_auth.py                — +2 test cho user role
```

## Verify (khi ship)

```bash
cd web
pnpm oxlint
pnpm build                       # tsc -b && vite build
pnpm test:e2e assistant-sidebar  # Playwright headless
```

## Judgment calls chờ chốt

1. **Delete UX** — native `window.confirm` (parity ReviewCard) vs. modal có branded? Đề xuất confirm — không đáng chi phí modal cho MVP.
2. **Sidebar cap** — hiện `limit=20`, không có "Load more". User >20 conv sẽ mất tab. F1: chấp nhận (bảo user tự xoá cũ); F2 mới thêm cursor pagination sidebar khi con số này thành pain point.
3. **Timestamp format** — hiển thị `updated_at` như thế nào trong row? `dd/MM` (VN short) vs. `2 giờ trước` (relative). Đề xuất relative dưới 24h, `dd/MM` sau đó — cần 1 hàm helper trong `lib/intent.ts` hoặc file mới `lib/time.ts` (~15 LOC).
4. **Route mock user auth vào channel picker?** Hiện có "Vào với quyền quản trị (dev)" — thêm nút thứ 3 "Vào với quyền user (dev)"? Hoặc chuyển 3 dev button thành dropdown? Đề xuất: thêm nút thứ 3 — không đáng refactor picker cho dev-only chrome.
