import { useCallback, useEffect, useState } from "react";
import { ChevronRight, Inbox as InboxIcon, Loader2, TriangleAlert, UserRound } from "lucide-react";
import { ApiError, fetchInbox, type PendingReplyOut } from "../lib/api";
import { intentMeta, statusMeta } from "../lib/intent";
import "./Inbox.css";

/**
 * B.2 — Inbox (spec 04 §3 B.2). Renders `PendingReplyOut[]` from `GET /api/inbox` — NOT the
 * mockup's `INIT_CONVS` mock conversations (semantics changed per spec). Polls every 10s
 * (constants.POLL_INTERVAL_MS) — no SSE, per spec §12 "chưa xác nhận".
 */

const POLL_INTERVAL_MS = 10_000;

interface InboxScreenProps {
  /** Passes the full row, not just `reply_id` — there is no single-fetch
   * `GET /api/inbox/{id}` endpoint (spec 04 U5 cut), so `ReviewCard` renders off the row this
   * list already fetched rather than triggering a second round-trip. */
  onOpenReply: (reply: PendingReplyOut) => void;
  onError: (message: string) => void;
}

export function InboxScreen({ onOpenReply, onError }: InboxScreenProps) {
  const [rows, setRows] = useState<PendingReplyOut[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await fetchInbox();
      setRows(data);
      setLoadError(null);
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 401
          ? "Phiên đăng nhập hết hạn — vui lòng kết nối lại."
          : "Không tải được danh sách — vui lòng thử lại.";
      setLoadError(message);
      onError(message);
    }
  }, [onError]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => {
      void load();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  if (rows === null && loadError === null) {
    return (
      <main className="screen inbox-screen inbox-loading">
        <Loader2 className="spin" size={28} aria-hidden="true" />
        <p>Đang tải hộp thư…</p>
      </main>
    );
  }

  if (rows === null && loadError !== null) {
    return (
      <main className="screen inbox-screen inbox-error">
        <TriangleAlert size={28} aria-hidden="true" />
        <p>{loadError}</p>
        <button type="button" className="btn-secondary" onClick={() => void load()}>
          Thử lại
        </button>
      </main>
    );
  }

  const list = rows ?? [];

  return (
    <main className="screen inbox-screen">
      <header className="screen-header">
        <h1>
          <InboxIcon size={22} aria-hidden="true" /> Hộp thư chờ duyệt
        </h1>
      </header>
      {list.length === 0 ? (
        <p className="inbox-empty">Chưa có tin nhắn cần duyệt</p>
      ) : (
        <ul className="reply-list">
          {list.map((row) => {
            const { label: intentLabel, Icon: IntentIcon } = intentMeta(row.intent);
            const { label: statusLabel } = statusMeta(row.status);
            const confidencePct = Math.round(row.confidence * 100);
            return (
              <li key={row.reply_id}>
                <button
                  type="button"
                  className="reply-row"
                  onClick={() => onOpenReply(row)}
                >
                  <UserRound size={32} aria-hidden="true" className="reply-row-avatar" />
                  <div className="reply-row-body">
                    <div className="reply-row-top">
                      <span className="reply-row-customer">{row.customer_id}</span>
                      <span className="badge">
                        <IntentIcon size={14} aria-hidden="true" />
                        {intentLabel}
                      </span>
                    </div>
                    <p className="reply-row-preview">{row.draft_text}</p>
                    <div className="reply-row-bottom">
                      <div
                        className="confidence-bar"
                        role="meter"
                        aria-label={`Độ tin cậy ${confidencePct}%`}
                        aria-valuenow={confidencePct}
                        aria-valuemin={0}
                        aria-valuemax={100}
                      >
                        <div className="confidence-bar-fill" style={{ width: `${confidencePct}%` }} />
                      </div>
                      <span className="badge badge-status">{statusLabel}</span>
                    </div>
                  </div>
                  <ChevronRight size={20} aria-hidden="true" className="reply-row-chevron" />
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </main>
  );
}
