import type { Page } from "@playwright/test";

/** Row escalate — 2 reason có nhãn VN trong ESCALATION_META (intent.ts). */
export const ESCALATED_ROW = {
  reply_id: "pr_esc",
  conversation_id: "conv_1",
  customer_id: "Khách Escalate",
  draft_text: "Dạ shop xin lỗi anh về đơn hàng, bên em kiểm tra ngay ạ.",
  intent: "refund",
  confidence: 0.42,
  status: "pending",
  escalation_reasons: ["sensitive_intent", "cost_cap"],
};

/** Row FAQ thường — list rỗng, tuyệt đối không được có chip. */
export const PLAIN_ROW = {
  reply_id: "pr_faq",
  conversation_id: "conv_2",
  customer_id: "Khách FAQ",
  draft_text: "Dạ shop mở cửa 8h-22h hằng ngày ạ.",
  intent: "general",
  confidence: 0.91,
  status: "pending",
  escalation_reasons: [],
};

/** Mock `POST /api/mock/authorize` — trả fixture y hệt shape `api/mock_auth.py`. */
export async function mockAuthorizeRoute(page: Page): Promise<void> {
  await page.route("**/api/mock/authorize*", async (route) => {
    const role = new URL(route.request().url()).searchParams.get("role") ?? "seller";
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ oa_id: "fixture-oa-001", shop_id: "fixture-shop-001", role }),
    });
  });
}

/** Mock `GET /api/inbox` với danh sách rows cho trước. */
export async function mockInboxRoute(page: Page, rows: object[]): Promise<void> {
  await page.route("**/api/inbox", async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(rows),
    });
  });
}

/** Đi qua flow B.1 tới Inbox: picker → Zalo OA → Cho phép & kết nối. */
export async function connectAsSeller(page: Page): Promise<void> {
  await page.goto("/");
  await page.getByRole("button", { name: /Zalo OA/ }).click();
  await page.getByRole("button", { name: "Cho phép & kết nối" }).click();
}
