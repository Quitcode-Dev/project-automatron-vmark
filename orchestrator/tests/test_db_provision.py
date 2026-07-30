"""Unit tests for database provisioning.

These cover naming/SQL-safety, key minting and the Traefik route shape. The
claim that actually matters — that a provisioned project is indistinguishable
from hosted Supabase to `_introspect_supabase_schema` — needs a real Postgres
and a real PostgREST, and is covered by `test_db_provision_live.py`.
"""

from __future__ import annotations

import jwt
import pytest

from orchestrator.db_provision import (
    ProvisionedDatabase,
    gateway,
    jwt_keys,
    naming,
    provisioning_unavailable_reason,
)
from orchestrator.db_provision.postgres import _ident, _literal, dsn_with

DSN = "postgresql://postgres:pw@automatron-pg:5432/postgres"


# --- Naming / SQL safety ---------------------------------------------------


def test_names_are_sql_safe_for_uuid_project_ids() -> None:
    pid = "3F2B9C4A-1D5E-4A7B-8C9D-0E1F2A3B4C5D"

    assert naming.database_name(pid) == "proj_3f2b9c4a1d5e4a7b8c9d0e1f2a3b4c5d"
    assert naming.authenticator_role(pid) == "auth_3f2b9c4a1d5e4a7b8c9d0e1f2a3b4c5d"
    # Both must survive the identifier guard the SQL builders apply.
    assert _ident(naming.database_name(pid))
    assert _ident(naming.authenticator_role(pid))


def test_project_slug_strips_injection_attempts() -> None:
    slug = naming.project_slug('a"; DROP DATABASE postgres; --')

    assert slug.isalnum()
    assert '"' not in slug and " " not in slug


def test_project_slug_rejects_unusable_ids() -> None:
    with pytest.raises(ValueError):
        naming.project_slug("---")


def test_ident_rejects_unsanitized_input() -> None:
    for bad in ('x"; DROP TABLE y; --', "Uppercase", "1leading_digit", "has space", ""):
        with pytest.raises(ValueError):
            _ident(bad)


def test_literal_rejects_non_alphanumeric() -> None:
    assert _literal("abc123XYZ") == "'abc123XYZ'"
    for bad in ("has'quote", "has space", "semi;colon"):
        with pytest.raises(ValueError):
            _literal(bad)


# --- DSN rewriting ---------------------------------------------------------


def test_dsn_with_swaps_credentials_and_database() -> None:
    out = dsn_with(DSN, user="auth_abc", password="s3cret", database="proj_abc")

    assert out == "postgresql://auth_abc:s3cret@automatron-pg:5432/proj_abc"


def test_dsn_with_database_only_keeps_admin_credentials() -> None:
    assert dsn_with(DSN, database="proj_abc") == (
        "postgresql://postgres:pw@automatron-pg:5432/proj_abc"
    )


def test_dsn_with_escapes_credentials() -> None:
    out = dsn_with(DSN, user="u@ser", password="p@ss/word")

    assert "u%40ser" in out and "p%40ss%2Fword" in out


# --- Key minting -----------------------------------------------------------


def test_minted_keys_are_supabase_shaped() -> None:
    secret = jwt_keys.generate_secret()

    anon, service = jwt_keys.mint_keypair(secret, ttl_seconds=3600, issued_at=1_700_000_000)

    for token, role in ((anon, "anon"), (service, "service_role")):
        # issued_at is pinned for determinism, so the token is long expired —
        # claim shape is what's under test here.
        claims = jwt.decode(token, secret, algorithms=["HS256"], options={"verify_exp": False})
        assert claims["role"] == role
        assert claims["iss"] == "supabase"
        assert claims["exp"] == 1_700_000_000 + 3600


def test_minted_keys_are_valid_now_with_the_default_ttl() -> None:
    """The production path mints from `time.time()` — those must not be born expired."""
    from orchestrator.config import settings

    secret = jwt_keys.generate_secret()
    anon, service = jwt_keys.mint_keypair(secret, settings.db_jwt_ttl_seconds)

    for token in (anon, service):
        claims = jwt.decode(token, secret, algorithms=["HS256"])
        assert claims["iss"] == "supabase"


def test_keys_do_not_verify_against_another_secret() -> None:
    """Per-project secrets are what stop one project's key working at another."""
    anon, _ = jwt_keys.mint_keypair(jwt_keys.generate_secret(), 3600)

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(anon, jwt_keys.generate_secret(), algorithms=["HS256"])


def test_derived_secret_is_stable_and_project_scoped() -> None:
    master = "master-secret"

    assert jwt_keys.derive_secret(master, "p1") == jwt_keys.derive_secret(master, "p1")
    assert jwt_keys.derive_secret(master, "p1") != jwt_keys.derive_secret(master, "p2")
    assert jwt_keys.derive_secret("other", "p1") != jwt_keys.derive_secret(master, "p1")


def test_derive_secret_requires_a_master() -> None:
    with pytest.raises(ValueError):
        jwt_keys.derive_secret("", "p1")


def test_generated_password_is_inlineable() -> None:
    """Passwords are inlined into CREATE ROLE, so the alphabet must stay safe."""
    assert _literal(jwt_keys.generate_password())


# --- Gateway ---------------------------------------------------------------


def test_traefik_route_strips_the_rest_prefix() -> None:
    """Without this the handed-out URL 404s: PostgREST serves at `/`, not `/rest/v1/`."""
    labels = gateway.traefik_labels("abc123", "db.example.com")

    assert labels["traefik.http.routers.db-abc123.rule"] == "Host(`db-abc123.db.example.com`)"
    assert labels["traefik.http.routers.db-abc123.middlewares"] == "db-abc123-strip"
    assert labels["traefik.http.middlewares.db-abc123-strip.stripprefix.prefixes"] == "/rest/v1"
    assert labels["traefik.http.services.db-abc123.loadbalancer.server.port"] == "3000"


def test_postgrest_environment_wires_roles_and_secret() -> None:
    env = gateway.postgrest_environment("postgresql://u:p@h/db", "sekret")

    assert env["PGRST_DB_URI"] == "postgresql://u:p@h/db"
    assert env["PGRST_JWT_SECRET"] == "sekret"
    assert env["PGRST_DB_ANON_ROLE"] == "anon"
    assert env["PGRST_DB_SCHEMAS"] == "public"


# --- Preflight -------------------------------------------------------------


def test_provisioning_is_off_by_default() -> None:
    assert provisioning_unavailable_reason() == "DB_PROVISION_ENABLED is false"


@pytest.mark.parametrize(
    "missing, expected",
    [
        ("db_shared_postgres_dsn", "DB_SHARED_POSTGRES_DSN"),
        ("db_base_domain", "DB_BASE_DOMAIN"),
        ("db_jwt_master_secret", "DB_JWT_MASTER_SECRET"),
    ],
)
def test_preflight_names_the_missing_setting(
    monkeypatch: pytest.MonkeyPatch, missing: str, expected: str
) -> None:
    from orchestrator.config import settings

    monkeypatch.setattr(settings, "db_provision_enabled", True)
    monkeypatch.setattr(settings, "db_shared_postgres_dsn", DSN)
    monkeypatch.setattr(settings, "db_base_domain", "db.example.com")
    monkeypatch.setattr(settings, "db_jwt_master_secret", "m")
    monkeypatch.setattr(settings, missing, "")

    reason = provisioning_unavailable_reason()

    assert reason is not None and expected in reason


async def test_teardown_is_a_noop_without_a_shared_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runs on every project deletion — must not touch Docker when unconfigured."""
    from orchestrator.config import settings
    from orchestrator.db_provision import teardown_project_database

    monkeypatch.setattr(settings, "db_shared_postgres_dsn", "")

    def explode() -> None:
        raise AssertionError("Docker must not be contacted when nothing was provisioned")

    monkeypatch.setattr(gateway, "_client", explode)

    await teardown_project_database("abc123")  # must not raise


async def test_deleting_a_project_reclaims_its_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing else collects provisioned resources — deletion has to."""
    from orchestrator.api import routes

    torn_down: list[str] = []

    async def fake_required(project_id: str) -> dict[str, str]:
        return {"id": project_id}

    async def fake_teardown(project_id: str) -> None:
        torn_down.append(project_id)

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(routes, "_get_required_project", fake_required)
    monkeypatch.setattr(routes, "update_project_stage", noop)
    monkeypatch.setattr(routes, "update_project_status", noop)
    monkeypatch.setattr(
        "orchestrator.db_provision.teardown_project_database", fake_teardown
    )

    result = await routes.api_delete_project("project-1")

    assert result["status"] == "deleted"
    assert torn_down == ["project-1"]


async def test_project_deletion_survives_teardown_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stuck container must not make a project undeletable."""
    from orchestrator.api import routes

    async def fake_required(project_id: str) -> dict[str, str]:
        return {"id": project_id}

    async def boom(project_id: str) -> None:
        raise RuntimeError("docker daemon unreachable")

    async def noop(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(routes, "_get_required_project", fake_required)
    monkeypatch.setattr(routes, "update_project_stage", noop)
    monkeypatch.setattr(routes, "update_project_status", noop)
    monkeypatch.setattr("orchestrator.db_provision.teardown_project_database", boom)

    result = await routes.api_delete_project("project-1")

    assert result["status"] == "deleted"


async def test_provision_refuses_before_any_side_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A half-configured deployment must fail cleanly, not part-provision."""
    from orchestrator.config import settings
    from orchestrator.db_provision import provision_project_database

    monkeypatch.setattr(settings, "db_provision_enabled", True)
    monkeypatch.setattr(settings, "db_shared_postgres_dsn", "")

    result = await provision_project_database("abc123")

    assert isinstance(result, ProvisionedDatabase)
    assert result.ok is False
    assert "DB_SHARED_POSTGRES_DSN" in result.error
