import { defineConfig } from "@playwright/test";

/**
 * Render tests thật cho SPA — tầng mà suite Python không với tới được (test_chat_ui.py là
 * source-contract: đọc file .tsx, không chạy browser; docstring ở đó tự khai giới hạn này).
 *
 * webServer build lại rồi serve `vite preview` thay vì `vite dev`: test đúng BUNDLE sẽ ship
 * (dist đang được commit — deviation P0 trong .gitignore), không phải cây source qua HMR.
 * Build ~400ms nên trả giá mỗi lần chạy là không đáng kể; `reuseExistingServer: false` để
 * không bao giờ test nhầm một preview cũ đang treo từ phiên trước.
 *
 * Mọi request `/api/**` trong test bị chặn bằng `page.route` TRƯỚC khi ra network — không
 * cần backend FastAPI/Postgres chạy. Đây là render test cho FE, không phải e2e full-stack;
 * hợp đồng field FE↔BE đã có gate introspection riêng trong tests/test_chat_ui.py.
 */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  reporter: process.env.CI ? "github" : "list",
  use: {
    baseURL: "http://localhost:4173",
    // Khung 430px — thiết kế mobile-first (xem comment "khung 430px" ở ChannelPicker.tsx).
    viewport: { width: 430, height: 860 },
  },
  webServer: {
    command: "npx vite build && npx vite preview --port 4173 --strictPort",
    url: "http://localhost:4173",
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
