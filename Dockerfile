# Backend image dùng chung cho 3 process (main_ohana_ai + main_seller + worker_seller).
# Cùng codebase, khác `command:` ở compose — tách 3 image tốn CI + registry, không tách
# = pattern chuẩn cho monorepo Python có nhiều entrypoint.
#
# python:3.11-slim khớp `requires-python = ">=3.11"` (pyproject.toml). Không 3.12+: openai
# 2.45 và pgvector 0.5 CHƯA verify trên 3.12 cluster này (pin cứng dependency có lý do
# ở pyproject.toml lines 6-28 — đừng đổi tại đây).

FROM python:3.11-slim AS base

# postgresql-client cho pg_isready (entrypoint chờ postgres healthy). curl cho healthcheck
# nội container. Không cài gcc/build-essential: openai/pydantic/pgvector đều có wheel
# manylinux, install-e không compile gì.
RUN apt-get update && apt-get install -y --no-install-recommends \
      postgresql-client \
      curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2 lớp copy: deps trước (đổi ít) → source sau (đổi nhiều). Cache invalidate đúng chỗ.
COPY pyproject.toml alembic.ini ./
# `pip install -e .` cần source tree tối thiểu để resolve package layout; copy đúng những
# gì `pyproject.toml` (setuptools auto-discovery) và runtime cần — KHÔNG copy tests/docs/
# web/ (đã ở .dockerignore, nhưng khai rõ để reader thấy scope).
COPY agent/ ./agent/
COPY api/ ./api/
COPY app/ ./app/
COPY auth/ ./auth/
COPY bridge/ ./bridge/
COPY channels/ ./channels/
COPY db/ ./db/
COPY parsing/ ./parsing/
COPY retrieval/ ./retrieval/
COPY scripts/ ./scripts/
COPY tools/ ./tools/

RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .

# Non-root user — uid 1000 khớp phần lớn host user cho bind-mount .env đọc được mà không
# cần chown chéo. `ohana` (không phải `root`) tạo blast radius nhỏ khi compromise container.
RUN useradd -m -u 1000 ohana && chown -R ohana:ohana /app

USER ohana

COPY --chown=ohana:ohana deploy/entrypoint.sh /entrypoint.sh

# Entrypoint chờ postgres, sau đó exec CMD. Default = main_ohana_ai; compose override
# `command:` cho main_seller / worker_seller.
ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "app.main_ohana_ai:app", "--host", "0.0.0.0", "--port", "8001"]
