"""Persistence boundary — tenant-first schema.

Every row-owning table carries `shop_id` and every read path is scoped by it (SQL-level
WHERE, never post-filter). Cross-shop leakage (R1.22 analog) is the failure mode we
guard against; the tenant-isolation test in tests/test_tenant_isolation.py is the gate.
"""
