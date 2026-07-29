"""Alembic environment — Phase 2 wires `db.models.Base.metadata` for tenant-first schema.

Reads DATABASE_URL from env directly (no app.config dependency until Phase 3+).
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from db.models import Base

config = context.config

target_metadata = Base.metadata

# Env vars override the (missing) [alembic] url so `alembic current` doesn't require the .ini
# to hardcode secrets. Falls back to a localhost DSN so the command doesn't crash in dev.
_url = os.environ.get("DATABASE_URL", "postgresql+psycopg://ohana:ohana@localhost:5432/ohana")
config.set_main_option("sqlalchemy.url", _url)


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live DB (`alembic upgrade head --sql`)."""
    context.configure(
        url=_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live DB connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
