"""Post-deployment health checker.

Runs after `executor.execute_plan` completes. Checks:
  1. Container status via remote `docker compose ps`
  2. HTTP health probe if an app URL is known
  3. Container logs scan for obvious startup errors

On failure, asks Docker AI (Gordon preferred) to explain sanitized logs.
"""

from __future__ import annotations

import logging
from typing import Any

import aiosqlite
import httpx

from orchestrator.docker_deployment_ai.events import (
    emit_healthcheck_completed,
    emit_healthcheck_started,
    emit_run_completed,
    emit_run_failed,
)
from orchestrator.docker_deployment_ai.models import HealthStatus
from orchestrator.docker_deployment_ai.ssh_client import SSHClient
from orchestrator.logsafe import redact

logger = logging.getLogger(__name__)

_STARTUP_ERROR_PATTERNS = [
    "error", "fatal", "exception", "panic", "segfault",
    "address already in use", "permission denied", "no such file",
    "exec format error", "cannot open",
]


def _scan_logs_for_errors(logs: str) -> list[str]:
    """Return lines that match known startup error patterns."""
    hits = []
    for line in logs.splitlines():
        lower = line.lower()
        if any(p in lower for p in _STARTUP_ERROR_PATTERNS):
            hits.append(line[:300])
    return hits[:20]


async def _http_check(url: str, timeout: int = 10) -> tuple[bool, str]:
    """Return (healthy, description)."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code < 400:
                return True, f"HTTP {resp.status_code}"
            return False, f"HTTP {resp.status_code}"
    except httpx.ConnectError as exc:
        return False, f"Connection refused: {exc}"
    except httpx.TimeoutException:
        return False, "Timeout"
    except Exception as exc:
        return False, str(exc)


async def run_healthcheck(
    *,
    run_id: str,
    project_id: str,
    plan: dict[str, Any],
    target: Any,  # DeploymentTarget
    db: aiosqlite.Connection,
) -> dict[str, Any]:
    """Run health checks after deployment. Returns status summary."""
    await emit_healthcheck_started(project_id, run_id)

    ssh = SSHClient(
        host=target.host,
        user=target.ssh_user,
        port=target.ssh_port,
        ssh_key_path=target.auth_reference if target.auth_mode == "ssh_key" else None,
    )

    health_info: dict[str, Any] = {
        "container_status": None,
        "http_check": None,
        "log_errors": [],
        "status": HealthStatus.unknown.value,
    }

    # 1. docker compose ps
    deploy_path = target.deploy_path
    compose_file = "docker-compose.yml"
    rc, ps_out, _ = ssh.run(
        f"docker compose -f {deploy_path}/{compose_file} ps --format json",
        timeout=30,
    )
    if rc == 0 and ps_out:
        health_info["container_status"] = ps_out[:2000]
        if "running" in ps_out.lower() or "up" in ps_out.lower():
            health_info["status"] = HealthStatus.healthy.value
        else:
            health_info["status"] = HealthStatus.unhealthy.value

    # 2. HTTP health probe
    routing_plan = plan.get("routing_plan") or {}
    domain = routing_plan.get("domain")
    if domain:
        url = f"https://{domain}/api/health"
        ok, desc = await _http_check(url)
        health_info["http_check"] = {"url": url, "ok": ok, "description": desc}
        if not ok and health_info["status"] == HealthStatus.healthy.value:
            health_info["status"] = HealthStatus.degraded.value

    # 3. Log scan
    rc_logs, logs_out, _ = ssh.run(
        f"docker compose -f {deploy_path}/{compose_file} logs --tail 100",
        timeout=30,
    )
    safe_logs = redact(logs_out)
    errors_found = _scan_logs_for_errors(safe_logs)
    health_info["log_errors"] = errors_found
    if errors_found and health_info["status"] == HealthStatus.healthy.value:
        health_info["status"] = HealthStatus.degraded.value

    # If unhealthy, ask Docker AI to analyse logs
    if health_info["status"] in (HealthStatus.unhealthy.value, HealthStatus.failed.value):
        try:
            from orchestrator.docker_deployment_ai.docker_ai_provider import DockerAIProvider  # noqa: PLC0415
            provider = DockerAIProvider()
            ai_result = await provider.explain_runtime_failure(
                logs=safe_logs[:4000],
                inspect_json={"container_status": health_info["container_status"]},
                project_id=project_id,
                target_id=getattr(target, "id", None),
                db=db,
            )
            health_info["runtime_analysis"] = ai_result.get("normalized") or {}
        except Exception as exc:
            logger.warning("Runtime failure analysis failed: %s", exc)

    # Persist health status to run
    await db.execute(
        "UPDATE deploy_runs SET health_status = ? WHERE id = ?",
        (health_info["status"], run_id),
    )
    await db.commit()

    await emit_healthcheck_completed(project_id, run_id, health_info["status"])

    if health_info["status"] in (HealthStatus.healthy.value, HealthStatus.degraded.value):
        await emit_run_completed(project_id, run_id, health_info["status"])
    else:
        await emit_run_failed(project_id, run_id, f"Health status: {health_info['status']}")

    return health_info
