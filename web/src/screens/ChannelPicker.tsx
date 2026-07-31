import { useState } from "react";
import { ChevronRight, Lock, Loader2, MessageCircle, ShieldCheck } from "lucide-react";
import { ApiError, mockAuthorize } from "../lib/api";
import { Logo } from "../components/Logo";
import "./ChannelPicker.css";

/**
 * B.1 — Channel picker + authorize (spec 04 §3 B.1). UX shape ported from
 * `~/Downloads/seller_ai_copilot_demo.jsx` lines 299-354 (layout/copy reference only, not
 * copied code — see spec 04 §1 blockers B1-B4, all fixed here: no client-side LLM call, no
 * hardcoded shop, mock-authorize instead of the mockup's instant "connected" state).
 *
 * GD0.5 = Zalo-only (DEC-OHANA-01 U1/U4). Facebook + TikTok render disabled with a "Sắp có"
 * (coming soon) pill — listed per spec so the seller sees the roadmap, not clickable.
 */

interface Channel {
  id: "zalo" | "facebook" | "tiktok";
  label: string;
  available: boolean;
}

const CHANNELS: Channel[] = [
  { id: "zalo", label: "Zalo OA", available: true },
  { id: "facebook", label: "Facebook Messenger", available: false },
  { id: "tiktok", label: "TikTok Shop", available: false },
];

type View =
  | { name: "pick" }
  | { name: "authorize"; channel: Channel }
  | { name: "connecting"; channel: Channel };

interface ChannelPickerProps {
  onConnected: () => void;
  onError: (message: string) => void;
}

export function ChannelPicker({ onConnected, onError }: ChannelPickerProps) {
  const [view, setView] = useState<View>({ name: "pick" });

  async function handleAuthorize(channel: Channel): Promise<void> {
    setView({ name: "connecting", channel });
    try {
      await mockAuthorize("seller");
      onConnected();
    } catch (err) {
      onError(
        err instanceof ApiError
          ? `Kết nối thất bại (mã ${err.status}) — vui lòng thử lại.`
          : "Kết nối thất bại — vui lòng thử lại.",
      );
      setView({ name: "authorize", channel });
    }
  }

  if (view.name === "pick") {
    return (
      <main className="screen channel-picker">
        <header className="screen-header">
          {/* Lockup chỉ đặt ở màn hình đầu tiên — đây là chỗ duy nhất seller thấy sản phẩm
              trước khi biết nó là gì. Các màn sau đã có ngữ cảnh, nhét logo vào chỉ tốn
              chiều cao trên khung 430px. */}
          <Logo size={36} />
          <h1>Kết nối kênh bán hàng</h1>
          <p>Chọn kênh để Ohana AI bắt đầu soạn sẵn phản hồi cho bạn duyệt.</p>
        </header>
        <ul className="channel-list">
          {CHANNELS.map((channel) => (
            <li key={channel.id}>
              <button
                type="button"
                className="channel-row"
                disabled={!channel.available}
                onClick={() => setView({ name: "authorize", channel })}
              >
                <MessageCircle size={24} aria-hidden="true" />
                <span className="channel-row-label">{channel.label}</span>
                {channel.available ? (
                  <ChevronRight size={20} aria-hidden="true" />
                ) : (
                  <span className="pill pill-muted">Sắp có</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      </main>
    );
  }

  const { channel } = view;
  const connecting = view.name === "connecting";

  return (
    <main className="screen channel-authorize">
      <button
        type="button"
        className="back-link"
        disabled={connecting}
        onClick={() => setView({ name: "pick" })}
      >
        Quay lại
      </button>
      <ShieldCheck size={40} aria-hidden="true" />
      <h1>Cho phép Ohana AI đọc tin nhắn {channel.label}</h1>
      <ul className="permission-list">
        <li>
          <Lock size={18} aria-hidden="true" />
          <span>Ohana AI đọc tin nhắn khách gửi đến kênh này để soạn sẵn phản hồi.</span>
        </li>
        <li>
          <ShieldCheck size={18} aria-hidden="true" />
          <span>Bạn luôn duyệt trước khi gửi — Ohana AI không tự ý gửi tin cho khách.</span>
        </li>
      </ul>
      <button
        type="button"
        className="btn-primary"
        disabled={connecting}
        onClick={() => {
          void handleAuthorize(channel);
        }}
      >
        {connecting ? <Loader2 className="spin" size={18} aria-hidden="true" /> : null}
        {connecting ? "Đang kết nối…" : "Cho phép & kết nối"}
      </button>
    </main>
  );
}
