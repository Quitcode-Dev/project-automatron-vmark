"""TypedExecutor — executes deployment plans using an explicit allowlist of action types.

Safety contract (P0 requirements):
  1. Action types: only the allowlist is accepted. SHELL/EXEC/BASH/SH/RUN_COMMAND/
     CUSTOM_COMMAND are rejected BEFORE any side effect.
  2. Parameter injection: every param from the LLM-generated plan is validated
     through param_validators and shlex.quoted by ExecutorSSHClient before
     it appears in any remote command string.
  3. Pre-execution gate: approval must exist, plan.risk_level != blocked,
     and validation must have passed (enforced in orchestrator.execute_plan).
  4. Unimplemented actions (UPLOAD_FILE, WRITE_ENV_FILE, DOCKER_LOGIN,
     ROLLBACK_TO_PREVIOUS_RELEASE) fail with an explicit error rather than
     returning a false-success "acknowledged" response.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from orchestrator.docker_deployment_ai.events import (
    emit_run_failed,
    emit_run_started,
    emit_run_step_completed,
    emit_run_step_failed,
    emit_run_step_started,
)
from orchestrator.docker_deployment_ai.executor_ssh_client import ExecutorSSHClient
from orchestrator.docker_deployment_ai.param_validators import (
    ParameterValidationError,
    validate_compose_file,
    validate_deploy_path,
    validate_env_file_path,
    validate_file_content,
    validate_log_lines,
    validate_network_driver,
    validate_network_name,
    validate_path_under_deploy_dir,
    validate_safe_name,
    validate_service_name,
    validate_volume_name,
)
from orchestrator.logsafe import redact

logger = logging.getLogger(__name__)

_ALLOWED_ACTION_TYPES = frozenset(
    [
        "CREATE_DIRECTORY",
        "UPLOAD_FILE",
        "WRITE_ENV_FILE",
        "DOCKER_LOGIN",
        "DOCKER_COMPOSE_CONFIG",
        "DOCKER_COMPOSE_PULL",
        "DOCKER_COMPOSE_BUILD",
        "DOCKER_COMPOSE_UP",
        "DOCKER_COMPOSE_DOWN",
        "DOCKER_COMPOSE_PS",
        "DOCKER_NETWORK_CREATE",
        "DOCKER_VOLUME_CREATE",
        "RUN_HEALTHCHECK",
        "CAPTURE_LOGS",
        "MARK_RELEASE",
        "ROLLBACK_TO_PREVIOUS_RELEASE",
    ]
)

# Explicitly forbidden — rejected before any side effect
_FORBIDDEN_ACTION_TYPES = frozenset(
    ["SHELL", "RUN_COMMAND", "EXEC", "BASH", "SH", "CUSTOM_COMMAND"]
)

# Actions that are schema-valid but not yet implemented for this version.
# These are also blocked by the validator before a plan can be approved/executed.
# Plans containing these actions WILL fail before any SSH call.
_NOT_IMPLEMENTED_ACTIONS = frozenset(
    ["DOCKER_LOGIN", "ROLLBACK_TO_PREVIOUS_RELEASE"]
)


class ActionTypeRejected(ValueError):
    """Raised when a plan contains a forbidden or unknown action type."""


class ActionNotImplemented(RuntimeError):
    """Raised for schema-valid actions that are not yet implemented."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _save_step(
    db: aiosqlite.Connection,
    *,
    run_id: str,
    step_index: int,
    action_type: str,
    status: str,
    started_at: str,
    finished_at: str | None,
    stdout_excerpt: str,
    stderr_excerpt: str,
    error_message: str | None,
) -> str:
    step_id = str(uuid.uuid4())
    await db.execute(
        """
        INSERT INTO deployment_run_steps
        (id, run_id, step_index, action_type, status,
         started_at, finished_at, stdout_excerpt, stderr_excerpt, error_message)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            step_id, run_id, step_index, action_type, status,
            started_at, finished_at,
            redact(stdout_excerpt[:2000]),
            redact(stderr_excerpt[:2000]),
            redact(error_message or ""),
        ),
    )
    await db.commit()
    return step_id


async def _update_run(
    db: aiosqlite.Connection,
    run_id: str,
    *,
    status: str,
    current_step: int | None = None,
    finished_at: str | None = None,
    health_status: str | None = None,
) -> None:
    parts = ["status = ?"]
    values: list[Any] = [status]
    if current_step is not None:
        parts.append("current_step = ?")
        values.append(current_step)
    if finished_at:
        parts.append("finished_at = ?")
        values.append(finished_at)
    if health_status:
        parts.append("health_status = ?")
        values.append(health_status)
    values.append(run_id)
    await db.execute(f"UPDATE deploy_runs SET {', '.join(parts)} WHERE id = ?", values)
    await db.commit()


def _validate_actions(actions: list[dict[str, Any]]) -> None:
    """Raise ActionTypeRejected before any side effect if any action is unsafe."""
    for action in actions:
        atype = (action.get("action_type") or "").upper()
        if atype in _FORBIDDEN_ACTION_TYPES:
            raise ActionTypeRejected(
                f"Action type '{atype}' is explicitly forbidden. "
                "Only typed deployment actions are permitted."
            )
        if atype and atype not in _ALLOWED_ACTION_TYPES:
            raise ActionTypeRejected(
                f"Unknown action type '{atype}'. "
                f"Allowed: {sorted(_ALLOWED_ACTION_TYPES)}"
            )


async def _check_approval(db: aiosqlite.Connection, plan_id: str) -> bool:
    cursor = await db.execute(
        "SELECT id FROM deployment_approvals WHERE plan_id = ? LIMIT 1", (plan_id,)
    )
    row = await cursor.fetchone()
    return row is not None


async def _execute_action(
    action: dict[str, Any],
    ssh: ExecutorSSHClient,
    *,
    deploy_path: str,
) -> tuple[str, str, str | None]:
    """Execute a single typed action via ExecutorSSHClient.

    All params are validated by param_validators and quoted by ExecutorSSHClient.
    Returns (stdout, stderr, error_or_None).
    """
    atype = (action.get("action_type") or "").upper()
    params = action.get("params") or {}

    # Reject unimplemented actions with an explicit error (never silently acknowledge)
    if atype in _NOT_IMPLEMENTED_ACTIONS:
        return "", "", (
            f"Action '{atype}' is not implemented in this version. "
            "This action type is schema-valid but requires additional infrastructure "
            "(SCP/heredoc file upload, credential store). "
            "Remove this action from the plan or implement it before executing."
        )

    try:
        if atype == "UPLOAD_FILE":
            path = validate_path_under_deploy_dir(
                params.get("path") or "", deploy_path
            )
            content = validate_file_content(params.get("content") or "", "content")
            rc, out, err = ssh.run_upload_file(path, content)
            return out, err, (None if rc == 0 else redact(err or f"upload_file exited {rc}"))

        if atype == "WRITE_ENV_FILE":
            path = validate_env_file_path(params.get("path") or "", deploy_path)
            content = validate_file_content(params.get("content") or "", "content")
            rc, out, err = ssh.run_write_env_file(path, content)
            return out, err, (None if rc == 0 else redact(err or f"write_env_file exited {rc}"))

        if atype == "CREATE_DIRECTORY":
            path = validate_deploy_path(params.get("path") or deploy_path)
            rc, out, err = ssh.run_mkdir(path)
            return out, err, (None if rc == 0 else redact(err or f"mkdir exited {rc}"))

        if atype == "DOCKER_COMPOSE_CONFIG":
            cf = validate_compose_file(params.get("compose_file", "docker-compose.yml"))
            dp = validate_deploy_path(deploy_path)
            rc, out, err = ssh.run_docker_compose_config(dp, cf)
            return out, err, (None if rc == 0 else redact(err or f"compose config exited {rc}"))

        if atype == "DOCKER_COMPOSE_PULL":
            cf = validate_compose_file(params.get("compose_file", "docker-compose.yml"))
            dp = validate_deploy_path(deploy_path)
            rc, out, err = ssh.run_docker_compose_pull(dp, cf)
            return out, err, (None if rc == 0 else redact(err or f"compose pull exited {rc}"))

        if atype == "DOCKER_COMPOSE_BUILD":
            cf = validate_compose_file(params.get("compose_file", "docker-compose.yml"))
            dp = validate_deploy_path(deploy_path)
            rc, out, err = ssh.run_docker_compose_build(dp, cf)
            return out, err, (None if rc == 0 else redact(err or f"compose build exited {rc}"))

        if atype == "DOCKER_COMPOSE_UP":
            cf = validate_compose_file(params.get("compose_file", "docker-compose.yml"))
            dp = validate_deploy_path(deploy_path)
            svc = validate_service_name(params.get("service", ""))
            rc, out, err = ssh.run_docker_compose_up(dp, cf, svc)
            return out, err, (None if rc == 0 else redact(err or f"compose up exited {rc}"))

        if atype == "DOCKER_COMPOSE_DOWN":
            cf = validate_compose_file(params.get("compose_file", "docker-compose.yml"))
            dp = validate_deploy_path(deploy_path)
            rc, out, err = ssh.run_docker_compose_down(dp, cf)
            return out, err, (None if rc == 0 else redact(err or f"compose down exited {rc}"))

        if atype == "DOCKER_COMPOSE_PS":
            cf = validate_compose_file(params.get("compose_file", "docker-compose.yml"))
            dp = validate_deploy_path(deploy_path)
            rc, out, err = ssh.run_docker_compose_ps(dp, cf)
            return out, err, (None if rc == 0 else redact(err or f"compose ps exited {rc}"))

        if atype == "DOCKER_NETWORK_CREATE":
            net = validate_network_name(params.get("network_name", ""))
            if not net:
                return "", "", "network_name is required for DOCKER_NETWORK_CREATE"
            driver = validate_network_driver(params.get("driver", "bridge"))
            rc, out, err = ssh.run_docker_network_create(net, driver)
            return out, err, (None if rc == 0 else redact(err or f"network create exited {rc}"))

        if atype == "DOCKER_VOLUME_CREATE":
            vol = validate_volume_name(params.get("volume_name", ""))
            if not vol:
                return "", "", "volume_name is required for DOCKER_VOLUME_CREATE"
            rc, out, err = ssh.run_docker_volume_create(vol)
            return out, err, (None if rc == 0 else redact(err or f"volume create exited {rc}"))

        if atype == "CAPTURE_LOGS":
            cf = validate_compose_file(params.get("compose_file", "docker-compose.yml"))
            dp = validate_deploy_path(deploy_path)
            svc = validate_service_name(params.get("service", ""))
            lines = validate_log_lines(params.get("lines", 100))
            rc, out, _ = ssh.run_docker_compose_logs(dp, cf, svc, lines)
            return redact(out), "", None

        if atype == "RUN_HEALTHCHECK":
            return "Healthcheck delegated to healthcheck module", "", None

        if atype == "MARK_RELEASE":
            ref = validate_safe_name(params.get("ref", "unknown"), "ref")
            return f"Release marked: {ref}", "", None

        return "", "", f"Unhandled action type: {atype}"

    except ParameterValidationError as exc:
        return "", "", f"Parameter validation failed: {exc}"
    except Exception as exc:
        return "", "", redact(str(exc))


async def execute_plan(
    *,
    plan_id: str,
    plan: dict[str, Any],
    target: Any,  # DeploymentTarget
    run_id: str,
    project_id: str,
    started_by: str,
    db: aiosqlite.Connection,
) -> dict[str, Any]:
    """Execute a validated + approved deployment plan.

    Pre-execution gates are enforced in orchestrator.execute_plan before
    this function is called. This function additionally:
      - Validates action types BEFORE any side effect.
      - Validates plan parameters BEFORE each SSH call.
      - Stops on first failure; rollback is a separate human decision.
    """
    # Local approval check — belt-and-suspenders guard
    if not await _check_approval(db, plan_id):
        return {"status": "rejected", "error": "Deployment approval is required before execution"}

    if plan.get("risk_level") == "blocked":
        return {"status": "rejected", "error": "Plan has risk_level=blocked and cannot be executed"}

    actions = plan.get("deployment_actions") or []

    # Validate all action types up-front — no side effects yet
    try:
        _validate_actions(actions)
    except ActionTypeRejected as exc:
        logger.error("Action type rejection for run %s: %s", run_id, exc)
        return {"status": "rejected", "error": str(exc)}

    await _update_run(db, run_id, status="running")
    await emit_run_started(project_id, run_id, plan_id)

    ssh = ExecutorSSHClient(
        host=target.host,
        user=target.ssh_user,
        port=target.ssh_port,
        ssh_key_path=target.auth_reference if target.auth_mode == "ssh_key" else None,
    )

    steps_completed = 0
    steps_failed = 0

    for idx, action in enumerate(actions):
        atype = action.get("action_type", "UNKNOWN").upper()
        started_at = _now()
        await emit_run_step_started(project_id, run_id, idx, atype)
        await _update_run(db, run_id, status="running", current_step=idx)

        stdout, stderr, error = await _execute_action(
            action, ssh, deploy_path=target.deploy_path
        )

        status = "failed" if error else "ok"
        await _save_step(
            db,
            run_id=run_id,
            step_index=idx,
            action_type=atype,
            status=status,
            started_at=started_at,
            finished_at=_now(),
            stdout_excerpt=stdout,
            stderr_excerpt=stderr,
            error_message=error,
        )

        if error:
            steps_failed += 1
            await emit_run_step_failed(project_id, run_id, idx, error)
            logger.error("Run %s step %d (%s) failed: %s", run_id, idx, atype, redact(error))
            await _update_run(db, run_id, status="failed", finished_at=_now())
            await emit_run_failed(project_id, run_id, error)
            return {
                "status": "failed",
                "run_id": run_id,
                "steps_completed": steps_completed,
                "steps_failed": steps_failed,
                "error": redact(error),
            }

        steps_completed += 1
        await emit_run_step_completed(project_id, run_id, idx)

    await _update_run(db, run_id, status="completed", finished_at=_now(), health_status="starting")
    return {
        "status": "completed",
        "run_id": run_id,
        "steps_completed": steps_completed,
        "steps_failed": 0,
    }
