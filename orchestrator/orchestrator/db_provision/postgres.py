"""Per-project database + role setup on the shared Postgres.

Isolation model — two independent layers, because one is not enough:

1. **Connection scope.** Postgres roles are cluster-wide but a connection is
   bound to exactly one database. `anon` / `authenticated` / `service_role` are
   therefore created once and shared, while each project gets its own LOGIN role
   (`auth_<slug>`) whose CONNECT privilege is granted on its own database only,
   with PUBLIC's CONNECT revoked. Project A's PostgREST cannot open a connection
   to project B's database at all.
2. **Key scope.** Each project's PostgREST verifies bearer tokens against its
   own `PGRST_JWT_SECRET`, so A's service_role key does not authenticate at B.

Identifiers are not parameterizable in SQL, so every name reaching this module
must come from `naming.py`, which reduces them to `[a-z0-9_]`. `_ident` asserts
that invariant rather than trusting it.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote, urlsplit, urlunsplit

import asyncpg

logger = logging.getLogger(__name__)

SHARED_ROLES = ("anon", "authenticated", "service_role")

_SAFE_IDENT = re.compile(r"^[a-z][a-z0-9_]*$")
_SAFE_LITERAL = re.compile(r"^[A-Za-z0-9]+$")


def _ident(name: str) -> str:
    """Quote a pre-sanitized identifier, refusing anything unexpected."""
    if not _SAFE_IDENT.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return f'"{name}"'


def _literal(value: str) -> str:
    """Inline an alphanumeric literal (passwords from `jwt_keys.generate_password`).

    Utility statements like CREATE ROLE don't accept bind parameters, so the
    password has to be inlined. Restricting the alphabet to alphanumerics makes
    that safe by construction instead of by escaping.
    """
    if not _SAFE_LITERAL.match(value):
        raise ValueError("password must be alphanumeric to be safely inlined")
    return f"'{value}'"


def dsn_with(
    dsn: str,
    *,
    user: str | None = None,
    password: str | None = None,
    database: str | None = None,
) -> str:
    """Rewrite a Postgres DSN's credentials and/or database, keeping host:port."""
    parts = urlsplit(dsn)
    netloc = parts.netloc
    if user is not None or password is not None:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        eff_user = user if user is not None else (parts.username or "")
        eff_pw = password if password is not None else (parts.password or "")
        cred = quote(eff_user, safe="")
        if eff_pw:
            cred += ":" + quote(eff_pw, safe="")
        netloc = f"{cred}@{host}" if cred else host
    path = parts.path if database is None else f"/{database}"
    return urlunsplit((parts.scheme, netloc, path, parts.query, parts.fragment))


async def ensure_shared_roles(conn: asyncpg.Connection) -> None:
    """Create the cluster-wide anon/authenticated/service_role roles if absent.

    NOLOGIN: they are only ever reached via SET ROLE from an authenticator that
    is already scoped to a single database.
    """
    for role in SHARED_ROLES:
        exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
        if exists:
            continue
        # service_role is the trusted server-side identity, mirroring Supabase's
        # own: it must see rows regardless of RLS policies the migration adds.
        extra = " BYPASSRLS" if role == "service_role" else ""
        await conn.execute(f"CREATE ROLE {_ident(role)} NOLOGIN NOINHERIT{extra}")
        logger.info("db_provision: created shared role %s", role)


async def ensure_project_database(conn: asyncpg.Connection, database: str) -> bool:
    """Create the project's database if absent. Returns True if it was created."""
    exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", database)
    if exists:
        return False
    # CREATE DATABASE cannot run inside a transaction block; asyncpg's execute
    # is autocommit unless a transaction is open, so this is fine as-is.
    await conn.execute(f"CREATE DATABASE {_ident(database)}")
    logger.info("db_provision: created database %s", database)
    return True


async def ensure_authenticator(
    conn: asyncpg.Connection, role: str, password: str, database: str
) -> None:
    """Create/refresh the project's PostgREST login role, scoped to its database."""
    exists = await conn.fetchval("SELECT 1 FROM pg_roles WHERE rolname = $1", role)
    if exists:
        # Re-provisioning: reset the password so the caller's freshly generated
        # DSN is the one that works.
        await conn.execute(f"ALTER ROLE {_ident(role)} WITH PASSWORD {_literal(password)}")
    else:
        await conn.execute(
            f"CREATE ROLE {_ident(role)} LOGIN NOINHERIT PASSWORD {_literal(password)}"
        )
        logger.info("db_provision: created authenticator role %s", role)

    await conn.execute(
        f"GRANT {', '.join(_ident(r) for r in SHARED_ROLES)} TO {_ident(role)}"
    )
    # Lock the database down to this authenticator only.
    await conn.execute(f"REVOKE CONNECT ON DATABASE {_ident(database)} FROM PUBLIC")
    await conn.execute(f"GRANT CONNECT ON DATABASE {_ident(database)} TO {_ident(role)}")


async def grant_schema_privileges(project_dsn: str) -> None:
    """Grant schema access inside the project's database.

    Also sets default privileges so tables created *later* by this same role are
    reachable without re-granting. Migrations applied by a different role still
    need `grant_existing_objects` afterwards — default privileges only cover the
    role that set them.
    """
    conn = await asyncpg.connect(project_dsn)
    try:
        roles = ", ".join(_ident(r) for r in SHARED_ROLES)
        await conn.execute(f"GRANT USAGE ON SCHEMA public TO {roles}")
        await conn.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT ALL ON TABLES TO {_ident('service_role')}"
        )
        await conn.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
            f"GRANT ALL ON SEQUENCES TO {_ident('service_role')}"
        )
        await conn.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO "
            f"{_ident('anon')}, {_ident('authenticated')}"
        )
    finally:
        await conn.close()


async def grant_existing_objects(project_dsn: str) -> None:
    """Re-grant across everything currently in `public`.

    Called after a migration lands. PostgREST only exposes tables the connecting
    role can reach, so skipping this is exactly the "No tables exposed via
    PostgREST (RLS or grants?)" introspection failure.
    """
    conn = await asyncpg.connect(project_dsn)
    try:
        await conn.execute(
            f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {_ident('service_role')}"
        )
        await conn.execute(
            f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {_ident('service_role')}"
        )
        await conn.execute(
            f"GRANT SELECT ON ALL TABLES IN SCHEMA public TO "
            f"{_ident('anon')}, {_ident('authenticated')}"
        )
    finally:
        await conn.close()


async def notify_schema_reload(project_dsn: str) -> None:
    """Tell PostgREST to rebuild its schema cache.

    PostgREST builds the cache once at startup and then only on NOTIFY. Without
    this, a migration applied after the container started stays invisible: the
    OpenAPI document keeps reporting zero tables and introspection fails with
    "No tables exposed via PostgREST" even though the tables plainly exist.
    """
    conn = await asyncpg.connect(project_dsn)
    try:
        await conn.execute("NOTIFY pgrst, 'reload schema'")
    finally:
        await conn.close()


async def drop_project_database(
    conn: asyncpg.Connection, database: str, role: str
) -> None:
    """Tear down a project's database and authenticator role."""
    await conn.execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
        database,
    )
    await conn.execute(f"DROP DATABASE IF EXISTS {_ident(database)}")
    await conn.execute(f"DROP ROLE IF EXISTS {_ident(role)}")
    logger.info("db_provision: dropped database %s and role %s", database, role)
