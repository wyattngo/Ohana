import { type FormEvent, useState } from "react";
import { Loader2, UploadCloud } from "lucide-react";
import { ApiError, postWikiIngest } from "../lib/api";

/**
 * Sub-task C — Admin wiki ingest UI (spec 04 §3, §7 Phase P2). Internal/admin-only screen:
 * raw text + `source_ref` → `POST /api/admin/wiki/ingest`, guarded server-side by
 * `auth.identity.require_admin`. There is no `shop_id` field here at all — the backend
 * hardcodes `PLATFORM_SHOP_ID` for this route (`parsing/ingest.py`); this form has no shop
 * concept to send.
 *
 * Reuses the shared `.screen` / `.btn-primary` / toast primitives from `App.tsx`/`App.css`
 * (same "parent owns toast, screen owns busy state" split as `ReviewCard.tsx`) rather than a
 * dedicated stylesheet — P2's ALLOWED_FILES lists only this `.tsx` under `web/src/screens/`,
 * not a new `AdminWikiIngest.css` (unlike P1's three screens, which each got one). See the
 * P2 ANCHOR report "Judgment calls".
 */

const MIN_TEXT_LENGTH = 100;

interface AdminWikiIngestProps {
  onBack: () => void;
  onIngested: (chunks: number) => void;
  onError: (message: string) => void;
}

type SubmitState = "idle" | "submitting";

export function AdminWikiIngest({ onBack, onIngested, onError }: AdminWikiIngestProps) {
  const [text, setText] = useState("");
  const [sourceRef, setSourceRef] = useState("");
  const [state, setState] = useState<SubmitState>("idle");

  const trimmedLength = text.trim().length;
  const tooShort = trimmedLength > 0 && trimmedLength < MIN_TEXT_LENGTH;
  const canSubmit = state === "idle" && trimmedLength >= MIN_TEXT_LENGTH && sourceRef.trim().length > 0;

  async function handleSubmit(e: FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    if (!canSubmit) return;

    setState("submitting");
    try {
      const result = await postWikiIngest(text.trim(), sourceRef.trim());
      onIngested(result.chunks);
      setText("");
      setSourceRef("");
    } catch (err) {
      onError(
        err instanceof ApiError && err.status === 403
          ? "Bạn không có quyền admin để nạp dữ liệu wiki."
          : "Nạp dữ liệu thất bại — vui lòng thử lại.",
      );
    } finally {
      setState("idle");
    }
  }

  return (
    <main className="screen admin-wiki-ingest">
      <button type="button" className="back-link" disabled={state === "submitting"} onClick={onBack}>
        Quay lại
      </button>

      <header className="screen-header">
        <h1>
          <UploadCloud size={22} aria-hidden="true" /> Nạp tài liệu Wiki
        </h1>
        <p>Nội dung sẽ được chia nhỏ và đưa vào kho tri thức dùng chung (platform wiki).</p>
      </header>

      <form
        className="admin-form"
        onSubmit={(e) => {
          void handleSubmit(e);
        }}
      >
        <label className="admin-field">
          <span>Nội dung</span>
          <textarea
            className="admin-textarea"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={10}
            placeholder="Dán nội dung chính sách, FAQ, hướng dẫn... (tối thiểu 100 ký tự)"
            disabled={state === "submitting"}
          />
          {tooShort && (
            <span className="admin-field-hint">
              Còn thiếu {MIN_TEXT_LENGTH - trimmedLength} ký tự (tối thiểu {MIN_TEXT_LENGTH}).
            </span>
          )}
        </label>

        <label className="admin-field">
          <span>Nguồn tham chiếu (source_ref)</span>
          <input
            className="admin-input"
            type="text"
            value={sourceRef}
            onChange={(e) => setSourceRef(e.target.value)}
            placeholder='vd: "policy-v1", "shipping-faq"'
            disabled={state === "submitting"}
          />
        </label>

        <button type="submit" className="btn-primary" disabled={!canSubmit}>
          {state === "submitting" && <Loader2 className="spin" size={18} aria-hidden="true" />}
          Ingest
        </button>
      </form>
    </main>
  );
}
