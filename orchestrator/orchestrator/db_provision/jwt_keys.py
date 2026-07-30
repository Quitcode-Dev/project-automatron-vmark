"""Mint Supabase-shaped anon / service_role keys.

PostgREST authenticates by verifying the bearer JWT against `PGRST_JWT_SECRET`
and switching to the role named in the `role` claim. Supabase's hosted keys are
exactly that — HS256 JWTs with `role` and `iss: supabase` — which is why a key
minted here is accepted by unmodified `@supabase/supabase-js` and by
`_introspect_supabase_schema`'s `Authorization: Bearer` header.

The secret is per project, so one project's service_role key is worthless
against another project's PostgREST.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string
import time

import jwt

ANON_ROLE = "anon"
SERVICE_ROLE = "service_role"

# Alphanumeric only. These values get inlined into `CREATE ROLE ... PASSWORD`
# and into container env, neither of which we want to think about quoting for.
_PASSWORD_ALPHABET = string.ascii_letters + string.digits


def generate_secret(length: int = 64) -> str:
    """A JWT signing secret. PostgREST requires at least 32 bytes for HS256."""
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def generate_password(length: int = 48) -> str:
    return "".join(secrets.choice(_PASSWORD_ALPHABET) for _ in range(length))


def derive_secret(master_secret: str, project_id: str) -> str:
    """Derive a project's signing secret from the server-side master.

    Deterministic, so restarting PostgREST reissues a secret that already-handed-
    out keys still verify against — no extra column, no rotation-on-restart bug.
    HMAC means holding one project's secret reveals nothing about another's.
    """
    if not master_secret:
        raise ValueError("db_jwt_master_secret is not set")
    return hmac.new(
        master_secret.encode("utf-8"), project_id.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def mint_key(secret: str, role: str, ttl_seconds: int, issued_at: int | None = None) -> str:
    """Mint one Supabase-shaped JWT. `issued_at` is injectable for tests."""
    iat = int(time.time()) if issued_at is None else issued_at
    return jwt.encode(
        {"role": role, "iss": "supabase", "iat": iat, "exp": iat + ttl_seconds},
        secret,
        algorithm="HS256",
    )


def mint_keypair(
    secret: str, ttl_seconds: int, issued_at: int | None = None
) -> tuple[str, str]:
    """Return `(anon_key, service_role_key)`."""
    return (
        mint_key(secret, ANON_ROLE, ttl_seconds, issued_at),
        mint_key(secret, SERVICE_ROLE, ttl_seconds, issued_at),
    )
