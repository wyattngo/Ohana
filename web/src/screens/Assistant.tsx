import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  Info,
  Loader2,
  MessageCircle,
  Menu,
  Plus,
  Send,
  Sparkles,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import {
  ApiError,
  type ConversationRow,
  deleteConversation,
  fetchMessages,
  listConversations,
  streamAssistantChat,
} from "../lib/api";
import { formatRelativeVN } from "../lib/time";
import "./Assistant.css";

/**
 * F1 · Tầng 2 Assistant — sidebar list conversations + main chat.
 *
 * Kiến trúc data flow (brief §Design layer):
 *   mount        → GET /assistant/conversations?limit=20
 *   click row    → GET /assistant/conversations/{id}/messages?limit=50
 *   send         → POST /assistant/chat với conversation_id (null ⇒ auto-create BE-side)
 *                  → append turns + refetch sidebar (bring row lên đầu, adopt auto-title)
 *   delete row   → window.confirm → DELETE → remove khỏi list; nếu active ⇒ clear main
 *
 * KHÔNG chạm `Chat.tsx` (Tầng 3 seller-facing). KHÔNG router URL — refresh mất state, quay
 * về `channel` (parity với hành vi hiện tại của app, brief §Ngoài scope).
 *
 * Optimistic UX:
 * - Turn user append trước khi await (parity Chat.tsx cold-start ~25s).
 * - Sidebar bump lên đầu SAU khi response OK (không optimistic sort trước — refetch server-
 *   order là ground truth, chi phí 1 GET không đáng bug re-order khi 2 chat race).
 *
 * Mobile (<768px) sidebar là drawer trượt trái mặc định đóng; desktop (≥768px) là pane cố
 * định 280px — CSS xử lý qua media query, JS không detect viewport.
 */

interface AssistantScreenProps {
  onBack: () => void;
  onError: (message: string) => void;
}

interface Turn {
  role: "user" | "assistant";
  text: string;
}

const MAX_MESSAGE_LENGTH = 4000; // khớp `AssistantChatIn.message` Field max_length

export function AssistantScreen({ onBack, onError }: AssistantScreenProps) {
  const [conversations, setConversations] = useState<ConversationRow[] | null>(null);
  const [activeConvId, setActiveConvId] = useState<number | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [turnsLoading, setTurnsLoading] = useState(false);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const transcriptRef = useRef<HTMLDivElement>(null);

  // ── Load list (mount + sau khi send để bump active lên đầu / adopt auto-title) ────
  const loadList = useCallback(async () => {
    try {
      const result = await listConversations();
      setConversations(result.items);
      setListError(null);
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 401
          ? "Phiên đăng nhập hết hạn — vui lòng kết nối lại."
          : "Không tải được danh sách cuộc hội thoại.";
      setListError(message);
      onError(message);
    }
  }, [onError]);

  useEffect(() => {
    void loadList();
  }, [loadList]);

  // ── Load messages khi activeConvId đổi ─────────────────────────────────────────────
  useEffect(() => {
    if (activeConvId === null) {
      setTurns([]);
      return;
    }
    let cancelled = false;
    setTurnsLoading(true);
    setTurns([]);
    (async () => {
      try {
        const rows = await fetchMessages(activeConvId);
        if (cancelled) return;
        setTurns(
          rows
            .filter((r) => r.role === "user" || r.role === "assistant")
            .map((r) => ({ role: r.role as "user" | "assistant", text: r.content })),
        );
      } catch (err) {
        if (cancelled) return;
        const message =
          err instanceof ApiError && err.status === 404
            ? "Cuộc hội thoại không tồn tại."
            : "Không tải được nội dung cuộc hội thoại.";
        onError(message);
        setActiveConvId(null);
      } finally {
        if (!cancelled) setTurnsLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [activeConvId, onError]);

  // ── Cuộn transcript xuống cuối khi có lượt mới hoặc đang chờ ──────────────────────
  useEffect(() => {
    transcriptRef.current?.scrollTo({ top: transcriptRef.current.scrollHeight });
  }, [turns, sending]);

  const trimmed = draft.trim();
  const canSend = !sending && trimmed.length > 0 && trimmed.length <= MAX_MESSAGE_LENGTH;

  async function handleSubmit(e: FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    if (!canSend) return;

    const question = trimmed;
    const wasNew = activeConvId === null;
    // Optimistic: append user turn + placeholder assistant turn RỖNG. Token frame
    // append vào text của placeholder này — user thấy "typing" thật.
    setTurns((prev) => [
      ...prev,
      { role: "user", text: question },
      { role: "assistant", text: "" },
    ]);
    setDraft("");
    setSending(true);

    let streamError: { code: string; message: string } | null = null;
    let receivedAnyToken = false;
    let doneMeta: { conversation_id: number } | null = null;

    try {
      await streamAssistantChat(question, activeConvId, {
        onToken: (text) => {
          receivedAnyToken = true;
          // Append vào placeholder assistant turn (turn cuối list). functional setState
          // — không giữ ref stale khi callback fire nhanh.
          setTurns((prev) => {
            const copy = prev.slice();
            const last = copy[copy.length - 1];
            if (last && last.role === "assistant") {
              copy[copy.length - 1] = { ...last, text: last.text + text };
            }
            return copy;
          });
        },
        onDone: (meta) => {
          doneMeta = meta;
        },
        onError: (frame) => {
          streamError = frame;
        },
      });

      if (streamError) {
        // Rollback: xoá placeholder + user turn, trả text vào input.
        setDraft(question);
        setTurns((prev) => prev.slice(0, -2));
        const code = (streamError as { code: string; message: string }).code;
        if (code === "llm_empty_response") {
          onError("Model không trả về nội dung. Thử lại giúp em nhé.");
        } else {
          onError("Lỗi phía model — vui lòng thử lại.");
        }
      } else if (doneMeta) {
        // BE gán/tạo conversation_id — nếu tạo mới, set active + refetch list adopt title.
        setActiveConvId((doneMeta as { conversation_id: number }).conversation_id);
        void loadList();
        if (wasNew) setDrawerOpen(false);
      } else if (!receivedAnyToken) {
        // Stream đóng KHÔNG có done + KHÔNG có token — bất thường (network drop giữa
        // handshake). Rollback như error path.
        setDraft(question);
        setTurns((prev) => prev.slice(0, -2));
        onError("Kết nối mất giữa chừng — vui lòng thử lại.");
      }
    } catch (err) {
      // HTTP 4xx/5xx TRƯỚC khi mở stream (rate limit, auth expired, conv 404).
      setDraft(question);
      setTurns((prev) => prev.slice(0, -2));
      if (err instanceof ApiError && err.status === 401) {
        onError("Phiên đăng nhập đã hết hạn — vui lòng đăng nhập lại.");
      } else if (err instanceof ApiError && err.status === 429) {
        onError("Đã đạt giới hạn hôm nay. Nâng cấp gói để tiếp tục.");
      } else if (err instanceof ApiError && err.status === 404) {
        onError("Cuộc hội thoại đã bị xoá — hãy bắt đầu cuộc mới.");
        setActiveConvId(null);
      } else {
        onError("Không gửi được câu hỏi — vui lòng thử lại.");
      }
    } finally {
      setSending(false);
    }
  }

  function handleNewConversation(): void {
    setActiveConvId(null);
    setTurns([]);
    setDraft("");
    setDrawerOpen(false);
  }

  async function handleDelete(id: number, title: string | null): Promise<void> {
    const label = title ?? "cuộc chưa đặt tên";
    if (!window.confirm(`Xoá "${label}"? Không hoàn tác được.`)) return;
    try {
      await deleteConversation(id);
      setConversations((prev) => (prev ?? []).filter((c) => c.conversation_id !== id));
      if (activeConvId === id) {
        setActiveConvId(null);
        setTurns([]);
      }
    } catch (err) {
      onError(
        err instanceof ApiError && err.status === 404
          ? "Cuộc hội thoại đã bị xoá trước đó."
          : "Không xoá được — vui lòng thử lại.",
      );
    }
  }

  const list = conversations ?? [];

  return (
    <main className="screen assistant-screen">
      {drawerOpen && (
        <button
          type="button"
          className="assistant-drawer-backdrop"
          aria-label="Đóng danh sách"
          onClick={() => setDrawerOpen(false)}
        />
      )}

      <aside className={`assistant-sidebar ${drawerOpen ? "assistant-sidebar-open" : ""}`}>
        <div className="assistant-sidebar-header">
          <button
            type="button"
            className="assistant-new-conv"
            onClick={handleNewConversation}
          >
            <Plus size={16} aria-hidden="true" />
            Cuộc mới
          </button>
        </div>
        <div className="assistant-conv-list" role="list">
          {conversations === null && listError === null && (
            <div className="assistant-loading">
              <Loader2 className="spin" size={20} aria-hidden="true" />
              <span>Đang tải…</span>
            </div>
          )}
          {conversations !== null && list.length === 0 && (
            <p className="assistant-conv-empty">
              Chưa có cuộc nào. Gõ câu hỏi để bắt đầu.
            </p>
          )}
          {listError && (
            <div className="assistant-list-error">
              <TriangleAlert size={16} aria-hidden="true" />
              <span>{listError}</span>
              <button
                type="button"
                className="assistant-retry-btn"
                onClick={() => void loadList()}
              >
                Thử lại
              </button>
            </div>
          )}
          {list.map((conv) => (
            <div
              key={conv.conversation_id}
              className={`assistant-conv-row ${
                activeConvId === conv.conversation_id ? "assistant-conv-active" : ""
              }`}
              role="listitem"
            >
              <button
                type="button"
                className="assistant-conv-open"
                onClick={() => {
                  setActiveConvId(conv.conversation_id);
                  setDrawerOpen(false);
                }}
              >
                <span className="assistant-conv-title">{conv.title ?? "Untitled"}</span>
                <span className="assistant-conv-time">
                  {formatRelativeVN(conv.updated_at)}
                </span>
              </button>
              <button
                type="button"
                className="assistant-conv-delete"
                aria-label={`Xoá cuộc ${conv.title ?? "chưa đặt tên"}`}
                onClick={() => void handleDelete(conv.conversation_id, conv.title)}
              >
                <Trash2 size={14} aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      </aside>

      <section className="assistant-main">
        <header className="assistant-main-header">
          <button
            type="button"
            className="assistant-drawer-toggle"
            aria-label="Mở danh sách"
            onClick={() => setDrawerOpen(true)}
          >
            <Menu size={20} aria-hidden="true" />
          </button>
          <h1>
            <MessageCircle size={20} aria-hidden="true" /> Trợ lý AI
          </h1>
          <button
            type="button"
            className="back-link back-link-compact"
            disabled={sending}
            onClick={onBack}
          >
            Đóng
          </button>
        </header>

        <p className="chat-disclaimer">
          <Info size={16} aria-hidden="true" />
          <span>
            Trợ lý cá nhân — chat có nhớ hội thoại. Nội dung chỉ dùng nội bộ, không gửi cho khách.
          </span>
        </p>

        <div className="chat-transcript" ref={transcriptRef}>
          {activeConvId === null && turns.length === 0 && !sending && (
            <div className="assistant-empty">
              <Sparkles size={28} aria-hidden="true" />
              <p>Chào — trợ lý cá nhân của bạn.</p>
              <p className="assistant-empty-hint">
                Hỏi bất cứ điều gì. Cuộc hội thoại sẽ tự lưu.
              </p>
            </div>
          )}

          {turnsLoading && (
            <div className="assistant-loading">
              <Loader2 className="spin" size={20} aria-hidden="true" />
              <span>Đang tải nội dung…</span>
            </div>
          )}

          {turns.map((turn, i) => {
            const isStreamingPlaceholder =
              sending &&
              i === turns.length - 1 &&
              turn.role === "assistant" &&
              turn.text === "";
            if (isStreamingPlaceholder) {
              // First-token chưa đến — cold Together cold-start ~20s. Hiển thị pending
              // spinner thay bong bóng rỗng (bong bóng rỗng trông như bug UI).
              return (
                <div
                  key={i}
                  className="chat-turn chat-turn-assistant chat-turn-pending"
                  role="status"
                >
                  <Loader2 className="spin" size={16} aria-hidden="true" />
                  <span>Đang soạn câu trả lời… lần đầu có thể mất 20–30 giây.</span>
                </div>
              );
            }
            return (
              <div key={i} className={`chat-turn chat-turn-${turn.role}`}>
                {turn.text}
                {sending && i === turns.length - 1 && turn.role === "assistant" && (
                  <span className="chat-turn-cursor" aria-hidden="true">
                    ▍
                  </span>
                )}
              </div>
            );
          })}
        </div>

        <form
          className="chat-composer"
          onSubmit={(e) => {
            void handleSubmit(e);
          }}
        >
          <textarea
            className="chat-input"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={2}
            maxLength={MAX_MESSAGE_LENGTH}
            placeholder="Nhập câu hỏi…"
            disabled={sending}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                e.currentTarget.form?.requestSubmit();
              }
            }}
          />
          <button type="submit" className="btn-primary chat-send" disabled={!canSend}>
            {sending ? (
              <Loader2 className="spin" size={18} aria-hidden="true" />
            ) : (
              <Send size={18} aria-hidden="true" />
            )}
            Gửi
          </button>
        </form>
      </section>
    </main>
  );
}
