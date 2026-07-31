/**
 * Typed HTTP client for the seller-facing screens (spec 04 §7 Phase P1 step 3).
 *
 * Every request uses `credentials: 'include'` so the browser sends the httpOnly
 * `ohana_session` cookie the backend derives `shop_id` from (auth/identity.py
 * `identity_from_cookie`) — this file never knows or sends `shop_id` itself (spec 04 §10 PC2 /
 * CLAUDE.md R1.22: tenant scope only ever comes from the verified JWT, never the client).
 *
 * State-mutating requests (POST) echo the `ohana_csrf` cookie back as the `X-CSRF-Token`
 * header — the double-submit pair `app/main.py`'s `enforce_csrf_double_submit` middleware
 * checks (spec 04 §10 PC8). GET is a CSRF-safe method so it's exempt, matching the server.
 */

export interface PendingReplyOut {
  reply_id: string;
  conversation_id: string;
  customer_id: string;
  draft_text: string;
  intent: string;
  confidence: number;
  status: string;
}

export interface MockAuthorizeResult {
  oa_id: string;
  shop_id: string;
  role: string;
}

export interface WikiIngestResult {
  success: boolean;
  chunks: number;
}

/**
 * Mirrors `ChatOut` in `api/chat.py`. Kept field-for-field identical on purpose: nothing in
 * the toolchain type-checks Python against TypeScript, so a rename on either side would
 * otherwise surface as a silently `undefined` value rendered as an empty bubble.
 * `tests/test_chat_ui.py::test_typescript_interface_matches_the_python_response_model`
 * introspects `ChatOut` and diffs it against this interface, so drift fails a gate instead.
 *
 * `grounded` is always `false` at GD0 — general chat has no Wiki-RAG, no live stock, no real
 * orders. It is carried through to the UI rather than assumed, so that when a later phase adds
 * grounded answers the screen can tell the two apart instead of guessing from the endpoint.
 */
export interface ChatResult {
  reply: string;
  model: string;
  grounded: boolean;
  usage: Record<string, number>;
}

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message?: string) {
    super(message ?? `request failed with status ${status}`);
    this.status = status;
    this.name = "ApiError";
  }
}

const CSRF_COOKIE_NAME = "ohana_csrf";
const CSRF_HEADER_NAME = "X-CSRF-Token";
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

function readCookie(name: string): string | null {
  const match = document.cookie.match(
    new RegExp(`(?:^|; )${name.replace(/[.$?*|{}()[\]\\/+^]/g, "\\$&")}=([^;]*)`),
  );
  return match ? decodeURIComponent(match[1]) : null;
}

async function apiFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
  const headers = new Headers(init.headers);

  if (!SAFE_METHODS.has(method)) {
    const csrfToken = readCookie(CSRF_COOKIE_NAME);
    if (csrfToken) {
      headers.set(CSRF_HEADER_NAME, csrfToken);
    }
  }

  const response = await fetch(path, { ...init, method, headers, credentials: "include" });
  if (!response.ok) {
    throw new ApiError(response.status);
  }
  return response;
}

/** `GET /api/inbox` — parked drafts for the caller's shop, oldest-first. */
export async function fetchInbox(): Promise<PendingReplyOut[]> {
  const resp = await apiFetch("/api/inbox");
  return (await resp.json()) as PendingReplyOut[];
}

/** `POST /api/inbox/{id}/approve` — flips status to `approved`. Does NOT send the draft to
 * the customer — the send-on-approve worker doesn't exist yet (PRE-004, spec 04 §3 B.3). */
export async function approveReply(replyId: string): Promise<void> {
  await apiFetch(`/api/inbox/${encodeURIComponent(replyId)}/approve`, { method: "POST" });
}

/** `POST /api/inbox/{id}/reject` — flips status to `rejected`. */
export async function rejectReply(replyId: string): Promise<void> {
  await apiFetch(`/api/inbox/${encodeURIComponent(replyId)}/reject`, { method: "POST" });
}

/** `POST /api/mock/authorize` (dev-only, DEC-OHANA-01 U4) — mints the fixture dev session +
 * CSRF cookies. Real Zalo OAuth is spec 05+ (PRE-004). */
export async function mockAuthorize(role: "seller" | "admin" = "seller"): Promise<MockAuthorizeResult> {
  const resp = await apiFetch(`/api/mock/authorize?role=${encodeURIComponent(role)}`, {
    method: "POST",
  });
  return (await resp.json()) as MockAuthorizeResult;
}

/** `POST /api/chat` (spec 07 §7 Phase G1) — seller ↔ AI tổng quát.
 *
 * No `shop_id` in the body, deliberately: the backend derives it from the verified JWT and
 * ignores anything the client claims (`ChatIn` uses `extra="ignore"`). Sending one here would
 * be harmless today but signals the wrong mental model about where tenant scope comes from —
 * `tests/test_chat_ui.py::test_chat_ui_never_sends_shop_id` keeps it that way.
 *
 * Callers must handle a slow first response: a cold Together endpoint measured **24.8s** on
 * the first call and 1.2s afterwards (spec 07 §14). There is no client-side timeout here on
 * purpose — aborting at, say, 10s would turn a working-but-cold request into a failure the
 * seller can only fix by retrying, which costs another paid LLM call.
 */
export async function postChat(message: string): Promise<ChatResult> {
  const resp = await apiFetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message }),
  });
  return (await resp.json()) as ChatResult;
}

/** `POST /api/admin/wiki/ingest` (admin-only, spec 04 §7 Phase P2) — ingests raw text into the
 * shared `_platform` wiki namespace. This client never sends a `shop_id`: the backend defaults
 * it server-side to `PLATFORM_SHOP_ID` (`parsing/ingest.py`) — that constant names a shared
 * namespace, not tenant data (spec 04 §10 PC2 distinguishes this from the R1.22 tenant-scope
 * invariant every other call in this file respects). Requires an admin-role session
 * (`auth.identity.require_admin`); a seller cookie gets `ApiError(403)`. */
export async function postWikiIngest(text: string, sourceRef: string): Promise<WikiIngestResult> {
  const resp = await apiFetch("/api/admin/wiki/ingest", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, source_ref: sourceRef }),
  });
  return (await resp.json()) as WikiIngestResult;
}
