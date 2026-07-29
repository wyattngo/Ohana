"""Auth boundary — verified identity carries tenant scope.

`shop_id` MUST come from a signed JWT claim, never from a request body/header/webhook
payload (R1.1 extended for multi-tenant). The rest of the codebase treats
`auth.identity.Identity` as the only tenant-scope source.
"""
