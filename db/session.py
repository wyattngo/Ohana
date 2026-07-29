"""Async engine + session factory. `DATABASE_URL` sourced via `app.config.Settings` (spec 05
Phase P2 — migrated off the direct `os.environ.get(...)` this used before, same treatment
`auth/identity.py get_jwt_secret()` got). Callers hold `session_factory` (an
`async_sessionmaker`) and open a session per unit of work — never share a session across
requests.

Builds a FRESH `Settings()` per call rather than the `@lru_cache`d `app.config.get_settings()`
— see `get_jwt_secret()`'s docstring for the staleness trap that cache carries (this read
isn't security-sensitive the way that one is, but a fresh read costs nothing and keeps every
migrated env-reader in this repo behaving the same way: re-read `os.environ` on every call,
identical to what `os.environ.get(...)` did pre-P2).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings


def get_database_url() -> str:
    return Settings().database_url


def make_engine() -> AsyncEngine:
    return create_async_engine(get_database_url(), echo=False)


def make_session_factory(engine: AsyncEngine | None = None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine or make_engine(), expire_on_commit=False)
