import { useCallback, useState } from "react";
import "./App.css";
import { AdminWikiIngest } from "./screens/AdminWikiIngest";
import { ChannelPicker } from "./screens/ChannelPicker";
import { ChatScreen } from "./screens/Chat";
import { InboxScreen } from "./screens/Inbox";
import { ReviewCard } from "./screens/ReviewCard";
import type { PendingReplyOut } from "./lib/api";

/**
 * GD0.5 shell. Screen switching is plain React state, NOT `react-router-dom` (not an
 * installed dependency, and neither P1's nor P2's ALLOWED_FILES cover `package.json`/lockfile
 * — see the P1 ANCHOR report "Judgment calls" for the routing tradeoff this decided against
 * Hash/BrowserRouter; P2 keeps the same call for the new `admin` screen).
 *
 * Admin reachability (P2 judgment call, see P2 ANCHOR report): GD0.5 has no real admin login
 * (`api/mock_auth.py` mints `?role=admin` via a query param, not a role-differentiated UI) and
 * P1 deliberately shipped no bottom nav for a single-screen-at-a-time flow. P2's ALLOWED_FILES
 * only covers `App.tsx`/`App.css`/`AdminWikiIngest.tsx` — NOT `Inbox.tsx` or the other P1
 * screens — so a nav entry inside any existing screen's header is out of scope. The minimal
 * in-scope option: a small persistent link in the shell chrome App.tsx already owns (same
 * place the toast lives), visible on every non-admin screen. It is NOT role-gated client-side
 * (there is nowhere in this app's state that knows the current session's role — `mockAuthorize`
 * returns it but nothing stores it) — the server-side `require_admin` 403 is the actual
 * boundary; a seller who clicks this link just sees the error toast this screen wires to
 * `onError`. Acceptable for a dev-fixture-only GD0.5 tool, not acceptable once spec 05 adds
 * real seller accounts.
 */

type Screen =
  | { name: "channel" }
  | { name: "inbox" }
  | { name: "review"; reply: PendingReplyOut }
  | { name: "chat" }
  | { name: "admin" };

interface ToastState {
  message: string;
  kind: "success" | "error";
}

const TOAST_DURATION_MS = 4000;

function App() {
  const [screen, setScreen] = useState<Screen>({ name: "channel" });
  const [toast, setToast] = useState<ToastState | null>(null);

  const showToast = useCallback((message: string, kind: ToastState["kind"]) => {
    setToast({ message, kind });
    window.setTimeout(() => {
      setToast((current) => (current?.message === message ? null : current));
    }, TOAST_DURATION_MS);
  }, []);

  const showError = useCallback((message: string) => showToast(message, "error"), [showToast]);

  return (
    <div className="ohana-app">
      {/* Chrome dùng chung cho các lối vào phụ. Trước G2 chỉ có một link admin định vị tuyệt
          đối; thêm link Chat cạnh nó bằng cách bọc cả hai vào một hàng — giữ nguyên vị trí
          góc trên phải, không phải nhét thêm một phần tử absolute thứ hai rồi canh tay. */}
      {screen.name !== "admin" && screen.name !== "chat" && (
        <div className="shell-chrome">
          <button type="button" className="chrome-link" onClick={() => setScreen({ name: "chat" })}>
            Hỏi AI
          </button>
          <button type="button" className="chrome-link" onClick={() => setScreen({ name: "admin" })}>
            Quản trị Wiki
          </button>
        </div>
      )}

      {screen.name === "channel" && (
        <ChannelPicker
          onConnected={() => setScreen({ name: "inbox" })}
          onError={showError}
        />
      )}

      {screen.name === "inbox" && (
        <InboxScreen
          onOpenReply={(reply) => setScreen({ name: "review", reply })}
          onError={showError}
        />
      )}

      {screen.name === "review" && (
        <ReviewCard
          reply={screen.reply}
          onBack={() => setScreen({ name: "inbox" })}
          onDecided={(status) => {
            showToast(
              status === "approved"
                ? "Đã duyệt. Tin nhắn CHƯA được gửi cho khách — worker gửi tự động chưa được triển khai."
                : "Đã từ chối. Draft sẽ không gửi khách.",
              "success",
            );
            setScreen({ name: "inbox" });
          }}
          onError={showError}
        />
      )}

      {screen.name === "chat" && (
        <ChatScreen onBack={() => setScreen({ name: "inbox" })} onError={showError} />
      )}

      {screen.name === "admin" && (
        <AdminWikiIngest
          onBack={() => setScreen({ name: "inbox" })}
          onIngested={(chunks) => {
            showToast(`Đã nạp ${chunks} chunk(s) vào wiki dùng chung.`, "success");
          }}
          onError={showError}
        />
      )}

      {toast && (
        <div className={`toast toast-${toast.kind}`} role="status">
          {toast.message}
        </div>
      )}
    </div>
  );
}

export default App;
