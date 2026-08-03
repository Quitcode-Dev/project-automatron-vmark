"""User identity and per-project membership.

Auth.js owns sign-in; this module owns the *local* record of who signed in, so
projects can be attributed to a person and shared with other people.

A `users` row is created on first sign-in (`upsert_user`) and also, with a NULL
`sub`, when someone is invited to a project before they have ever signed in —
the row is claimed on their first login by email match.

Access to a project is: owner, or a `project_members` row, or an admin
(`AUTOMATRON_ADMIN_EMAILS`). Kept out of `models/project.py` for the same reason
as `docker_deployment_ai/schema.py` — one entry point, `init_user_schema(db)`,
called from `models.project.init_db`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from orchestrator.models import project as project_model

logger = logging.getLogger(__name__)

ROLE_OWNER = "owner"
ROLE_COLLABORATOR = "collaborator"
ROLE_ADMIN = "admin"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_email(email: str | None) -> str:
    """Emails are stored lowercased so plain `UNIQUE` / `=` behave case-insensitively."""
    return (email or "").strip().lower()


async def init_user_schema(db: aiosqlite.Connection) -> None:
    """Create the users / project_members tables. Idempotent."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            sub           TEXT,
            email         TEXT NOT NULL UNIQUE,
            name          TEXT,
            image         TEXT,
            created_at    TEXT NOT NULL,
            last_login_at TEXT
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS project_members (
            project_id TEXT NOT NULL,
            user_id    TEXT NOT NULL,
            role       TEXT NOT NULL DEFAULT 'collaborator',
            invited_by TEXT,
            created_at TEXT NOT NULL,
            PRIMARY KEY (project_id, user_id),
            FOREIGN KEY (project_id) REFERENCES projects(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
        """
    )
    await db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_sub ON users(sub) WHERE sub IS NOT NULL")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id)")
    await db.execute("CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_id)")


async def upsert_user(
    *,
    sub: str | None,
    email: str,
    name: str | None = None,
    image: str | None = None,
) -> dict[str, Any]:
    """Find-or-create the local user row for a signed-in session.

    Matched by `sub` first, then by email — the email path is what claims a
    placeholder row created by an invite (see `add_project_member`) and links it
    to the Google account on first sign-in.
    """
    email = normalize_email(email)
    if not email:
        raise ValueError("upsert_user requires an email")

    async with aiosqlite.connect(project_model._db_path) as db:
        db.row_factory = aiosqlite.Row
        row = None
        if sub:
            cursor = await db.execute("SELECT * FROM users WHERE sub = ?", (sub,))
            row = await cursor.fetchone()
        if row is None:
            cursor = await db.execute("SELECT * FROM users WHERE email = ?", (email,))
            row = await cursor.fetchone()

        now = _now()
        if row is None:
            user_id = str(uuid.uuid4())
            await db.execute(
                """
                INSERT INTO users (id, sub, email, name, image, created_at, last_login_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, sub, email, name, image, now, now),
            )
        else:
            user_id = row["id"]
            await db.execute(
                """
                UPDATE users
                SET sub = COALESCE(?, sub),
                    email = ?,
                    name = COALESCE(?, name),
                    image = COALESCE(?, image),
                    last_login_at = ?
                WHERE id = ?
                """,
                (sub, email, name, image, now, user_id),
            )
        await db.commit()

        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        created = await cursor.fetchone()
        return dict(created) if created else {}


async def get_user_by_email(email: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(project_model._db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE email = ?", (normalize_email(email),))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    async with aiosqlite.connect(project_model._db_path) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE id = ?", (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def ensure_user_by_email(email: str) -> dict[str, Any]:
    """Get-or-create a user row from an email alone (no `sub` yet).

    Used when inviting someone who has not signed in — the row is a placeholder
    until `upsert_user` claims it.
    """
    existing = await get_user_by_email(email)
    if existing:
        return existing
    return await upsert_user(sub=None, email=email)


async def is_project_member(project_id: str, user_id: str) -> bool:
    async with aiosqlite.connect(project_model._db_path) as db:
        cursor = await db.execute(
            "SELECT 1 FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        )
        return await cursor.fetchone() is not None


async def list_project_members(project_id: str) -> list[dict[str, Any]]:
    """Members of a project, owner first. The owner is synthesised from
    `projects.owner_id` — ownership is not duplicated into `project_members`."""
    async with aiosqlite.connect(project_model._db_path) as db:
        db.row_factory = aiosqlite.Row
        members: list[dict[str, Any]] = []

        cursor = await db.execute("SELECT owner_id FROM projects WHERE id = ?", (project_id,))
        project_row = await cursor.fetchone()
        owner_id = project_row["owner_id"] if project_row else None
        if owner_id:
            cursor = await db.execute("SELECT * FROM users WHERE id = ?", (owner_id,))
            owner = await cursor.fetchone()
            if owner:
                members.append(
                    {
                        "user_id": owner["id"],
                        "email": owner["email"],
                        "name": owner["name"],
                        "image": owner["image"],
                        "role": ROLE_OWNER,
                        "pending": owner["sub"] is None,
                        "created_at": owner["created_at"],
                    }
                )

        cursor = await db.execute(
            """
            SELECT pm.role, pm.created_at, u.id, u.email, u.name, u.image, u.sub
            FROM project_members pm
            JOIN users u ON u.id = pm.user_id
            WHERE pm.project_id = ?
            ORDER BY pm.created_at
            """,
            (project_id,),
        )
        for row in await cursor.fetchall():
            members.append(
                {
                    "user_id": row["id"],
                    "email": row["email"],
                    "name": row["name"],
                    "image": row["image"],
                    "role": row["role"],
                    "pending": row["sub"] is None,
                    "created_at": row["created_at"],
                }
            )
        return members


async def add_project_member(
    project_id: str,
    email: str,
    *,
    role: str = ROLE_COLLABORATOR,
    invited_by: str | None = None,
) -> dict[str, Any]:
    """Share a project with an email. Creates a placeholder user if needed."""
    user = await ensure_user_by_email(email)
    async with aiosqlite.connect(project_model._db_path) as db:
        await db.execute(
            """
            INSERT INTO project_members (project_id, user_id, role, invited_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(project_id, user_id) DO UPDATE SET role = excluded.role
            """,
            (project_id, user["id"], role, invited_by, _now()),
        )
        await db.commit()
    return {
        "user_id": user["id"],
        "email": user["email"],
        "name": user.get("name"),
        "image": user.get("image"),
        "role": role,
        "pending": user.get("sub") is None,
    }


async def remove_project_member(project_id: str, user_id: str) -> bool:
    async with aiosqlite.connect(project_model._db_path) as db:
        cursor = await db.execute(
            "DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
            (project_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def backfill_project_owners(db: aiosqlite.Connection, admin_emails: list[str]) -> None:
    """Give every ownerless project to the first configured admin.

    Runs on every startup and is idempotent, so projects that predate ownership
    get claimed as soon as `AUTOMATRON_ADMIN_EMAILS` is set and the process is
    restarted. With no admin configured, unowned projects stay unowned (and so
    are invisible to everyone) — we log loudly rather than guessing an owner.
    """
    cursor = await db.execute("SELECT COUNT(*) FROM projects WHERE owner_id IS NULL")
    row = await cursor.fetchone()
    unowned = row[0] if row else 0
    if not unowned:
        return

    if not admin_emails:
        logger.warning(
            "%d project(s) have no owner and AUTOMATRON_ADMIN_EMAILS is not set — "
            "they are hidden from every user. Set AUTOMATRON_ADMIN_EMAILS and restart to claim them.",
            unowned,
        )
        return

    email = normalize_email(admin_emails[0])
    now = _now()
    cursor = await db.execute("SELECT id FROM users WHERE email = ?", (email,))
    existing = await cursor.fetchone()
    if existing:
        admin_id = existing[0]
    else:
        admin_id = str(uuid.uuid4())
        await db.execute(
            "INSERT INTO users (id, sub, email, name, image, created_at) VALUES (?, NULL, ?, NULL, NULL, ?)",
            (admin_id, email, now),
        )

    await db.execute("UPDATE projects SET owner_id = ? WHERE owner_id IS NULL", (admin_id,))
    await db.commit()
    logger.info("Assigned %d ownerless project(s) to admin %s", unowned, email)
