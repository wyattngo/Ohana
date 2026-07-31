import { expect, test } from "@playwright/test";
import { mockAuthorizeRoute } from "./helpers";

/**
 * Render thật cho gap 2+3 (audit T6): đường mint admin từ picker phải dẫn tới màn tạo shop
 * hoạt động, và `shop_id` server sinh phải hiển thị BỀN trong màn (không phải toast 4s) —
 * đó là quyết định UX chính của AdminShopOnboard, chỉ browser test chứng minh được.
 */

test.beforeEach(async ({ page }) => {
  await mockAuthorizeRoute(page);
});

test("picker → quyền quản trị → tạo shop: shop_id server sinh hiện bền trong màn", async ({
  page,
}) => {
  await page.route("**/api/admin/shops", async (route) => {
    const body = route.request().postDataJSON() as { name: string };
    await route.fulfill({
      status: 201,
      contentType: "application/json",
      body: JSON.stringify({ shop_id: "shop_e2e0123456789ab", name: body.name }),
    });
  });

  await page.goto("/");
  await page.getByRole("button", { name: /Vào với quyền quản trị/ }).click();

  await expect(page.getByRole("heading", { name: "Tạo shop mới" })).toBeVisible();
  await page.getByLabel("Tên shop").fill("Shop Áo Thun E2E");
  await page.getByRole("button", { name: "Tạo shop" }).click();

  const result = page.locator(".onboard-result");
  await expect(result).toContainText("Shop Áo Thun E2E");
  await expect(result.locator(".onboard-result-id")).toHaveText("shop_e2e0123456789ab");
});

test("seller lạc vào màn tạo shop: 403 ra toast đúng lời, không silent", async ({ page }) => {
  await page.route("**/api/admin/shops", async (route) => {
    await route.fulfill({
      status: 403,
      contentType: "application/json",
      body: JSON.stringify({ detail: "forbidden" }),
    });
  });

  await page.goto("/");
  // Vào thẳng màn qua chrome-link (không mint admin) — server-side 403 là boundary thật.
  await page.getByRole("button", { name: "Tạo shop" }).click();
  await page.getByLabel("Tên shop").fill("Shop Không Quyền");
  await page.getByRole("button", { name: "Tạo shop" }).click();

  await expect(page.locator(".toast")).toContainText("Bạn không có quyền admin để tạo shop.");
});

test("shell chrome đủ ba lối vào phụ trên màn picker", async ({ page }) => {
  await page.goto("/");
  for (const label of ["Hỏi AI", "Quản trị Wiki", "Tạo shop"]) {
    await expect(page.getByRole("button", { name: label })).toBeVisible();
  }
});
