/**
 * Intent + status badge metadata shared by `Inbox.tsx` and `ReviewCard.tsx` (spec 04 §3 B.2).
 *
 * DEC-OHANA-01 §U2 froze 6 hue families + a single toast color + the CTA gradient — it did
 * NOT pull a semantic danger/warning/success set from Figma (the toast component itself is
 * one fixed color regardless of message type, confirming the source system doesn't define
 * one either). Spec 04 §3 B.2 asks for "red/yellow/green" intent badges, but inventing hex
 * values outside `tokens.ts` would violate the single-point-of-change contract. Differentiation
 * here is icon + Vietnamese label (works without color, and reads fine to color-blind sellers)
 * with a neutral `--ohana-color-greyscale-*` chip — see the P1 report for the flagged gap.
 */

import {
  Check,
  MessageCircle,
  TriangleAlert,
  Clock,
  CircleCheck,
  CircleX,
  type LucideIcon,
} from "lucide-react";

export interface IntentMeta {
  label: string;
  Icon: LucideIcon;
}

const INTENT_META: Record<string, IntentMeta> = {
  refund: { label: "Hoàn tiền", Icon: TriangleAlert },
  complaint: { label: "Khiếu nại", Icon: TriangleAlert },
  order_question: { label: "Hỏi đơn hàng", Icon: MessageCircle },
  general: { label: "Chung", Icon: Check },
};

const FALLBACK_INTENT_META: IntentMeta = { label: "Khác", Icon: MessageCircle };

export function intentMeta(intent: string): IntentMeta {
  return INTENT_META[intent] ?? { ...FALLBACK_INTENT_META, label: intent };
}

export interface EscalationMeta {
  label: string;
  Icon: LucideIcon;
}

/**
 * A8 `escalation_reasons` → Vietnamese chip labels. The seven keys mirror
 * `agent/policy_gate.py` SEVERITY_RANK verbatim (the DB CHECK `escalation_reasons_known`
 * admits exactly these). Same frozen-token decision as INTENT_META above: no danger hue
 * exists in `tokens.ts`, so the signal is outline + TriangleAlert + label, not color.
 * The server already sorts by severity — render in received order, never re-sort here.
 */
const ESCALATION_META: Record<string, EscalationMeta> = {
  sensitive_intent: { label: "Nội dung nhạy cảm", Icon: TriangleAlert },
  injection_attempt: { label: "Tin nhắn đáng ngờ", Icon: TriangleAlert },
  data_unavailable: { label: "Thiếu dữ liệu", Icon: TriangleAlert },
  media_content: { label: "Có ảnh/media", Icon: TriangleAlert },
  window_closed: { label: "Hết cửa sổ nhắn", Icon: TriangleAlert },
  window_unknown: { label: "Cửa sổ nhắn không rõ", Icon: TriangleAlert },
  cost_cap: { label: "Chạm hạn mức token", Icon: TriangleAlert },
};

export function escalationMeta(reason: string): EscalationMeta {
  // Unknown reason ⇒ raw key as label (same posture as intentMeta): the DB CHECK makes
  // this near-impossible, but hiding a reason the gate DID record would be worse than
  // showing an ugly key.
  return ESCALATION_META[reason] ?? { label: reason, Icon: TriangleAlert };
}

export interface StatusMeta {
  label: string;
  Icon: LucideIcon;
}

const STATUS_META: Record<string, StatusMeta> = {
  pending: { label: "Chờ duyệt", Icon: Clock },
  approved: { label: "Đã duyệt", Icon: CircleCheck },
  rejected: { label: "Đã từ chối", Icon: CircleX },
};

const FALLBACK_STATUS_META: StatusMeta = { label: "—", Icon: Clock };

export function statusMeta(status: string): StatusMeta {
  return STATUS_META[status] ?? { ...FALLBACK_STATUS_META, label: status };
}
