"""Outbound Ohana platform bridge — async REST client for `{base_url}/{method}` endpoints.

Transport only — no typed per-endpoint methods (those live in `tools/ohana_read.py`), no
retry, no cache (deferred). Ported shape-for-shape from drnickv4/bridge/onfa_client.py; the
key deviations for Ohana are:

  1. R1.1 EXTENDED — a handler's identity is (`user_id`, `shop_id`), so `call()` takes BOTH
     as SEPARATE required args and writes them into the body LAST. Even if a caller (or an
     LLM-emitted args dict) smuggles `user_id`/`shop_id` into `params`, the verified values
     overwrite them. The client itself never decides these; it forwards whatever the caller
     (which must read `auth.identity.Identity`) supplied.
  2. `verify=True` is hardcoded on the lazy-built client (R1.3) — no code path can disable
     TLS verification, even by config.
  3. Envelope contract mirrors ONFA's: HTTP 200 + `{"status": true, "data": …}` unwraps to
     `data`; `{"status": false}` raises `OhanaAppError`. PRE-002 unresolved — the real
     platform envelope is assumed to follow the same shape (backfill when spec lands).
  4. No pre-auth / call_preauth path — F2 is read-only for authenticated sellers. If the
     platform ever needs a pre-auth flow (e.g. OAuth exchange), add a distinct method with
     its own no-identity contract; do NOT relax `.call()`.

Constructor takes `base_url` + `service_key` directly (no `app.config` coupling yet — the
Phase 3+ config wire will inject a `Settings` object). Path `/{method}` — no `/drnick_api/`
prefix; the Ohana platform base URL is expected to already point at the API root.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Method names are code literals in tools/* — never strings the LLM emits. This is
# defence-in-depth against path injection, not the primary guard.
_METHOD_RE = re.compile(r"\A[a-z0-9_]+\Z")


class OhanaError(Exception):
    """Base for all Ohana bridge failures."""


class OhanaConfigError(OhanaError):
    """Bridge is not configured (missing base URL or service key) — raised loudly at call time."""


class OhanaAuthError(OhanaError):
    """Platform rejected the service key (HTTP 401)."""


class OhanaRateLimitError(OhanaError):
    """Platform rate limit hit (HTTP 429). No auto-retry here — the caller decides."""


class OhanaAppError(OhanaError):
    """HTTP 200 but `status: false` — app-level failure. Carries the platform's message
    verbatim, plus the optional structured `data.reason` so callers can branch on the
    CAUSE, not just the human message."""

    def __init__(self, message: str, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class OhanaTransportError(OhanaError):
    """Network / timeout / TLS / unexpected HTTP status — no valid reply was produced."""


class OhanaProtocolError(OhanaError):
    """Reply was not the expected envelope (bad JSON, or missing the `status` field)."""


class OhanaClient:
    """Async client for the Ohana platform REST API.

    Inject an `httpx.AsyncClient` for tests (MockTransport); otherwise one is built lazily
    from the constructor's `base_url` + `service_key` with `verify=True` hardcoded.
    """

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        base_url: str = "",
        service_key: str = "",
        timeout_s: float = 10.0,
    ) -> None:
        self._client = client
        self._owns_client = client is None
        self._base_url = base_url
        self._service_key = service_key
        self._timeout_s = timeout_s

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        if not self._base_url:
            raise OhanaConfigError("base_url not set")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_s,
            verify=True,  # R1.3: TLS verification is mandatory; no path disables it.
        )
        return self._client

    async def call(
        self,
        method: str,
        *,
        user_id: str,
        shop_id: str,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """POST to `/{method}` with the verified `(user_id, shop_id)` and `params`.

        Returns the `data` field on success (a legitimate `null` stays `None`; never coerced).
        Raises an `OhanaError` subclass on any failure; nothing is swallowed. `params` is
        never mutated — a fresh dict is built for the request body.
        """
        if not _METHOD_RE.fullmatch(method):
            raise ValueError(f"invalid ohana method name: {method!r}")
        if not self._service_key:
            raise OhanaConfigError("service_key not set")

        # Verified identity written LAST → overrides any user_id/shop_id smuggled into params
        # (R1.1 extended). guardrail: allow R1_TIER_FROM_BODY
        payload: dict[str, Any] = dict(params or {})
        payload["user_id"] = user_id
        payload["shop_id"] = shop_id

        client = self._ensure_client()
        started = time.monotonic()
        try:
            resp = await client.post(
                f"/{method}",
                json=payload,
                headers={"X-Ohana-Key": self._service_key},
            )
        except httpx.RequestError as exc:  # timeout, connect, TLS, DNS …
            raise OhanaTransportError(f"{method}: {exc}") from exc
        latency_ms = int((time.monotonic() - started) * 1000)

        # NEVER log the service key or the params (secret + PII). Method / identity / status only.
        if resp.status_code == 401:
            logger.warning(
                "ohana_call method=%s shop_id=%s user_id=%s rejected=auth",
                method,
                shop_id,
                user_id,
            )
            raise OhanaAuthError(f"{method}: service key rejected")
        if resp.status_code == 429:
            logger.warning(
                "ohana_call method=%s shop_id=%s user_id=%s rejected=ratelimit",
                method,
                shop_id,
                user_id,
            )
            raise OhanaRateLimitError(f"{method}: rate limit")
        if resp.status_code >= 400:
            raise OhanaTransportError(f"{method}: unexpected http {resp.status_code}")

        try:
            body = resp.json()
        except ValueError as exc:
            raise OhanaProtocolError(f"{method}: invalid json") from exc
        if not isinstance(body, dict) or "status" not in body:
            raise OhanaProtocolError(f"{method}: missing status field")

        if body.get("status") is not True:
            data = body.get("data")
            reason = data.get("reason") if isinstance(data, dict) else None
            raise OhanaAppError(
                body.get("message") or f"{method}: platform returned status=false", reason
            )

        logger.info(
            "ohana_call method=%s shop_id=%s user_id=%s status=ok latency_ms=%d",
            method,
            shop_id,
            user_id,
            latency_ms,
        )
        return body.get("data")

    async def aclose(self) -> None:
        """Close the underlying client if we created it (no-op for an injected one)."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
