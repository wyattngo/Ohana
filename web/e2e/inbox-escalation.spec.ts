import { expect, test } from "@playwright/test";
import {
  connectAsSeller,
  ESCALATED_ROW,
  mockAuthorizeRoute,
  mockInboxRoute,
  PLAIN_ROW,
} from "./helpers";

/**
 * Render thật của A8 end-to-end — thứ mà gate source-contract (test_chat_ui.py) chỉ chứng
 * minh được gián tiếp: draft ESCALATE phải NHÌN KHÁC draft FAQ ở cả hai màn seller. Đây
 * chính là kịch bản `db/repos.py::list_pending` cảnh báo ("draft nhạy cảm render y hệt
 * draft FAQ") — giờ có browser thật xác nhận nó không xảy ra.
 */

test.beforeEach(async ({ page }) => {
  await mockAuthorizeRoute(page);
  await mockInboxRoute(page, [ESCALATED_ROW, PLAIN_ROW]);
});

test("inbox: draft escalate mang chip nhãn VN, draft FAQ sạch chip", async ({ page }) => {
  await connectAsSeller(page);

  const escalatedRow = page.locator(".reply-row", { hasText: "Khách Escalate" });
  await expect(escalatedRow.locator(".badge-escalation")).toHaveCount(2);
  // Nhãn VN từ ESCALATION_META — đúng thứ tự server trả (sensitive_intent trước cost_cap).
  await expect(escalatedRow.locator(".badge-escalation").nth(0)).toHaveText(/Nội dung nhạy cảm/);
  await expect(escalatedRow.locator(".badge-escalation").nth(1)).toHaveText(/Chạm hạn mức token/);

  const plainRow = page.locator(".reply-row", { hasText: "Khách FAQ" });
  await expect(plainRow).toBeVisible();
  await expect(plainRow.locator(".badge-escalation")).toHaveCount(0);
});

test("review card: mở draft escalate thấy đủ chip trước khi bấm Duyệt", async ({ page }) => {
  await connectAsSeller(page);

  await page.locator(".reply-row", { hasText: "Khách Escalate" }).click();

  const card = page.locator(".review-card");
  await expect(card.getByRole("button", { name: "Duyệt" })).toBeVisible();
  await expect(card.locator(".badge-escalation")).toHaveCount(2);
  await expect(card.locator(".badge-escalation").first()).toHaveText(/Nội dung nhạy cảm/);
});
