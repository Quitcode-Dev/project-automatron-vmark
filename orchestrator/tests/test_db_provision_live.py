"""Live end-to-end provisioning test. Opt-in: needs Docker and pulls images.

Run with:  AUTOMATRON_LIVE_DB_TESTS=1 pytest tests/test_db_provision_live.py -v

Everything else about provisioning can be unit-tested, but the claim the whole
design rests on cannot: that a provisioned project is indistinguishable from
hosted Supabase to `_introspect_supabase_schema`. That needs a real Postgres, a
real PostgREST, real grants and a real minted JWT — so it lives here.

The gateway is nginx rather than Traefik. Traefik's docker provider needs access
to the Docker socket, which Docker Desktop does not reliably grant a container;
nginx's `proxy_pass` with a trailing slash performs the identical /rest/v1 -> /
rewrite. The Traefik label shape is asserted in `test_db_provision.py`.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time

import asyncpg
import httpx
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("AUTOMATRON_LIVE_DB_TESTS") != "1",
    reason="set AUTOMATRON_LIVE_DB_TESTS=1 to run (starts Docker containers)",
)

NET = "automatron-livetest-net"
PG = "automatron-livetest-pg"
GATEWAY = "automatron-livetest-gateway"
PG_PW = "livetestpw"
GATEWAY_PORT = 18080
PG_PORT = 15432
PROJECT_ID = "11112222-3333-4444-5555-666677778888"

MIGRATION = """
CREATE TABLE donors (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    email text,
    total_given numeric DEFAULT 0
);
CREATE TABLE gifts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    donor_id uuid NOT NULL REFERENCES donors(id),
    amount numeric NOT NULL
);
"""


def _cleanup(client) -> None:  # type: ignore[no-untyped-def]
    from orchestrator.db_provision.naming import container_name

    for name in (GATEWAY, PG, container_name(PROJECT_ID)):
        with contextlib.suppress(Exception):
            client.containers.get(name).remove(force=True)
    with contextlib.suppress(Exception):
        client.networks.get(NET).remove()


async def _wait_for_postgres(dsn: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = await asyncpg.connect(dsn)
            await conn.close()
            return
        except Exception:
            await asyncio.sleep(1)
    raise RuntimeError("Postgres did not become ready")


async def _wait_for_gateway(url: str, timeout: float = 60) -> None:
    """Ready means PostgREST answered — a 404 is the gateway itself."""
    deadline = time.time() + timeout
    last = ""
    async with httpx.AsyncClient(timeout=5) as c:
        while time.time() < deadline:
            try:
                r = await c.get(url)
                if r.status_code in (200, 401, 403):
                    return
                last = f"HTTP {r.status_code}"
            except Exception as exc:
                last = str(exc)
            await asyncio.sleep(1)
    raise RuntimeError(f"gateway never reached PostgREST: {last}")


@pytest.fixture
async def provisioned(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    import docker

    from orchestrator.config import settings
    from orchestrator.db_provision import provision_project_database
    from orchestrator.db_provision.naming import container_name

    client = docker.from_env()
    _cleanup(client)
    client.networks.create(NET, driver="bridge")
    client.containers.run(
        "postgres:16-alpine",
        detach=True,
        name=PG,
        network=NET,
        environment={"POSTGRES_PASSWORD": PG_PW},
        ports={"5432/tcp": PG_PORT},
    )

    # The host reaches Postgres on localhost; PostgREST reaches it by container
    # name. That split is exactly what db_postgrest_dsn exists for.
    admin_dsn = f"postgresql://postgres:{PG_PW}@localhost:{PG_PORT}/postgres"
    await _wait_for_postgres(admin_dsn)

    monkeypatch.setattr(settings, "db_provision_enabled", True)
    monkeypatch.setattr(settings, "db_shared_postgres_dsn", admin_dsn)
    monkeypatch.setattr(
        settings, "db_postgrest_dsn", f"postgresql://postgres:{PG_PW}@{PG}:5432/postgres"
    )
    monkeypatch.setattr(settings, "db_base_domain", "db.livetest.local")
    monkeypatch.setattr(settings, "db_jwt_master_secret", "live-test-master-secret")
    monkeypatch.setattr(settings, "preview_traefik_network", NET)
    monkeypatch.setattr(settings, "preview_traefik_certresolver", "")

    result = await provision_project_database(PROJECT_ID)
    assert result.ok, result.error

    # nginx resolves its upstream at config load, so it starts after PostgREST.
    conf = (
        "server {\n  listen 80;\n  location /rest/v1/ {\n"
        f"    proxy_pass http://{container_name(PROJECT_ID)}:3000/;\n  }}\n}}\n"
    )
    client.containers.run(
        "nginx:alpine",
        detach=True,
        name=GATEWAY,
        network=NET,
        environment={"CONF": conf},
        command=[
            "sh",
            "-c",
            'printf "%s" "$CONF" > /etc/nginx/conf.d/default.conf && '
            "exec nginx -g 'daemon off;'",
        ],
        ports={"80/tcp": GATEWAY_PORT},
    )
    gateway = f"http://localhost:{GATEWAY_PORT}"
    await _wait_for_gateway(f"{gateway}/rest/v1/")

    # Redirect the real introspection call at the local gateway. It does
    # `import httpx` inside the function, so the patch lands on httpx itself.
    real_client = httpx.AsyncClient

    class Redirecting(real_client):  # type: ignore[misc, valid-type]
        async def get(self, url, **kw):  # type: ignore[override]
            return await super().get(url.replace(result.url, gateway), **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Redirecting)

    try:
        yield result
    finally:
        _cleanup(client)
        client.close()


async def test_empty_database_is_refused(provisioned) -> None:  # type: ignore[no-untyped-def]
    """Provisioning alone must NOT satisfy the schema gate.

    This is the load-bearing assumption of the whole design: an empty database
    still fails introspection, so a schema has to be designed and applied before
    planning can proceed.
    """
    from orchestrator.orchestrator import _introspect_supabase_schema

    schema = await _introspect_supabase_schema(
        provisioned.url, provisioned.service_role_key
    )

    assert schema.ok is False
    assert "No tables exposed" in schema.error


async def test_migration_makes_the_project_look_like_supabase(provisioned) -> None:  # type: ignore[no-untyped-def]
    """The end-to-end claim: after a migration, existing code paths just work."""
    from orchestrator.db_provision import apply_migration
    from orchestrator.orchestrator import _introspect_supabase_schema

    assert await apply_migration(PROJECT_ID, MIGRATION) is None
    await asyncio.sleep(2)  # PostgREST acts on the NOTIFY asynchronously

    schema = await _introspect_supabase_schema(
        provisioned.url, provisioned.service_role_key
    )

    assert schema.ok, schema.error
    assert set(schema.tables) >= {"donors", "gifts"}
    assert set(schema.tables["donors"]) >= {"id", "name", "email", "total_given"}
    # The compile-time safety net has to be real, not the permissive stub.
    assert "export type Database" in schema.typescript
    assert "donors" in schema.typescript and "gifts" in schema.typescript


async def test_a_forged_key_is_rejected(provisioned) -> None:  # type: ignore[no-untyped-def]
    """Per-project signing secrets are what stop one project reading another's data."""
    from orchestrator.db_provision import jwt_keys
    from orchestrator.orchestrator import _introspect_supabase_schema

    forged = jwt_keys.mint_key(jwt_keys.generate_secret(), "service_role", 3600)

    result = await _introspect_supabase_schema(provisioned.url, forged)

    assert result.ok is False
    assert "401" in result.error
