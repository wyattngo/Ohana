/**
 * Timestamp format cho sidebar row `updated_at` (F1 §Judgment call 3).
 *
 * Quy tắc:
 * - < 60 giây     → "vừa xong"
 * - < 60 phút     → "N phút trước"
 * - < 24 giờ      → "N giờ trước"
 * - ≥ 24 giờ      → "dd/MM" (nếu cùng năm) hoặc "dd/MM/yy"
 *
 * Không dùng Intl.RelativeTimeFormat: dịch VN cứng "trước" đọc quen hơn "1 giờ trước
 * đây", và không đáng thêm dep. Không dùng `date-fns` vì cùng lý do — file 15 dòng thay
 * cho ~30KB bundle.
 *
 * Input: ISO string từ BE (`created_at`/`updated_at`). Timezone: BE trả UTC (Postgres
 * `TIMESTAMPTZ`), `new Date(iso)` parse đúng.
 */

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function formatRelativeVN(iso: string, now: Date = new Date()): string {
  const then = new Date(iso);
  const diff = now.getTime() - then.getTime();
  if (diff < MINUTE) return "vừa xong";
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)} phút trước`;
  if (diff < DAY) return `${Math.floor(diff / HOUR)} giờ trước`;

  const dd = String(then.getDate()).padStart(2, "0");
  const mm = String(then.getMonth() + 1).padStart(2, "0");
  if (then.getFullYear() === now.getFullYear()) return `${dd}/${mm}`;
  return `${dd}/${mm}/${String(then.getFullYear()).slice(-2)}`;
}
