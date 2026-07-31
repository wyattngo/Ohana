import { useState } from "react";
import { Check, ChevronLeft, Loader2, X } from "lucide-react";
import { ApiError, approveReply, rejectReply, type PendingReplyOut } from "../lib/api";
import { intentMeta, statusMeta } from "../lib/intent";
import "./ReviewCard.css";

/**
 * B.3 — Review card (spec 04 §3 B.3). `draft_text` is READ-ONLY (no textarea, no
 * free-typing, no AI re-compose — that is the entire HITL point, mockup blocker B3). Two
 * actions only: Duyệt (approve) / Từ chối (reject, native confirm dialog first).
 *
 * No conversation history rendered (DEC-OHANA-01 U5 cut) — only what `PendingReplyOut`
 * carries: customer_id, intent, confidence, draft_text, status.
 */

interface ReviewCardProps {
  reply: PendingReplyOut;
  onBack: () => void;
  onDecided: (status: "approved" | "rejected") => void;
  onError: (message: string) => void;
}

type Busy = "idle" | "approving" | "rejecting";

export function ReviewCard({ reply, onBack, onDecided, onError }: ReviewCardProps) {
  const [busy, setBusy] = useState<Busy>("idle");
  const { label: intentLabel, Icon: IntentIcon } = intentMeta(reply.intent);
  const { label: statusLabel } = statusMeta(reply.status);
  const confidencePct = Math.round(reply.confidence * 100);
  const disabled = busy !== "idle";

  async function handleApprove(): Promise<void> {
    setBusy("approving");
    try {
      await approveReply(reply.reply_id);
      onDecided("approved");
    } catch (err) {
      onError(
        err instanceof ApiError
          ? `Duyệt thất bại (mã ${err.status}) — vui lòng thử lại.`
          : "Duyệt thất bại — vui lòng thử lại.",
      );
      setBusy("idle");
    }
  }

  async function handleReject(): Promise<void> {
    // Native confirm — spec 04 §3 B.3 asks only for "a confirmation dialog", no branded modal
    // in scope for P1 (flagged in the ANCHOR report as a judgment call, not silently picked).
    const confirmed = window.confirm("Bạn muốn từ chối? Draft sẽ không gửi khách.");
    if (!confirmed) return;

    setBusy("rejecting");
    try {
      await rejectReply(reply.reply_id);
      onDecided("rejected");
    } catch (err) {
      onError(
        err instanceof ApiError
          ? `Từ chối thất bại (mã ${err.status}) — vui lòng thử lại.`
          : "Từ chối thất bại — vui lòng thử lại.",
      );
      setBusy("idle");
    }
  }

  return (
    <main className="screen review-card">
      <button type="button" className="back-link" disabled={disabled} onClick={onBack}>
        <ChevronLeft size={18} aria-hidden="true" /> Hộp thư
      </button>

      <header className="review-card-header">
        <span className="review-card-customer">{reply.customer_id}</span>
        <span className="badge">
          <IntentIcon size={14} aria-hidden="true" />
          {intentLabel}
        </span>
      </header>

      <div className="review-card-meta">
        <span>Độ tin cậy {confidencePct}%</span>
        <span className="badge badge-status">{statusLabel}</span>
      </div>

      <p className="review-card-draft">{reply.draft_text}</p>

      <div className="review-card-actions">
        <button
          type="button"
          className="btn-primary"
          disabled={disabled}
          onClick={() => {
            void handleApprove();
          }}
        >
          {busy === "approving" ? (
            <Loader2 className="spin" size={18} aria-hidden="true" />
          ) : (
            <Check size={18} aria-hidden="true" />
          )}
          Duyệt
        </button>
        <button
          type="button"
          className="btn-ghost-danger"
          disabled={disabled}
          onClick={() => {
            void handleReject();
          }}
        >
          {busy === "rejecting" ? (
            <Loader2 className="spin" size={18} aria-hidden="true" />
          ) : (
            <X size={18} aria-hidden="true" />
          )}
          Từ chối
        </button>
      </div>
    </main>
  );
}
