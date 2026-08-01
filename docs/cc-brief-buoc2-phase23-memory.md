# CC Brief · Bước 2 · Phase 2.3 — Memory per-user (save + recall)

**Frozen** · form `ohana-be-coder` · scope decision: 1 PR (primitive only, chưa consume).

## Bối cảnh & phạm vi

Phase 2.1 (`8aba78b`) đã ship `assistant.user_memory` table + HNSW index. Phase 2.2
(`cff1b98`) đã ship Redis primitives (cost + rate-limit). Phase 2.3 ship **primitive
memory**: SAVE và RECALL bằng semantic HNSW, tách khỏi router (P2.4 consume).

**Trong scope:**
- `agent/assistant_memory.py` — `AssistantMemory` class (save + recall) + `MemoryHit` dc.
- `save_text(content)` → embed passage + INSERT → memory_id.
- `recall_text(query, k)` → embed query + HNSW cosine + WHERE user_id = user_scope LIMIT k.
- E5 **asymmetric embed** (I11): `embed_documents` cho save, `embed_query` cho recall.
- **user_scope BẮT BUỘC** ở constructor (không default) — cùng bài `PgvectorRetriever.
  shop_scope`: hai lỗi (không type-permitted) mới leak được.
- Tests (Postgres integration + FakeEmbedder deterministic).

**KHÔNG scope (Phase 2.4+):**
- Chat router consume memory (P2.4).
- Forget/DELETE endpoint — ADR model comment nói P2.4 quyết DELETE thật vs `forgotten_at`.
- Consolidation / summarization (memory advanced, phase sau).
- Cross-conversation retrieval / join với `messages` (memory là 1-hop text→embed→hit).
- Consume Redis từ P2.2 (memory không cần counter/rate-limit ở tầng primitive).

## Hồ sơ ADR (đọc trước khi code)

`docs/adr-tang2-ohana-ai-assistant.md` §3 **Hệ quả kiến trúc**:
> Memory = per-user semantic (namespace `mem:user:{id}`, append-only, vector recall),
> embedding e5 1024d dùng chung pgvector (`EMBED_DIM=1024`, xác nhận từ design doc).

**`db/models.py::AssistantUserMemory` docstring** (P2.1):
> Append-only tại Phase 2.1 — `forget` endpoint (Phase 2.4) quyết DELETE thật hay
> `forgotten_at`. `embedding vector(1024)` khớp EMBED_DIM=1024 (e5). HNSW viết tay trong
> migration (autogen không thấy).

**`db/migrations/versions/a12_assistant_schema.py:106-132`** — table + 2 index đã có:
- `idx_assistant_user_memory_hnsw` (HNSW `embedding vector_cosine_ops`)
- `idx_assistant_user_memory_user` (btree `user_id, created_at DESC`)

## Bất biến chạm

- **I11 · embed prefix** — save dùng `embed_documents` (passage), recall dùng `embed_query`
  (query). E5 bất đối xứng: trộn hai bên KHÔNG crash, chỉ recall tệ đi âm thầm.
  Cưỡng chế bằng CODE (không phải type): call `embedder.embed_documents([content])` và
  `embedder.embed_query(query)`, KHÔNG gọi thẳng `embed()`. Test tường minh (mock
  embedder + assert method gọi đúng) để refactor sau lỡ đổi thành `embed()` bắt được.
- **U-scope hard filter** (analog I15 tenant isolation) — `user_scope` REQUIRED ở
  constructor, WHERE `user_id = user_scope` đứng TRƯỚC `ORDER BY dist LIMIT k`. Một
  memory user khác gần hơn về vector KHÔNG được leak. Test đối chứng: save user A,
  recall user B → 0 hits (gate).
- **I1c** (P2.2) đã cover `agent.assistant_*` — module mới `agent.assistant_memory` tự
  động thuộc luồng A, luồng B (bridge/channels/api.webhook/api.inbox) forbidden.
- **I2** · KHÔNG chạm grant (D2 P2.1 đã mở `svc_ohana_ai` FULL trong `assistant.*`).
- **I14** · KHÔNG bảng mới (dùng `assistant.user_memory` sẵn có).

## Việc — thứ tự thi công

### 1. `agent/assistant_memory.py`

```python
@dataclass(frozen=True)
class MemoryHit:
    memory_id: int
    content: str
    score: float          # cosine distance (nhỏ = gần hơn)
    created_at: datetime


class AssistantMemory:
    """Memory per-user Tầng 2. Save + recall, HNSW cosine, user_scope hard filter."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        embedder: Embedder,
        *,
        user_scope: str,
    ) -> None:
        if not user_scope:
            raise ValueError("user_scope is required — no default, no cross-user surface")
        self._sm = session_factory
        self._embedder = embedder
        self._user_scope = user_scope

    async def save_text(self, content: str) -> int:
        """Embed content (passage prefix) + INSERT. Trả memory_id."""
        if not content or not content.strip():
            raise ValueError("content must be non-empty")
        (vec,) = await self._embedder.embed_documents([content])
        # INSERT ... RETURNING memory_id — atomic, không cần round-trip.
        stmt = (
            insert(AssistantUserMemory)
            .values(user_id=self._user_scope, content=content, embedding=vec)
            .returning(AssistantUserMemory.memory_id)
        )
        async with self._sm() as session:
            memory_id = (await session.execute(stmt)).scalar_one()
            await session.commit()
        return int(memory_id)

    async def recall_text(self, query: str, k: int) -> list[MemoryHit]:
        """Embed query (query prefix) + SELECT k gần nhất trong scope. WHERE user_id
        đứng trước ORDER — cross-user leak bị cấm ở SQL, không phải post-filter."""
        if not query or not query.strip():
            raise ValueError("query must be non-empty")
        if k <= 0:
            return []
        query_vec = await self._embedder.embed_query(query)
        dist = AssistantUserMemory.embedding.cosine_distance(query_vec).label("dist")
        stmt = (
            select(
                AssistantUserMemory.memory_id,
                AssistantUserMemory.content,
                AssistantUserMemory.created_at,
                dist,
            )
            .where(AssistantUserMemory.user_id == self._user_scope)
            .order_by(dist)
            .limit(k)
        )
        async with self._sm() as session:
            rows = (await session.execute(stmt)).all()
        return [
            MemoryHit(memory_id=int(r[0]), content=r[1], created_at=r[2], score=float(r[3]))
            for r in rows
        ]
```

### 2. `tests/test_assistant_memory.py`

Postgres integration (pgvector), FakeEmbedder deterministic từ `tests/test_wiki_rag.py`
(sparse keyword-slot hash — reproducible, không cần network).

- `test_save_returns_memory_id` — save one, memory_id > 0.
- `test_recall_returns_hits_ordered_by_similarity` — save 3 với semantics khác nhau,
  query gần với 1 → 1st hit là câu đó.
- `test_user_scope_hard_filter` — save user A, `AssistantMemory(user_scope="user-B")
  .recall_text(...)` → 0 hits kể cả gần hơn về vector.
- `test_recall_respects_k_limit` — save 5, recall k=2 → len(hits) == 2.
- `test_recall_empty_when_no_memories` — user chưa save → [].
- `test_recall_zero_k_returns_empty` — k=0 → [] (không round-trip DB).
- `test_constructor_requires_user_scope` — empty string → ValueError.
- `test_save_uses_embed_documents_prefix` — mock embedder assert `embed_documents` gọi,
  KHÔNG `embed_query`/`embed` (I11).
- `test_recall_uses_embed_query_prefix` — mock embedder assert `embed_query` gọi,
  KHÔNG `embed_documents`/`embed` (I11).
- `test_save_rejects_empty_content` — "" hoặc "   " → ValueError.
- `test_recall_rejects_empty_query` — "" hoặc "   " → ValueError.

## Chống drift

- **KHÔNG** gọi thẳng `embedder.embed()` — luôn qua `embed_documents`/`embed_query`.
  I11 test bắt regression.
- **KHÔNG** post-filter `user_id` ở Python (fetch tất cả rồi filter) — WHERE ở SQL,
  trước ORDER. Cross-user leak = SQL sai, không phải Python sai.
- **KHÔNG** default `user_scope=""` — required. Empty string đầu ra ValueError, không
  âm thầm scope rỗng khớp mọi row.
- **KHÔNG** dùng `PgvectorRetriever` — memory schema khác (assistant.user_memory vs
  platform.corpus/embeddings), constraint khác (user_scope vs shop_scope + namespaces).
  Share code = kéo namespace/shop_scope vào memory nhầm.
- **KHÔNG** thêm `forgotten_at` hay DELETE endpoint (out of scope, P2.4).
- **KHÔNG** thêm bảng, migration, grant (schema đã đủ ở P2.1).
- **KHÔNG** wire vào `main_ohana_ai` (P2.4 consume qua router).
- **KHÔNG** join với `assistant.messages` để "recall conversation nearest" — memory là
  1-hop text→embed→hit; conversation retrieval là scope khác (P2.4+).

## Verify

```bash
export DATABASE_URL="postgresql+psycopg://ohana:PW@localhost:5433/ohana_test"

# Test module mới (Postgres integration + FakeEmbedder)
pytest tests/test_assistant_memory.py -v

# Full suite regression
pytest --ignore=web -q

# Lint + type + import
ruff check . && ruff format --check .
mypy app agent retrieval parsing db bridge tools api auth
lint-imports  # kỳ vọng 5 kept, 0 broken (I1c cover assistant_memory tự động)
```

**Kỳ vọng:**
- Test mới GREEN (≥11 test).
- Full pytest 400+ pass không regress (baseline 400 sau P2.2).
- lint-imports 5 kept (assistant_memory rơi vào scope I1c hiện có, không cần contract mới).

## Rollback

Test-only + 1 module mới ⇒ `git revert`. Không migration, không grant, không compose,
không lifespan wire.

## Ghi chú kiến trúc (không phải việc)

- **P2.4 sẽ consume:** chat router:
  1. Nhận message.
  2. `memory.recall_text(query=message, k=5)` — augment context.
  3. Gọi LLM với context + memory hits.
  4. `memory.save_text(content=summary_or_message)` (heuristic quyết khi nào save;
     đơn giản nhất: save mọi turn của user).
- **Memory schema đã ổn** — nếu về sau cần metadata (source, importance), thêm cột
  vào `assistant.user_memory` (migration mới) chứ KHÔNG tạo bảng bên.
- **HNSW default params** (m=16, ef_construction=64) — chưa đo recall. Nếu P2.4 hoặc
  eval golden set (nếu có) cho thấy recall tệ, tune trong migration mới (cùng họ ISSUE-022
  ghi ở a12 comment).
