"""Per-user project ownership, sharing, and admin override.

Every project belongs to a `users` row, and `require_project_access` — registered
once on the API router — is what stops one user reaching another's project by id.
These tests drive the real app through TestClient with only the *cookie decode*
stubbed, so the router dependency, the ownership queries and the routes are all
the production code paths.

Coverage:
  1.  A created project is owned by its creator
  2.  The list endpoint returns only the caller's projects
  3.  A non-owner gets 404 (not 403) on every project route shape — id existence
      is itself privileged information
  4.  Sharing grants read + drive access; revoking takes it away again
  5.  Collaborators cannot delete or re-share (owner-only operations)
  6.  Admins see and reach everything
  7.  Sharing with an email that could never sign in is rejected
  8.  Projects that predate ownership are backfilled to the first admin
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import aiosqlite
import pytest
from fastapi.testclient import TestClient

import orchestrator.auth as auth_module
from orchestrator.config import settings

# Mutable "who is signed in" — swapped between requests by `as_user`.
_SESSION: dict[str, Any] = {}


def as_user(email: str, sub: str) -> None:
    _SESSION.clear()
    _SESSION.update({"email": email, "sub": sub, "name": email.split("@")[0]})


@pytest.fixture()
def client(tmp_path: Any) -> Iterator[TestClient]:
    """The real app on a throwaway DB, with session-cookie decoding stubbed out."""
    import orchestrator.main as main_module

    db_path = str(tmp_path / "ownership.db")
    prev_db, prev_admins, prev_allowed, prev_dev = (
        settings.sqlite_db_path,
        settings.automatron_admin_emails,
        settings.automatron_allowed_emails,
        settings.automatron_dev_no_auth,
    )
    settings.sqlite_db_path = db_path
    settings.automatron_admin_emails = "boss@example.com"
    settings.automatron_allowed_emails = "@example.com"
    # A developer's local .env may set this; it makes every user an admin, which
    # would mask exactly what these tests check.
    settings.automatron_dev_no_auth = False

    async def fake_require_auth(request: Any) -> dict[str, Any]:
        return dict(_SESSION)

    real_require_auth = auth_module.require_auth
    auth_module.require_auth = fake_require_auth  # type: ignore[assignment]
    main_module.fastapi_app.dependency_overrides[main_module.require_auth] = lambda: dict(_SESSION)

    as_user("alice@example.com", "sub-alice")
    try:
        with TestClient(main_module.fastapi_app) as c:
            yield c
    finally:
        auth_module.require_auth = real_require_auth  # type: ignore[assignment]
        main_module.fastapi_app.dependency_overrides.clear()
        settings.sqlite_db_path = prev_db
        settings.automatron_admin_emails = prev_admins
        settings.automatron_allowed_emails = prev_allowed
        settings.automatron_dev_no_auth = prev_dev


def make_project(client: TestClient, name: str) -> dict[str, Any]:
    res = client.post(
        "/api/projects",
        json={"name": name, "repo_url": f"https://github.com/acme/{name}"},
    )
    assert res.status_code == 200, res.text
    return res.json()


# ---------------------------------------------------------------------------
# 1-2. Ownership on create, scoping on list
# ---------------------------------------------------------------------------


def test_creator_owns_the_project(client: TestClient) -> None:
    project = make_project(client, "alpha")

    assert project["owner_email"] == "alice@example.com"
    assert project["viewer_role"] == "owner"
    assert project["owner_id"]


def test_list_returns_only_the_callers_projects(client: TestClient) -> None:
    alice_project = make_project(client, "alpha")

    as_user("bob@example.com", "sub-bob")
    bob_project = make_project(client, "beta")

    assert [p["id"] for p in client.get("/api/projects").json()] == [bob_project["id"]]

    as_user("alice@example.com", "sub-alice")
    assert [p["id"] for p in client.get("/api/projects").json()] == [alice_project["id"]]


# ---------------------------------------------------------------------------
# 3. Direct-id access is closed on every route shape
# ---------------------------------------------------------------------------


def test_non_owner_gets_404_on_every_project_route(client: TestClient) -> None:
    project_id = make_project(client, "alpha")["id"]

    as_user("bob@example.com", "sub-bob")
    probes = [
        ("get", f"/api/projects/{project_id}"),
        ("get", f"/api/projects/{project_id}/issues"),
        ("get", f"/api/projects/{project_id}/plan"),
        ("get", f"/api/projects/{project_id}/logs"),
        ("get", f"/api/projects/{project_id}/chat-history"),
        ("get", f"/api/projects/{project_id}/trace"),
        ("get", f"/api/projects/{project_id}/members"),
        ("post", f"/api/projects/{project_id}/start"),
        ("post", f"/api/projects/{project_id}/stop"),
        ("post", f"/api/projects/{project_id}/audit"),
        ("post", f"/api/projects/{project_id}/sync-issues"),
        ("post", f"/api/projects/{project_id}/assign-copilot"),
        ("delete", f"/api/projects/{project_id}"),
    ]

    for method, path in probes:
        res = getattr(client, method)(path)
        assert res.status_code == 404, f"{method.upper()} {path} -> {res.status_code}"


def test_unknown_project_id_is_indistinguishable_from_someone_elses(client: TestClient) -> None:
    project_id = make_project(client, "alpha")["id"]

    as_user("bob@example.com", "sub-bob")
    forbidden = client.get(f"/api/projects/{project_id}")
    missing = client.get("/api/projects/00000000-0000-0000-0000-000000000000")

    assert forbidden.status_code == missing.status_code == 404
    assert forbidden.json() == missing.json()


async def test_deployment_resources_are_scoped_through_their_project(
    client: TestClient, tmp_path: Any
) -> None:
    """`/deployment-targets/{target_id}` carries no project_id, so the gate resolves
    the owning project from the row — otherwise it would be a way around the check."""
    project_id = make_project(client, "alpha")["id"]

    async with aiosqlite.connect(settings.sqlite_db_path) as db:
        await db.execute(
            """
            INSERT INTO deployment_targets
                (id, project_id, name, host, ssh_user, ssh_port, environment, domain,
                 app_name, deploy_path, created_at, updated_at)
            VALUES ('target-1', ?, 'prod', 'example.com', 'deploy', 22, 'production', NULL,
                    'alpha', '/srv/alpha', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')
            """,
            (project_id,),
        )
        await db.commit()

    assert client.get("/api/deployment-targets/target-1").status_code == 200

    as_user("bob@example.com", "sub-bob")
    assert client.get("/api/deployment-targets/target-1").status_code == 404
    assert client.get("/api/deployment-targets/target-1/inventory/latest").status_code == 404


# ---------------------------------------------------------------------------
# 4-5. Sharing
# ---------------------------------------------------------------------------


def test_sharing_grants_then_revoking_removes_access(client: TestClient) -> None:
    project_id = make_project(client, "alpha")["id"]

    added = client.post(f"/api/projects/{project_id}/members", json={"email": "bob@example.com"})
    assert added.status_code == 200, added.text
    bob_user_id = added.json()["user_id"]
    assert added.json()["role"] == "collaborator"

    members = client.get(f"/api/projects/{project_id}/members").json()
    assert [m["role"] for m in members] == ["owner", "collaborator"]

    as_user("bob@example.com", "sub-bob")
    assert client.get(f"/api/projects/{project_id}").status_code == 200
    shared = [p for p in client.get("/api/projects").json() if p["id"] == project_id]
    assert shared and shared[0]["viewer_role"] == "collaborator"
    assert shared[0]["owner_email"] == "alice@example.com"

    as_user("alice@example.com", "sub-alice")
    assert client.delete(f"/api/projects/{project_id}/members/{bob_user_id}").status_code == 200

    as_user("bob@example.com", "sub-bob")
    assert client.get(f"/api/projects/{project_id}").status_code == 404
    assert [p["id"] for p in client.get("/api/projects").json()] == []


def test_collaborator_cannot_delete_or_reshare(client: TestClient) -> None:
    project_id = make_project(client, "alpha")["id"]
    client.post(f"/api/projects/{project_id}/members", json={"email": "bob@example.com"})

    as_user("bob@example.com", "sub-bob")
    assert client.delete(f"/api/projects/{project_id}").status_code == 403
    assert (
        client.post(
            f"/api/projects/{project_id}/members", json={"email": "eve@example.com"}
        ).status_code
        == 403
    )
    # ...but a collaborator can still see who else has access, and drive the project.
    assert client.get(f"/api/projects/{project_id}/members").status_code == 200


def test_owner_cannot_be_removed(client: TestClient) -> None:
    project = make_project(client, "alpha")

    res = client.delete(f"/api/projects/{project['id']}/members/{project['owner_id']}")

    assert res.status_code == 422


def test_invited_user_is_pending_until_first_sign_in(client: TestClient) -> None:
    project_id = make_project(client, "alpha")["id"]

    invited = client.post(
        f"/api/projects/{project_id}/members", json={"email": "dave@example.com"}
    ).json()
    assert invited["pending"] is True

    # Dave signs in for the first time: the placeholder row is claimed, not duplicated,
    # so the project he was invited to is already waiting for him.
    as_user("dave@example.com", "sub-dave")
    assert [p["id"] for p in client.get("/api/projects").json()] == [project_id]

    as_user("alice@example.com", "sub-alice")
    dave = [m for m in client.get(f"/api/projects/{project_id}/members").json() if m["email"] == "dave@example.com"]
    assert dave and dave[0]["pending"] is False


# ---------------------------------------------------------------------------
# 7. Sharing validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email,reason",
    [
        ("carol@other-company.com", "not allowlisted — could never sign in to see it"),
        ("alice@example.com", "already has access as the owner"),
        ("not-an-email", "malformed"),
    ],
)
def test_invalid_share_targets_are_rejected(client: TestClient, email: str, reason: str) -> None:
    project_id = make_project(client, "alpha")["id"]

    res = client.post(f"/api/projects/{project_id}/members", json={"email": email})

    assert res.status_code == 422, f"{email} ({reason}) -> {res.status_code}"


# ---------------------------------------------------------------------------
# 6. Admin override
# ---------------------------------------------------------------------------


def test_admin_sees_and_reaches_every_project(client: TestClient) -> None:
    alice_id = make_project(client, "alpha")["id"]
    as_user("bob@example.com", "sub-bob")
    bob_id = make_project(client, "beta")["id"]

    as_user("boss@example.com", "sub-boss")
    listed = client.get("/api/projects").json()

    assert sorted(p["id"] for p in listed) == sorted([alice_id, bob_id])
    assert {p["viewer_role"] for p in listed} == {"admin"}
    assert client.get(f"/api/projects/{alice_id}").status_code == 200
    assert client.get(f"/api/projects/{bob_id}").status_code == 200


# ---------------------------------------------------------------------------
# 8. Backfill of pre-ownership projects
# ---------------------------------------------------------------------------


async def test_legacy_projects_are_claimed_by_the_first_admin(tmp_path: Any) -> None:
    """A project row with a NULL owner (written before ownership existed) is
    assigned to the first AUTOMATRON_ADMIN_EMAILS entry on the next startup."""
    from orchestrator.models.project import get_project, init_db
    from orchestrator.models.user import get_user_by_email

    db_path = str(tmp_path / "legacy.db")
    prev_db, prev_admins = settings.sqlite_db_path, settings.automatron_admin_emails
    settings.sqlite_db_path = db_path
    settings.automatron_admin_emails = "boss@example.com"
    try:
        await init_db(db_path)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                INSERT INTO projects (id, name, status, project_stage, intake_text,
                                      intake_source, created_at, updated_at, owner_id)
                VALUES ('legacy-1', 'Legacy', 'pending', 'intake', 'repo', 'manual',
                        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', NULL)
                """
            )
            await db.commit()

        # Second startup — this is what the backfill runs on.
        await init_db(db_path)

        admin = await get_user_by_email("boss@example.com")
        project = await get_project("legacy-1")
        assert admin is not None
        assert project is not None
        assert project["owner_id"] == admin["id"]
        assert project["owner_email"] == "boss@example.com"
    finally:
        settings.sqlite_db_path = prev_db
        settings.automatron_admin_emails = prev_admins


async def test_legacy_projects_stay_unowned_without_an_admin(tmp_path: Any) -> None:
    """No admin configured: we do not guess an owner. The projects stay hidden
    (and a warning is logged) until AUTOMATRON_ADMIN_EMAILS is set."""
    from orchestrator.models.project import get_project, init_db

    db_path = str(tmp_path / "legacy2.db")
    prev_db, prev_admins = settings.sqlite_db_path, settings.automatron_admin_emails
    settings.sqlite_db_path = db_path
    settings.automatron_admin_emails = ""
    try:
        await init_db(db_path)
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """
                INSERT INTO projects (id, name, status, project_stage, intake_text,
                                      intake_source, created_at, updated_at, owner_id)
                VALUES ('legacy-2', 'Legacy', 'pending', 'intake', 'repo', 'manual',
                        '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', NULL)
                """
            )
            await db.commit()

        await init_db(db_path)

        project = await get_project("legacy-2")
        assert project is not None
        assert project["owner_id"] is None
    finally:
        settings.sqlite_db_path = prev_db
        settings.automatron_admin_emails = prev_admins


# ---------------------------------------------------------------------------
# Admin matching rules
# ---------------------------------------------------------------------------


def test_admin_matching_accepts_exact_emails_and_domains() -> None:
    prev_admins, prev_dev = settings.automatron_admin_emails, settings.automatron_dev_no_auth
    settings.automatron_dev_no_auth = False
    try:
        settings.automatron_admin_emails = "boss@example.com, @quitcode.com"
        assert auth_module.is_admin("boss@example.com") is True
        assert auth_module.is_admin("BOSS@example.com") is True
        assert auth_module.is_admin("anyone@quitcode.com") is True
        assert auth_module.is_admin("bob@example.com") is False

        settings.automatron_admin_emails = ""
        assert auth_module.is_admin("boss@example.com") is False
    finally:
        settings.automatron_admin_emails = prev_admins
        settings.automatron_dev_no_auth = prev_dev


def test_dev_no_auth_user_is_an_admin() -> None:
    """Local dev (AUTOMATRON_DEV_NO_AUTH=1) must keep showing every project —
    otherwise a dev DB full of unowned projects looks empty."""
    prev = settings.automatron_dev_no_auth
    settings.automatron_dev_no_auth = True
    try:
        assert auth_module.is_admin("anyone@localhost") is True
    finally:
        settings.automatron_dev_no_auth = prev
