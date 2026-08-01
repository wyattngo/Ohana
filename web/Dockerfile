# Web image = SPA build + nginx serve + reverse proxy tới backend containers.
# 1 nginx container serve /usr/share/nginx/html (static SPA) VÀ proxy /api /webhook —
# tách 2 nginx (1 SPA + 1 proxy) không đáng chi phí ở single-host; mọi request đi qua
# cùng vhost đã chấm cert Let's Encrypt.
#
# Build context = REPO ROOT (compose set `context: .`), KHÔNG phải `web/`, để COPY
# được cả `web/` (source SPA) lẫn `deploy/nginx.conf` (proxy config) — hai thứ ở hai
# nhánh khác nhau của repo.

# ── Stage 1: build SPA ────────────────────────────────────────────────────────
# Node 20, KHÔNG 22 — corepack pnpm 11 vỡ trên Node 20 (memory `ohana-web-toolchain.md`).
# Repo pin pnpm@10 (lockfile khớp).
FROM node:20-alpine AS build

WORKDIR /web

RUN corepack enable && corepack prepare pnpm@10 --activate

# Cache dep install: chỉ package.json + lockfile trước
COPY web/package.json web/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

# Copy source + build. `tsc -b && vite build` (package.json scripts.build)
COPY web/ ./
RUN pnpm build

# ── Stage 2: nginx runtime ────────────────────────────────────────────────────
FROM nginx:1.27-alpine AS runtime

# SPA build artifact
COPY --from=build /web/dist /usr/share/nginx/html

# Reverse proxy + SPA fallback. Override toàn bộ default.conf.
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf

# Healthcheck: nginx trả 200 ở /health (khai trong nginx.conf)
HEALTHCHECK --interval=10s --timeout=3s --retries=3 \
    CMD wget -qO- http://127.0.0.1/health || exit 1

EXPOSE 80 443
