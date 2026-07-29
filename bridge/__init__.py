"""Outbound integrations — Ohana platform REST (F2), Zalo OA (F3). Transport-only clients.

Handlers upstream translate bridge exceptions into `{success, error}` envelopes; no bridge
error should surface to the LLM as an unhandled exception (that would leak internal state).
"""
