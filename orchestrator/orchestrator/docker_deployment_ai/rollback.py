"""Rollback metadata capture — MVP state.

ROLLBACK EXECUTION IS DISABLED FOR MVP (Option B decision).

Reason: UPLOAD_FILE and WRITE_ENV_FILE actions — required to restore the
previous compose file to the remote server — are not yet implemented.
Without file upload, `ROLLBACK_TO_PREVIOUS_RELEASE` would silently do nothing,
creating false confidence that a rollback succeeded.

What IS implemented:
  - `capture_rollback_metadata()` — captures server state before each deploy
    (containers, images, compose snapshot). This data is preserved so a
    future implementation can use it.

What is NOT implemented:
  - `execute_rollback()` — returns explicit NOT_IMPLEMENTED error.
  - The `rollback_available` flag on deploy_runs is always 0 (never set).

The API route POST /deployment-runs/{run_id}/rollback returns HTTP 501.

P1 task to implement: add UPLOAD_FILE / heredoc-based compose restore,
then enable execute_rollback and set rollback_available=1 after capture.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from orchestrator.docker_deployment_ai.ssh_client import SSHClient
from orchestrator.logsafe import redact

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def capture_rollback_metadata(
    *,
    run_id: str,
    plan: dict[str, Any],
    target: Any,  # DeploymentTarget
    db: aiosqlite.Connection,
) -> str:
    """Capture a pre-deploy server snapshot into deployment_rollbacks.

    This is best-effort: SSH failures are caught and logged, not raised.
    The rollback record is written with rollback_status='metadata_only'
    to make clear that execution is not yet possible.

    Returns the rollback_id (UUID string).
    """
    rollback_id = str(uuid.uuid4())

    ssh = SSHClient(
        host=target.host,
        user=target.ssh_user,
        port=target.ssh_port,
        ssh_key_path=target.auth_reference if target.auth_mode == "ssh_key" else None,
    )

    deploy_path = target.deploy_path
    compose_file = "docker-compose.yml"

    current_containers = ""
    current_images = ""
    current_compose = ""

    try:
        rc, ps_out, _ = ssh.run("docker ps --format json", timeout=20)
        if rc == 0:
            current_containers = ps_out
    except Exception as exc:
        logger.debug("Rollback metadata: docker ps failed: %s", exc)

    try:
        rc, images_out, _ = ssh.run("docker images --format json", timeout=20)
        if rc == 0:
            current_images = images_out
    except Exception as exc:
        logger.debug("Rollback metadata: docker images failed: %s", exc)

    try:
        rc, compose_out, _ = ssh.run(f"cat {deploy_path}/{compose_file}", timeout=10)
        if rc == 0:
            current_compose = compose_out
    except Exception as exc:
        logger.debug("Rollback metadata: cat compose failed: %s", exc)

    rollback_snapshot = {
        "mvp_note": (
            "Rollback execution is disabled in this version. "
            "This record captures pre-deploy state only."
        ),
        "previous_containers_snapshot": current_containers[:5000],
        "previous_images_snapshot": current_images[:5000],
        "previous_compose_snapshot": current_compose[:10_000],
    }

    await db.execute(
        """
        INSERT INTO deployment_rollbacks
        (id, run_id, rollback_status, previous_release_ref, rollback_plan_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            rollback_id,
            run_id,
            "metadata_only",
            plan.get("_previous_release_ref") or "",
            json.dumps(rollback_snapshot),
            _now(),
        ),
    )
    # rollback_available stays 0 — execution is not available yet
    await db.commit()
    logger.info(
        "Rollback metadata captured for run %s (metadata_only; execution disabled)",
        run_id,
    )
    return rollback_id


async def execute_rollback(
    *,
    run_id: str,
    project_id: str,
    target: Any,
    db: aiosqlite.Connection,
) -> dict[str, Any]:
    """Rollback execution — NOT IMPLEMENTED in this version.

    Returns a structured error so callers get an unambiguous failure
    instead of a false success.
    """
    return {
        "status": "not_implemented",
        "error": (
            "Rollback execution is not available in this version. "
            "UPLOAD_FILE and WRITE_ENV_FILE actions required for compose restore "
            "are not yet implemented. "
            "To rollback manually: ssh to the server, restore the previous "
            "docker-compose.yml, and run docker compose up -d."
        ),
    }
