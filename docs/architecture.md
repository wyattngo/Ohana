# Architecture — Ohana BE

Chi tiết cho người muốn hiểu code. Overview ngắn ở [README](../README.md); hợp đồng authoritative ở [ohana-be-design.md](ohana-be-design.md).

## 3 process, 4 role Postgres, 1 nginx

**Isolation bằng grant**, không bằng discipline — dù code có bug, `svc_ohana_ai` cũng không đọc được dữ liệu shop vì Postgres role không có quyền.

```
                          Internet (443)
                               │
                               ▼
                        ┌─────────────┐
                        │   nginx     │  TLS terminate + SPA fallback
                        │             │  (build SPA React 19 stage 1,
                        │             │   serve + reverse proxy stage 2)
                        └──┬───┬───┬──┘
              /api/chat    │   │   │    /api/inbox
           /api/assistant  │   │   │    /api/admin
                           │   │   │    /webhook
                           ▼   │   ▼
                 ┌──────────┐  │  ┌─────────────┐
                 │ ohana-ai │  │  │ohana-seller │
                 │   :8001  │  │  │    :8002    │
                 │svc_ohana │  │  │ svc_seller  │
                 │   _ai    │  │  │             │
                 └────┬─────┘  │  └──────┬──────┘
                      │        │         │
                      │        │         ▼
                      │        │   ┌─────────────┐
                      │        │   │ohana-worker │
                      │        │   │  (no port)  │
                      │        │   │ svc_seller  │
                      │        │   └──────┬──────┘
                      ▼        │          │
                 ┌─────────────┴──────────┴─────┐
                 │       postgres:16            │
                 │  (pgvector, isolated by      │
                 │   role grant — I2 / I14)     │
                 └──────────────────────────────┘

                 ┌─────────────┐    ┌───────────────┐
                 │  redis:7    │    │  langfuse:2   │
                 │ (counter,   │    │ (self-host,   │
                 │  no persist)│    │  UI :3000     │
                 │             │    │  localhost    │
                 │             │    │  only)        │
                 └─────────────┘    └───────────────┘
```

Route rule (chi tiết [deploy/nginx.conf](../deploy/nginx.conf)):

| Prefix | Upstream | Luồng |
|---|---|---|
| `/webhook/*` | `ohana-seller:8002` | B — Zalo webhook |
| `/api/inbox\|admin\|mock/*` | `ohana-seller:8002` | B — seller UI + admin |
| `/api/chat\|assistant/*` | `ohana-ai:8001` | A — chat luồng A + Tầng 2 |
| `/*` | SPA fallback | — |

## 16 bất biến

5 điều quan trọng nhất — full list + nguồn gốc ở [ohana-be-design.md §1](ohana-be-design.md):

- **I1** Luồng A ⊥ B, không chung process — 3 entrypoint + `importlinter`
- **I2** `svc_ohana_ai` không đọc dữ liệu shop — Postgres grant + `ALTER DEFAULT PRIVILEGES`
- **I5** SDK provider chỉ trong `agent/providers/` — `importlinter forbidden`
- **I13** Mọi claim có timeout + reaper gỡ được — worker reaper loop
- **I16** Langfuse chỉ nhận `Scrubbed` — hook trong `agent/llm_client.py`

## Cấu trúc thư mục

```
agent/           logic AI: drafter, PII, policy_gate, tier, memory, cost, tracing_context
├── providers/   ⚠ CHỈ chỗ import SDK LLM/tracing (I5)
api/             FastAPI router — webhook, inbox, chat, assistant_*, admin
app/             3 entrypoint (main_ohana_ai + main_seller + worker_seller) + config
auth/            JWT — 2 identity: Identity (Tầng 3, có shop_id) + UserIdentity (Tầng 2)
bridge/          Tầng 3 outbound tool call
channels/        adapter Zalo/FB/TikTok webhook parse
db/              models + session factory + Alembic revisions
parsing/         ingest wiki → chunk 480tok → embed
retrieval/       BM25 + vector + rerank (Tầng 3)
scripts/         bootstrap_roles.py (1 lần/cluster) + ai_coder tooling
tests/           501 test — unit + contract (I2/I14/I15) + eval (khi chạm prompt)
tools/           MCP `mcp_readonly` role wrappers (I15)
web/             React 19 + Vite + Playwright — 6 màn Tầng 3 + 1 màn Tầng 2 (F1 sidebar)
deploy/          docker-compose prod — nginx.conf, entrypoint, .env.prod.example
```

## Reference

- [ohana-be-design.md](ohana-be-design.md) — hợp đồng authoritative (16 bất biến, schema, 10 câu SQL giữ nguyên văn)
- [adr-tang2-ohana-ai-assistant.md](adr-tang2-ohana-ai-assistant.md) — kiến trúc Tầng 2 (D1–D7 ratified)
- [adopt-plan.md](adopt-plan.md) — đã có gì · thiếu gì · thứ tự A1–A8
- [api-reference.md](api-reference.md) — API dev list (endpoint, cookie, error, curl)
