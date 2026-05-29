"""CRUD for deployment_targets table."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from orchestrator.docker_deployment_ai.models import DeploymentTarget, DeploymentTargetCreate


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_target(row: dict[str, Any]) -> DeploymentTarget:
    return DeploymentTarget(
        id=row["id"],
        project_id=row["project_id"],
        name=row["name"],
        host=row["host"],
        ssh_user=row["ssh_user"],
        ssh_port=row["ssh_port"] or 22,
        environment=row.get("environment", "production"),
        domain=row.get("domain"),
        app_name=row["app_name"],
        deploy_path=row["deploy_path"],
        preferred_strategy=row.get("preferred_strategy", "auto_detect"),
        auth_mode=row.get("auth_mode", "ssh_key"),
        auth_reference=row.get("auth_reference"),
        created_by=row.get("created_by"),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_inventory_snapshot_id=row.get("last_inventory_snapshot_id"),
    )


async def create_target(
    db: aiosqlite.Connection,
    data: DeploymentTargetCreate,
    created_by: str | None = None,
) -> DeploymentTarget:
    row_id = str(uuid.uuid4())
    now = _now()
    await db.execute(
        """
        INSERT INTO deployment_targets
        (id, project_id, name, host, ssh_user, ssh_port, environment,
         domain, app_name, deploy_path, preferred_strategy, auth_mode,
         auth_reference, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id, data.project_id, data.name, data.host, data.ssh_user,
            data.ssh_port, data.environment.value, data.domain, data.app_name,
            data.deploy_path, data.preferred_strategy.value, data.auth_mode,
            data.auth_reference, created_by, now, now,
        ),
    )
    await db.commit()
    return DeploymentTarget(
        id=row_id, created_by=created_by, created_at=now, updated_at=now,
        **data.model_dump(),
    )


async def get_target(db: aiosqlite.Connection, target_id: str) -> DeploymentTarget | None:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM deployment_targets WHERE id = ?", (target_id,)
    )
    row = await cursor.fetchone()
    return _row_to_target(dict(row)) if row else None


async def list_targets(
    db: aiosqlite.Connection, project_id: str
) -> list[DeploymentTarget]:
    db.row_factory = aiosqlite.Row
    cursor = await db.execute(
        "SELECT * FROM deployment_targets WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,),
    )
    rows = await cursor.fetchall()
    return [_row_to_target(dict(r)) for r in rows]


async def update_last_snapshot(
    db: aiosqlite.Connection, target_id: str, snapshot_id: str
) -> None:
    await db.execute(
        "UPDATE deployment_targets SET last_inventory_snapshot_id = ?, updated_at = ? WHERE id = ?",
        (snapshot_id, _now(), target_id),
    )
    await db.commit()
