"""Provision a Supabase-shaped database for a project that has no credentials.

The result is deliberately indistinguishable from hosted Supabase: an origin
serving PostgREST under `/rest/v1/`, plus `anon` and `service_role` JWTs. That
is what lets every existing code path — `_introspect_supabase_schema`, the
generated `database.types.ts`, the architect's schema gate, `preview.py`'s env
injection — work against a provisioned project with no changes at all.

Provisioning an *empty* database is not on its own enough to unblock planning:
introspection reports "No tables exposed via PostgREST" and the architect's gate
still (correctly) aborts. A schema has to be designed and applied first; see
`apply_migration`, which the planning flow calls once the architect has emitted
one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import asyncpg

from orchestrator.config import settings
from orchestrator.db_provision import jwt_keys, postgres
from orchestrator.db_provision.gateway import start_postgrest, stop_postgrest
from orchestrator.db_provision.naming import (
    api_host,
    authenticator_role,
    database_name,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ProvisionedDatabase",
    "apply_migration",
    "provision_project_database",
    "provisioning_unavailable_reason",
    "teardown_project_database",
]


@dataclass
class ProvisionedDatabase:
    """Outcome of a provisioning attempt.

    Mirrors `SupabaseSchema`'s shape: callers MUST check `ok` and surface
    `error` rather than treating an empty url as "not configured".
    """

    ok: bool
    url: str = ""
    anon_key: str = ""
    service_role_key: str = ""
    error: str = ""


def provisioning_unavailable_reason() -> str | None:
    """Return why provisioning can't run, or None when it can.

    Checked before any side effect so a half-configured deployment fails with a
    specific message instead of a partially created database.
    """
    if not settings.db_provision_enabled:
        return "DB_PROVISION_ENABLED is false"
    if not settings.db_shared_postgres_dsn:
        return "DB_SHARED_POSTGRES_DSN is not set"
    if not settings.db_base_domain:
        return (
            "DB_BASE_DOMAIN is not set — provisioned projects are addressed at "
            "`/rest/v1/`, which only the Traefik stripprefix route provides; "
            "without it the generated URL would 404 on every request"
        )
    if not settings.db_jwt_master_secret:
        return "DB_JWT_MASTER_SECRET is not set"
    if not settings.preview_traefik_network:
        return "PREVIEW_TRAEFIK_NETWORK is not set (shared with preview routing)"
    return None


async def provision_project_database(project_id: str) -> ProvisionedDatabase:
    """Create this project's database, role, keys and PostgREST container.

    Idempotent: re-running resets the authenticator password and recreates the
    container, but keeps the same database and — because the signing secret is
    derived, not random — the same anon/service_role keys.
    """
    reason = provisioning_unavailable_reason()
    if reason:
        return ProvisionedDatabase(ok=False, error=f"Database provisioning unavailable: {reason}")

    database = database_name(project_id)
    role = authenticator_role(project_id)
    password = jwt_keys.generate_password()
    secret = jwt_keys.derive_secret(settings.db_jwt_master_secret, project_id)
    anon_key, service_role_key = jwt_keys.mint_keypair(secret, settings.db_jwt_ttl_seconds)

    try:
        admin = await asyncpg.connect(settings.db_shared_postgres_dsn)
    except Exception as exc:
        return ProvisionedDatabase(ok=False, error=f"Cannot reach shared Postgres: {exc}")

    try:
        await postgres.ensure_shared_roles(admin)
        await postgres.ensure_project_database(admin, database)
        await postgres.ensure_authenticator(admin, role, password, database)
    except Exception as exc:
        logger.exception("db_provision: Postgres setup failed for %s", project_id)
        return ProvisionedDatabase(ok=False, error=f"Postgres setup failed: {exc}")
    finally:
        await admin.close()

    admin_project_dsn = postgres.dsn_with(settings.db_shared_postgres_dsn, database=database)
    try:
        await postgres.grant_schema_privileges(admin_project_dsn)
    except Exception as exc:
        logger.exception("db_provision: grant failed for %s", project_id)
        return ProvisionedDatabase(ok=False, error=f"Granting schema privileges failed: {exc}")

    postgrest_dsn = postgres.dsn_with(
        settings.db_postgrest_dsn or settings.db_shared_postgres_dsn,
        user=role,
        password=password,
        database=database,
    )
    try:
        start_postgrest(project_id, postgrest_dsn, secret)
    except Exception as exc:
        logger.exception("db_provision: PostgREST start failed for %s", project_id)
        return ProvisionedDatabase(ok=False, error=f"Starting PostgREST failed: {exc}")

    return ProvisionedDatabase(
        ok=True,
        url=f"https://{api_host(project_id, settings.db_base_domain)}",
        anon_key=anon_key,
        service_role_key=service_role_key,
    )


async def apply_migration(project_id: str, sql: str) -> str | None:
    """Run the architect's migration against the project's database.

    Returns None on success, or an error string. Re-grants afterwards: PostgREST
    only exposes what the connecting role can reach, and tables created here are
    owned by the admin role, so the default privileges set at provisioning time
    do not cover them.
    """
    if not sql.strip():
        return "migration SQL is empty"

    dsn = postgres.dsn_with(settings.db_shared_postgres_dsn, database=database_name(project_id))
    try:
        conn = await asyncpg.connect(dsn)
    except Exception as exc:
        return f"cannot connect to project database: {exc}"

    try:
        async with conn.transaction():
            await conn.execute(sql)
    except Exception as exc:
        logger.exception("db_provision: migration failed for %s", project_id)
        return f"migration failed: {exc}"
    finally:
        await conn.close()

    try:
        await postgres.grant_existing_objects(dsn)
    except Exception as exc:
        return f"migration applied but granting privileges failed: {exc}"

    # Both of these are required before the new tables are visible: the grants
    # decide what the role may reach, the NOTIFY makes PostgREST re-read the
    # schema it cached at startup. Skipping either leaves introspection
    # reporting an empty database.
    try:
        await postgres.notify_schema_reload(dsn)
    except Exception as exc:
        return f"migration applied but PostgREST schema reload failed: {exc}"

    return None


async def teardown_project_database(project_id: str) -> None:
    """Remove the container, database and role. Called when a project is deleted.

    Without this, provisioned resources accumulate forever — the host is
    stateful now, and nothing else reclaims them.
    """
    # No shared Postgres configured means nothing was ever provisioned, so
    # there is nothing to reclaim — return before touching Docker, since this
    # runs on every project deletion including in deployments that never
    # enabled provisioning. Keyed on the DSN rather than db_provision_enabled
    # so that turning the feature off still lets existing projects be cleaned up.
    if not settings.db_shared_postgres_dsn:
        return

    try:
        stop_postgrest(project_id)
    except Exception:
        logger.exception("db_provision: could not stop container for %s", project_id)

    admin = await asyncpg.connect(settings.db_shared_postgres_dsn)
    try:
        await postgres.drop_project_database(
            admin, database_name(project_id), authenticator_role(project_id)
        )
    finally:
        await admin.close()
