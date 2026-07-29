"""Tool boundary — read tools (F1 Wiki RAG, F2 API Q&A) + write tools (F3, later phases).

Every handler receives `(user_id, shop_id, args)` — user_id + shop_id come from a verified
`auth.identity.Identity`, NEVER from the arguments the LLM emits. This is R1.1 extended for
multi-tenant: the LLM cannot direct a tool at another user or another shop.
"""
