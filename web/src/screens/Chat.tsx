import { type FormEvent, useEffect, useRef, useState } from "react";
import { Info, Loader2, MessageCircle, Send } from "lucide-react";
import { ApiError, postChat } from "../lib/api";
import "./Chat.css";

/**
 * Sub-task C — General Chat (spec 07 §3, §7 Phase G2). Seller ↔ AI tổng quát, **nội bộ**:
 * nothing typed or produced here can reach a customer. That isolation is enforced structurally
 * on the backend (`api/chat.py` may not import the sender / `PendingReply` / `policy_gate`,
 * gated by an import-graph test), not by discipline in this file.
 *
 * The disclaimer below is not boilerplate. Measured during G1 against the real model: asked
 * about shipping for a shop with no shipping configured, models still volunteered "2-3 ngày".
 * A seller who reads an ungrounded answer as authoritative and pastes it to a customer turns
 * an invented delivery promise into a real one. So the warning is persistent chrome, not a
 * tooltip — `tests/test_chat_ui.py` fails if it is ever moved into `title`/`aria-label`.
 *
 * Reuses the shared `.screen` / `.btn-primary` / toast primitives from `App.tsx`/`App.css`
 * (parent owns toast, screen owns busy state — same split as `ReviewCard.tsx`), plus a
 * dedicated `Chat.css` for the transcript layout, which has no analogue in the P1 screens.
 */

interface ChatScreenProps {
  onBack: () => void;
  onError: (message: string) => void;
}

interface Turn {
  role: "user" | "assistant";
  text: string;
}

const MAX_MESSAGE_LENGTH = 4000; // khớp `ChatIn.message` (Field max_length=4000)

export function ChatScreen({ onBack, onError }: ChatScreenProps) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const transcriptRef = useRef<HTMLDivElement>(null);

  // Cuộn xuống cuối khi có lượt mới — kể cả lúc đang chờ, vì "đang soạn..." cũng là một dòng
  // mới cần thấy được (chờ tới 25s mà chỉ báo nằm ngoài khung nhìn thì coi như không có).
  useEffect(() => {
    transcriptRef.current?.scrollTo({ top: transcriptRef.current.scrollHeight });
  }, [turns, sending]);

  const trimmed = draft.trim();
  const canSend = !sending && trimmed.length > 0 && trimmed.length <= MAX_MESSAGE_LENGTH;

  async function handleSubmit(e: FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    if (!canSend) return;

    const question = trimmed;
    // Hiện lượt của seller NGAY rồi mới gọi mạng: request đầu tiên có thể mất ~25 giây (cold
    // start, đo ở G1). Nếu đợi response mới vẽ, seller gõ xong thấy ô input trống trơn và
    // không có gì chứng tỏ tin đã được nhận.
    setTurns((prev) => [...prev, { role: "user", text: question }]);
    setDraft("");
    setSending(true);
    try {
      const result = await postChat(question);
      setTurns((prev) => [...prev, { role: "assistant", text: result.reply }]);
    } catch (err) {
      // Trả lại câu vừa gõ vào ô nhập — bắt seller gõ lại một câu dài chỉ vì mạng lỗi là mất
      // công vô ích, và họ sẽ gõ lại ngắn hơn/khác đi rồi đổ cho AI.
      setDraft(question);
      setTurns((prev) => prev.slice(0, -1));
      if (err instanceof ApiError && err.status === 401) {
        onError("Phiên đăng nhập đã hết hạn — vui lòng đăng nhập lại.");
      } else if (err instanceof ApiError && err.status === 502) {
        onError("Model không trả về nội dung. Thử lại giúp em nhé.");
      } else {
        onError("Không gửi được câu hỏi — vui lòng thử lại.");
      }
    } finally {
      setSending(false);
    }
  }

  return (
    <main className="screen chat-screen">
      <button type="button" className="back-link" disabled={sending} onClick={onBack}>
        Quay lại
      </button>

      <header className="screen-header">
        <h1>
          <MessageCircle size={22} aria-hidden="true" /> Hỏi AI
        </h1>
      </header>

      {/* Chrome thường trực, không phải tooltip — xem docstring đầu file. */}
      <p className="chat-disclaimer">
        <Info size={16} aria-hidden="true" />
        <span>
          AI tổng quát — <strong>chưa kết nối dữ liệu shop</strong> (đơn hàng, tồn kho, giá).
          Câu trả lời chỉ để tham khảo; hãy tự kiểm tra trước khi trả lời khách.
        </span>
      </p>

      <div className="chat-transcript" ref={transcriptRef}>
        {turns.length === 0 && !sending && (
          <p className="chat-empty">
            Hỏi bất cứ điều gì về bán hàng — cách trả lời khách, viết mô tả sản phẩm, xử lý
            tình huống khó.
          </p>
        )}

        {turns.map((turn, i) => (
          <div key={i} className={`chat-turn chat-turn-${turn.role}`}>
            {turn.text}
          </div>
        ))}

        {sending && (
          <div className="chat-turn chat-turn-assistant chat-turn-pending" role="status">
            <Loader2 className="spin" size={16} aria-hidden="true" />
            {/* Nói rõ "có thể mất một chút" — đo được 24.8s ở lần gọi đầu (§14). Im lặng
                trong 25 giây đọc như treo máy. */}
            <span>Đang soạn câu trả lời… lần đầu có thể mất khoảng 20–30 giây.</span>
          </div>
        )}
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
            // Enter gửi, Shift+Enter xuống dòng — quy ước quen thuộc của mọi app nhắn tin.
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
    </main>
  );
}
