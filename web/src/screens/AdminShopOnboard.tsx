import { type FormEvent, useState } from "react";
import { Loader2, Store } from "lucide-react";
import { ApiError, postShopOnboard, type ShopOnboardResult } from "../lib/api";

/**
 * Admin shop onboard (đóng gap 2 của audit T6): UI cho `POST /api/admin/shops` — trước đây
 * endpoint tạo tenant chỉ gọi được bằng curl. Server-side `require_admin` là boundary thật
 * (seller cookie → 403 toast), cùng posture với `AdminWikiIngest`.
 *
 * Kết quả tạo shop hiển thị TRONG màn, không qua toast: `shop_id` do server sinh là thứ
 * admin phải copy lại (cấu hình webhook, seed dữ liệu...) — toast 4s biến mất là mất id,
 * phải vào DB tra. Form chỉ gửi `name`; không có ô nhập `shop_id` vì client không bao giờ
 * được chọn danh tính tenant (docstring `api/admin.py::onboard_shop`).
 *
 * Reuses the shared `.screen`/`.admin-*` primitives from `App.css` — same decision as
 * `AdminWikiIngest.tsx` (no dedicated stylesheet for an internal dev tool).
 */

const MAX_NAME_LENGTH = 200;

interface AdminShopOnboardProps {
  onBack: () => void;
  onError: (message: string) => void;
}

type SubmitState = "idle" | "submitting";

export function AdminShopOnboard({ onBack, onError }: AdminShopOnboardProps) {
  const [name, setName] = useState("");
  const [state, setState] = useState<SubmitState>("idle");
  const [created, setCreated] = useState<ShopOnboardResult | null>(null);

  const trimmed = name.trim();
  const canSubmit = state === "idle" && trimmed.length > 0 && trimmed.length <= MAX_NAME_LENGTH;

  async function handleSubmit(e: FormEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    if (!canSubmit) return;

    setState("submitting");
    try {
      const result = await postShopOnboard(trimmed);
      setCreated(result);
      setName("");
    } catch (err) {
      onError(
        err instanceof ApiError && err.status === 403
          ? "Bạn không có quyền admin để tạo shop."
          : "Tạo shop thất bại — vui lòng thử lại.",
      );
    } finally {
      setState("idle");
    }
  }

  return (
    <main className="screen admin-shop-onboard">
      <button type="button" className="back-link" disabled={state === "submitting"} onClick={onBack}>
        Quay lại
      </button>

      <header className="screen-header">
        <h1>
          <Store size={22} aria-hidden="true" /> Tạo shop mới
        </h1>
        <p>Tạo một tenant thật. Mã shop do hệ thống sinh — lưu lại sau khi tạo.</p>
      </header>

      <form
        className="admin-form"
        onSubmit={(e) => {
          void handleSubmit(e);
        }}
      >
        <label className="admin-field">
          <span>Tên shop</span>
          <input
            className="admin-input"
            type="text"
            value={name}
            onChange={(e) => setName(e.target.value)}
            maxLength={MAX_NAME_LENGTH}
            placeholder='vd: "Shop Áo Thun Sài Gòn"'
            disabled={state === "submitting"}
          />
        </label>

        <button type="submit" className="btn-primary" disabled={!canSubmit}>
          {state === "submitting" && <Loader2 className="spin" size={18} aria-hidden="true" />}
          Tạo shop
        </button>
      </form>

      {created && (
        <div className="onboard-result" role="status">
          <p>
            Đã tạo shop <strong>{created.name}</strong>. Lưu lại mã shop:
          </p>
          <code className="onboard-result-id">{created.shop_id}</code>
        </div>
      )}
    </main>
  );
}
